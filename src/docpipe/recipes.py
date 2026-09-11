"""The recipe file: what an agent writes down once a source is figured out,
and what it learns about that source afterwards.

A recipe is one source that works: an id, the adapter, and the config that
made it work. The file is the durable artifact of the discovery loop, and
the input to every later run. Plain JSON, because an agent has to be able
to read it, append to it, and diff it.

Each recipe also carries a `memory` block that `docpipe run --remember`
updates: how often the source has worked, when it last did, what shape its
document URLs have, and whether it has failed enough times in a row to be
quarantined. That memory is what lets a fleet of a few hundred sources be
maintained by exception. Without it, every run looks the same and a source
that quietly died a month ago is indistinguishable from one that works.

    {
      "version": 1,
      "sources": [
        {
          "id": "acme-county",
          "source_type": "pdf_direct",
          "config": {"page_url": "https://example.org/board/minutes"},
          "enabled": true,
          "notes": "Verified 2026-09-10: 5 documents, pdfplumber."
        }
      ]
    }

`upsert` is idempotent on `id`, so an agent re-running discovery for a
source it already knows updates the entry instead of duplicating it.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

VERSION = 1

# Consecutive failures, with no success in between, before a source is
# quarantined. Three is enough to rule out a transient outage without
# leaving a dead source in the rotation for weeks.
QUARANTINE_AFTER_FAILURES = 3


# Tokens that vary between documents of the same source: dates and running
# numbers. Everything else is treated as part of the fixed skeleton.
_VARIABLE_TOKENS = re.compile(r"\d{4}-\d{2}-\d{2}|\d{8}|\d+")

# A UUID-per-document scheme (resource managers, CMS asset stores) has no
# useful skeleton beyond the path, so hex runs collapse too.
_HEX_RUN = re.compile(r"[0-9a-f]{8,}", re.IGNORECASE)


def derive_url_pattern(urls: list[str]) -> Optional[str]:
    """Return a regex matching the shape these document URLs share, or None.

    Two or more URLs are needed, and they must reduce to the same skeleton
    once dates, numbers and hex ids are blanked out. The result is a
    tripwire, not a filter: when a later run returns URLs that no longer
    match, the site changed under us.
    """
    if len(urls) < 2:
        return None

    skeletons = set()
    for url in urls:
        # Escape first so the literal parts stay literal, then swap the
        # placeholders in. Doing it the other way round would escape the
        # regex we just inserted.
        marked = _HEX_RUN.sub("\x00HEX\x00", url)
        marked = _VARIABLE_TOKENS.sub("\x00NUM\x00", marked)
        skeleton = re.escape(marked)
        skeleton = skeleton.replace("\x00HEX\x00", "[0-9a-fA-F]+")
        skeleton = skeleton.replace("\x00NUM\x00", "[0-9-]+")
        skeletons.add(skeleton)

    if len(skeletons) != 1:
        return None
    return "^" + skeletons.pop() + "$"


@dataclass
class SourceMemory:
    """What running a source has taught us about it.

    Counters are cumulative; `consecutive_failures` is what drives
    quarantine, so one success anywhere resets the clock. Quarantine is
    never silent: it records why, and `docpipe run` reports what it skipped.
    """

    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    last_status: Optional[str] = None
    last_ok_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_error: Optional[str] = None
    doc_url_pattern: Optional[str] = None
    quarantined: bool = False
    quarantine_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, 0, False)}

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "SourceMemory":
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def record(self, status: str, document_urls: list[str], error: Optional[str] = None) -> None:
        """Fold one run's outcome into the memory.

        Only a clean "ok" counts as a success. "partial" means documents
        came back without usable text, which is a failure of the thing the
        source exists to do.
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.last_run_at = now
        self.last_status = status

        if status == "ok":
            self.success_count += 1
            self.consecutive_failures = 0
            self.last_ok_at = now
            self.last_error = None
            if self.quarantined:
                self.quarantined = False
                self.quarantine_reason = None
            learned = derive_url_pattern(document_urls)
            if learned:
                self.doc_url_pattern = learned
            return

        self.failure_count += 1
        self.consecutive_failures += 1
        self.last_error = error
        if self.consecutive_failures >= QUARANTINE_AFTER_FAILURES and self.success_count == 0:
            # Never worked and keeps failing: almost always a bad config
            # recorded without --verify, not a site that broke.
            self._quarantine(
                f"{self.consecutive_failures} runs, never succeeded. "
                "The config is probably wrong: re-run `docpipe probe --verify`."
            )
        elif self.consecutive_failures >= QUARANTINE_AFTER_FAILURES:
            self._quarantine(
                f"{self.consecutive_failures} consecutive failures since "
                f"{self.last_ok_at or 'unknown'}. The site most likely changed."
            )

    def _quarantine(self, reason: str) -> None:
        self.quarantined = True
        self.quarantine_reason = reason

    def matches_learned_shape(self, document_urls: list[str]) -> bool:
        """False when documents stop looking like the ones we learned.

        A source can return documents and still be broken: a redesign that
        swaps the archive for a "page not found" list still yields links.
        A sudden change of URL shape is the cheapest signal that something
        moved.
        """
        if not self.doc_url_pattern or not document_urls:
            return True
        return all(re.match(self.doc_url_pattern, url) for url in document_urls)


