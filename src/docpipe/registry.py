"""Source-type registry.

The original implementation hard-coded a dict of every adapter, so adding
one meant editing the library. Here adapters register themselves:

    from docpipe import BaseSource, register_source

    @register_source("my_portal")
    class MyPortalSource(BaseSource):
        config_model = MyPortalConfig
        ...

Host projects can register their own adapters at import time and resolve
them through the same `get_source` entry point as the built-ins.
"""
from __future__ import annotations

from typing import Callable, Optional, TypeVar

from docpipe.base import BaseSource
from docpipe.settings import Settings

_REGISTRY: dict[str, type[BaseSource]] = {}

T = TypeVar("T", bound=type[BaseSource])


def register_source(*names: str) -> Callable[[T], T]:
    """Class decorator registering an adapter under one or more names."""

    def decorator(cls: T) -> T:
        for name in names:
            existing = _REGISTRY.get(name)
            if existing is not None and existing is not cls:
                raise ValueError(
                    f"source_type {name!r} is already registered to "
                    f"{existing.__name__}"
                )
            _REGISTRY[name] = cls
        return cls

    return decorator


def available_sources() -> list[str]:
    """Registered source_type names, sorted."""
    return sorted(_REGISTRY)


def source_class(source_type: str) -> type[BaseSource]:
    try:
        return _REGISTRY[source_type]
    except KeyError:
        raise ValueError(
            f"Unknown source_type: {source_type!r}. "
            f"Registered: {', '.join(available_sources()) or '(none)'}"
        ) from None


def get_source(
    source_type: str,
    source_id: str,
    config: dict | object,
    settings: Optional[Settings] = None,
) -> BaseSource:
    """Instantiate an adapter, validating `config` against its schema.

    `config` is usually a raw dict (from JSON, a DB column, a YAML file).
    It is coerced through the adapter's pydantic model so a missing or
    misspelled key fails loudly here instead of mid-scrape.
    """
    cls = source_class(source_type)
    validated = (
        config
        if isinstance(config, cls.config_model)
        else cls.config_model.model_validate(config)
    )
    return cls(source_id, validated, settings=settings)
