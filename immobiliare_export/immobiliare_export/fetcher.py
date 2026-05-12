"""HTML fetching with Playwright + retries + adaptive rate limit.

immobiliare.it is fronted by Cloudflare/DataDome. The fetcher supports
two modes:

* **Launch mode (default).** Playwright launches its own bundled
  Chromium, creates a fresh context, and patches every new page with
  ``playwright-stealth`` before the first navigation. Stealth rewrites
  the JS surface (``navigator.webdriver``, plugins, ``chrome.runtime``,
  WebGL vendor, …) so the page looks like a real Chrome session.
  Sufficient for many DataDome deployments, but not always.

* **CDP mode** (``connect_to_existing_browser: true``). DataDome can
  still fingerprint a stealth-patched Playwright session — at that point
  the only reliable workaround is to drive a *real* Chrome the user is
  running themselves with ``--remote-debugging-port``. The fetcher
  ``connect_over_cdp`` to that Chrome, reuses its first context and
  first tab, and never closes them. No stealth is applied (the browser
  is genuine) and no user-agent / viewport are forced (we inherit
  whatever the user's Chrome reports). See the README for the launch
  instructions.

The fetcher has three responsibilities:

1. own the Playwright lifecycle (browser, context, page);
2. retry transient HTTP errors with exponential backoff;
3. adapt the per-page delay if the site responds with 429 / 403 (rate
   limiting) — double it for the next 5 pages, then return to normal.

The class deliberately does not know anything about the listings shape;
the caller hands the HTML over to ``parser.parse_results_page``.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Protocol

logger = logging.getLogger(__name__)


class FetcherError(RuntimeError):
    pass


class BlockedError(FetcherError):
    """Raised when a CAPTCHA / 403 / antibot block is detected."""


class _PageLike(Protocol):
    """Minimal protocol so tests can pass a stub instead of a real Page."""

    def goto(self, url: str, **kwargs) -> object: ...
    def content(self) -> str: ...
    def close(self) -> None: ...


@dataclass
class FetchResult:
    url: str
    html: str
    status: int
    attempts: int


class Fetcher:
    """Stateful page fetcher.

    Use as a context manager:

        with Fetcher(headless=True) as f:
            res = f.fetch(url)
            html = res.html
    """

    BASE_DELAY_MULTIPLIER_RESET_AFTER = 5  # pages
    SLOWDOWN_MULTIPLIER = 2.0

    def __init__(
        self,
        *,
        headless: bool = True,
        delay_between_pages_sec: float = 2.0,
        max_attempts_per_page: int = 3,
        retry_backoff_sec: float = 5.0,
        request_timeout_ms: int = 30000,
        user_agent: str | None = None,
        connect_to_existing_browser: bool = False,
        cdp_endpoint: str = "http://localhost:9222",
    ) -> None:
        self.headless = headless
        self.base_delay = float(delay_between_pages_sec)
        self.max_attempts = int(max_attempts_per_page)
        self.retry_backoff = float(retry_backoff_sec)
        self.timeout_ms = int(request_timeout_ms)
        self.connect_to_existing_browser = bool(connect_to_existing_browser)
        self.cdp_endpoint = str(cdp_endpoint)
        # A current, *stable* Chrome on Windows 10 is the most common UA in
        # the wild, so it draws the least scrutiny from DataDome / Cloudflare.
        # Bump alongside playwright-stealth's bundled Chrome version.
        # Ignored in CDP mode — we inherit the real Chrome's UA.
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        )

        self._slowdown_remaining = 0
        self._healthy_pages_in_a_row = 0
        self._playwright = None
        self._browser = None
        self._context = None
        # Resolved lazily inside ``_start_browser`` so the rest of the
        # package keeps importing in environments without playwright-stealth
        # (e.g. CI running just the unit tests). Always ``None`` in CDP
        # mode — a real Chrome doesn't need stealth.
        self._stealth = None
        # In CDP mode we reuse the user's first tab across every fetch
        # instead of opening / closing a new page each time.
        self._cdp_page = None

    # --------------------------------------------------------------- lifecycle
    def __enter__(self) -> "Fetcher":
        self._start_browser()
        return self

    def __exit__(self, *exc) -> None:
        self._stop_browser()

    def _start_browser(self) -> None:
        # Imported lazily so the rest of the package (parser, db, exporter)
        # is usable in environments where Playwright isn't installed
        # (e.g. unit tests that operate purely on cached HTML fixtures).
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise FetcherError(
                "playwright is not installed. Install it with "
                "`pip install playwright` and run `playwright install chromium`."
            ) from e

        self._playwright = sync_playwright().start()

        if self.connect_to_existing_browser:
            self._start_cdp()
        else:
            self._start_launched()

    def _start_launched(self) -> None:
        """Launch a fresh Chromium and patch each new page with stealth."""
        # Stealth is required to defeat immobiliare.it's antibot in launch
        # mode. Without it, every navigation comes back 403. Loaded lazily
        # so the ImportError message is actionable.
        try:
            from playwright_stealth import Stealth
        except ImportError as e:
            raise FetcherError(
                "playwright-stealth is not installed. Install it with "
                "`pip install 'playwright-stealth>=2.0'`. Without it the "
                "site's antibot answers 403 to every request. As a stronger "
                "fallback set ``connect_to_existing_browser: true`` in the "
                "YAML config and drive your real Chrome via CDP."
            ) from e
        self._stealth = Stealth()

        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(
            user_agent=self.user_agent,
            locale="it-IT",
            # Full-HD matches the most common desktop screen size and
            # avoids the "small viewport" signal flagged by some antibots.
            viewport={"width": 1920, "height": 1080},
        )
        # A blanket timeout is friendlier than per-call ones; we still
        # treat timeouts as retryable below.
        self._context.set_default_navigation_timeout(self.timeout_ms)

    def _start_cdp(self) -> None:
        """Attach to the user's already-running Chrome via CDP.

        Expects Chrome to have been launched with
        ``--remote-debugging-port=<port>``. We reuse the first existing
        context (the user's profile) and the first existing tab, so the
        scrape inherits the cookies / fingerprint of a genuine session.
        No stealth patching, no UA/viewport overrides — anything we'd set
        would only make us *look more synthetic* to DataDome.
        """
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(
                self.cdp_endpoint
            )
        except Exception as e:
            raise FetcherError(
                f"could not connect to Chrome at {self.cdp_endpoint!r}: {e}. "
                "Make sure Chrome is running with "
                "``--remote-debugging-port=9222`` (see README, "
                "'Modalità CDP / Chrome reale')."
            ) from e

        contexts = list(self._browser.contexts)
        if not contexts:
            # ``connect_over_cdp`` against a CDP endpoint that has no
            # default context is unusual but possible — make one rather
            # than crash, so the fetcher still works.
            self._context = self._browser.new_context()
        else:
            self._context = contexts[0]
        self._context.set_default_navigation_timeout(self.timeout_ms)

        pages = list(self._context.pages)
        if pages:
            self._cdp_page = pages[0]
        else:
            self._cdp_page = self._context.new_page()
        logger.info(
            "connected to Chrome over CDP at %s (context=%d, pages=%d)",
            self.cdp_endpoint, len(contexts), len(pages),
        )

    def _stop_browser(self) -> None:
        # In CDP mode we are guests in the user's Chrome — closing the
        # context, the browser, or the tab would kill their session. We
        # only release the Playwright handle.
        if not self.connect_to_existing_browser:
            try:
                if self._context:
                    self._context.close()
            finally:
                self._context = None
            try:
                if self._browser:
                    self._browser.close()
            finally:
                self._browser = None
        else:
            self._context = None
            self._browser = None
            self._cdp_page = None
        try:
            if self._playwright:
                self._playwright.stop()
        finally:
            self._playwright = None

    # --------------------------------------------------------------- fetching
    def fetch(self, url: str) -> FetchResult:
        """Fetch a single URL with retry/backoff. Raises on permanent failure."""
        attempt = 0
        last_exc: Exception | None = None
        while attempt < self.max_attempts:
            attempt += 1
            try:
                html, status = self._goto(url)
            except Exception as e:
                last_exc = e
                logger.warning(
                    "fetch attempt %d/%d failed for %s: %s",
                    attempt, self.max_attempts, url, e,
                )
                time.sleep(self.retry_backoff * attempt)
                continue

            if status in (403, 429):
                # Rate-limit / antibot. Adapt + retry.
                self._trigger_slowdown()
                logger.warning(
                    "rate-limit/antibot status %d for %s (attempt %d/%d)",
                    status, url, attempt, self.max_attempts,
                )
                time.sleep(self.retry_backoff * attempt)
                continue

            if status >= 500:
                logger.warning(
                    "server error %d for %s (attempt %d/%d)",
                    status, url, attempt, self.max_attempts,
                )
                time.sleep(self.retry_backoff * attempt)
                continue

            if "__NEXT_DATA__" not in html:
                # Looks like an antibot interstitial.
                logger.warning(
                    "page %s returned status %d but no __NEXT_DATA__ found",
                    url, status,
                )
                self._trigger_slowdown()
                raise BlockedError(
                    f"page {url} did not contain __NEXT_DATA__ "
                    "(likely Cloudflare/DataDome challenge)"
                )

            self._on_healthy_page()
            return FetchResult(url=url, html=html, status=status, attempts=attempt)

        raise FetcherError(
            f"failed to fetch {url} after {self.max_attempts} attempts: {last_exc}"
        )

    def _goto(self, url: str) -> tuple[str, int]:
        if not self._context:
            raise FetcherError("fetcher used outside its context manager")

        if self._cdp_page is not None:
            # CDP mode: reuse the user's first tab across every fetch.
            # Do NOT close it on exit — it belongs to the user.
            page = self._cdp_page
            response = page.goto(url, wait_until="domcontentloaded")
            status = response.status if response is not None else 0
            return page.content(), status

        # Launch mode: a fresh page per fetch, stealth-patched before the
        # first navigation (stealth's hooks ride on Page.add_init_script,
        # which only fires on subsequent goto() calls).
        page = self._context.new_page()
        if self._stealth is not None:
            try:
                self._stealth.apply_stealth_sync(page)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("apply_stealth_sync failed (%s); continuing without it", e)
        try:
            response = page.goto(url, wait_until="domcontentloaded")
            status = response.status if response is not None else 0
            html = page.content()
            return html, status
        finally:
            page.close()

    # ------------------------------------------------------------ rate limit
    def current_delay(self) -> float:
        if self._slowdown_remaining > 0:
            return self.base_delay * self.SLOWDOWN_MULTIPLIER
        return self.base_delay

    def sleep_between_pages(self) -> None:
        delay = self.current_delay()
        if delay > 0:
            time.sleep(delay)

    def _trigger_slowdown(self) -> None:
        self._slowdown_remaining = self.BASE_DELAY_MULTIPLIER_RESET_AFTER
        self._healthy_pages_in_a_row = 0

    def _on_healthy_page(self) -> None:
        if self._slowdown_remaining > 0:
            self._healthy_pages_in_a_row += 1
            if self._healthy_pages_in_a_row >= self.BASE_DELAY_MULTIPLIER_RESET_AFTER:
                self._slowdown_remaining = 0
                self._healthy_pages_in_a_row = 0


@contextmanager
def make_fetcher(**kwargs) -> Iterator[Fetcher]:
    """Convenience helper for ``with make_fetcher(...) as f:`` syntax."""
    f = Fetcher(**kwargs)
    f._start_browser()
    try:
        yield f
    finally:
        f._stop_browser()