@dataclass
class Recipe:
    id: str
    source_type: str
    config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    notes: Optional[str] = None
    memory: SourceMemory = field(default_factory=SourceMemory)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "source_type": self.source_type,
            "config": self.config,
            "enabled": self.enabled,
        }
        if self.notes is not None:
            data["notes"] = self.notes
        memory = self.memory.to_dict()
        if memory:
            data["memory"] = memory
        return data


@dataclass
class RecipeBook:
    path: Optional[Path] = None
    sources: list[Recipe] = field(default_factory=list)

    def __iter__(self) -> Iterator[Recipe]:
        return iter(self.sources)

    def __len__(self) -> int:
        return len(self.sources)

    def get(self, source_id: str) -> Optional[Recipe]:
        return next((r for r in self.sources if r.id == source_id), None)

    def enabled_sources(self) -> list[Recipe]:
        return [r for r in self.sources if r.enabled]

    def active_sources(self) -> list[Recipe]:
        """Enabled and not quarantined: what a routine run should touch."""
        return [r for r in self.sources if r.enabled and not r.memory.quarantined]

    def quarantined_sources(self) -> list[Recipe]:
        return [r for r in self.sources if r.enabled and r.memory.quarantined]

    def upsert(self, recipe: Recipe) -> str:
        """Add or replace by id. Returns "added" or "updated".

        An update keeps the existing memory unless the caller brought its
        own: re-probing a source that changed platform should not erase the
        record of how it has behaved.
        """
        for i, existing in enumerate(self.sources):
            if existing.id == recipe.id:
                if recipe.memory == SourceMemory():
                    recipe.memory = existing.memory
                self.sources[i] = recipe
                return "updated"
        self.sources.append(recipe)
        return "added"

    def to_dict(self) -> dict[str, Any]:
        return {"version": VERSION, "sources": [r.to_dict() for r in self.sources]}

    def save(self, path: Optional[Path] = None) -> Path:
        target = Path(path or self.path or "")
        if not str(target):
            raise ValueError("No path to save to")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        self.path = target
        return target


def load(path: Path | str) -> RecipeBook:
    """Read a recipe file. A missing file yields an empty book, so an agent
    can append to a path that does not exist yet."""
    path = Path(path)
    if not path.exists():
        return RecipeBook(path=path)

    data = json.loads(path.read_text())
    if not isinstance(data, dict) or "sources" not in data:
        raise ValueError(f"{path} is not a recipe file (expected a 'sources' key)")

    sources = []
    for i, entry in enumerate(data["sources"]):
        missing = {"id", "source_type"} - set(entry)
        if missing:
            raise ValueError(f"{path}: source #{i} is missing {sorted(missing)}")
        sources.append(Recipe(
            id=entry["id"],
            source_type=entry["source_type"],
            config=entry.get("config", {}),
            enabled=entry.get("enabled", True),
            notes=entry.get("notes"),
            memory=SourceMemory.from_dict(entry.get("memory")),
        ))
    return RecipeBook(path=path, sources=sources)
