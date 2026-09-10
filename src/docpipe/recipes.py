"""The recipe file: what an agent writes down once a source is figured out.

A recipe is one source that works: an id, the adapter, and the config that
made it work. The file is the durable artifact of the discovery loop, and
the input to every later run. Plain JSON, because an agent has to be able
to read it, append to it, and diff it.

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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

VERSION = 1


@dataclass
class Recipe:
    id: str
    source_type: str
    config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    notes: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


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

    def upsert(self, recipe: Recipe) -> str:
        """Add or replace by id. Returns "added" or "updated"."""
        for i, existing in enumerate(self.sources):
            if existing.id == recipe.id:
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
        ))
    return RecipeBook(path=path, sources=sources)
