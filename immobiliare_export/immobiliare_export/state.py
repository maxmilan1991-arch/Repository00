"""Resume checkpoint for the scraper.

The checkpoint is a small JSON document written next to the SQLite file.
After every page we flush it; on next launch we look for it and, if it
matches the current run signature (same config + searches), we resume from
the next page. Otherwise we drop it and start fresh.

We keep this dead simple — the SQLite DB is the source of truth for what
data we already have; the checkpoint just remembers *where* in the
pagination we were so we don't re-scrape pages we've already covered in
this run.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CheckpointEntry:
    """Per-search resume info."""
    nome: str
    last_completed_page: int = 0
    finished: bool = False
    seen_ids: list[int] = field(default_factory=list)


@dataclass
class Checkpoint:
    config_signature: str
    started_at: str
    entries: dict[str, CheckpointEntry] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "config_signature": self.config_signature,
                "started_at": self.started_at,
                "entries": {k: asdict(v) for k, v in self.entries.items()},
            },
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_json(cls, blob: str) -> "Checkpoint":
        d = json.loads(blob)
        entries = {
            k: CheckpointEntry(**v) for k, v in (d.get("entries") or {}).items()
        }
        return cls(
            config_signature=d.get("config_signature", ""),
            started_at=d.get("started_at", ""),
            entries=entries,
        )


def signature_for_config(raw_yaml: str, search_names: list[str]) -> str:
    h = hashlib.sha256()
    h.update(raw_yaml.encode("utf-8"))
    h.update(b"|")
    h.update("|".join(sorted(search_names)).encode("utf-8"))
    return h.hexdigest()[:16]


def default_checkpoint_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".checkpoint.json")


def load(path: str | Path) -> Checkpoint | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return Checkpoint.from_json(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(path: str | Path, cp: Checkpoint) -> None:
    """Atomic write so a crash mid-flush can't leave a half-written file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), prefix=".cp.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(cp.to_json())
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def clear(path: str | Path) -> None:
    p = Path(path)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
