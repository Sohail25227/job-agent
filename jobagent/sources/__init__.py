"""Source registry."""

from __future__ import annotations

import logging
from typing import Iterator

from ..config import Config
from ..http import HttpClient
from .aggregators import AGGREGATOR_SOURCES
from .ats import ATS_SOURCES
from .base import JobPosting, Source
from .discovery import DISCOVERY_SOURCES
from .feeds import FEED_SOURCES
from .workday import WORKDAY_SOURCES

log = logging.getLogger(__name__)

ALL_SOURCES: list[type[Source]] = [
    *ATS_SOURCES,
    *WORKDAY_SOURCES,
    *DISCOVERY_SOURCES,
    *AGGREGATOR_SOURCES,
    *FEED_SOURCES,
]
SOURCES_BY_NAME: dict[str, type[Source]] = {cls.name: cls for cls in ALL_SOURCES}


def enabled_sources(config: Config, only: list[str] | None = None) -> list[type[Source]]:
    selected = []
    for cls in ALL_SOURCES:
        if only and cls.name not in only:
            continue
        source_config = getattr(config.sources, cls.name, None)
        if only or (source_config and source_config.enabled):
            selected.append(cls)
    return selected


def collect(config: Config, client: HttpClient, only: list[str] | None = None) -> Iterator[JobPosting]:
    """Yield postings from every enabled source, isolating per-source failures."""
    for cls in enabled_sources(config, only):
        source = cls(config, client)
        try:
            yield from source.fetch()
        except Exception as exc:
            log.error("source %s failed entirely: %s", cls.name, exc, exc_info=True)


__all__ = ["ALL_SOURCES", "SOURCES_BY_NAME", "JobPosting", "Source", "collect", "enabled_sources"]
