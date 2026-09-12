"""Public remote-job feeds.

Each of these publishes a free, keyless JSON or RSS endpoint intended for
consumption by third parties. They skew towards worldwide-remote roles, which is
exactly the segment an India-based candidate can apply to without relocation -
but many of them restrict hiring to the US or EU, so the location filter still
does real work downstream.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator
from xml.etree import ElementTree

from ..matching.filters import title_prefilter
from .base import JobPosting, Source, html_to_text, looks_remote, parse_iso

log = logging.getLogger(__name__)


class RemoteOKSource(Source):
    """https://remoteok.com/api - single JSON array, no pagination."""

    name = "remoteok"
    ats = None
    LIST_URL = "https://remoteok.com/api"

    def fetch(self) -> Iterator[JobPosting]:
        try:
            data = self.client.get_json(self.LIST_URL)
        except Exception as exc:
            log.warning("remoteok unavailable: %s", exc)
            return
        if not isinstance(data, list):
            return
        count = 0
        # The first element is a legal notice, not a job.
        for item in data:
            if not isinstance(item, dict) or not item.get("position"):
                continue
            title = item.get("position") or ""
            if not title_prefilter(self.config, title):
                continue
            location = item.get("location") or "Remote"
            yield JobPosting(
                source=self.name,
                ats=None,
                external_id=str(item.get("id") or item.get("slug")),
                company=item.get("company") or "Unknown",
                title=title,
                location=location,
                remote=True,
                url=item.get("url") or item.get("apply_url") or "",
                apply_url=item.get("apply_url") or item.get("url"),
                description=html_to_text(item.get("description")),
                posted_at=parse_iso(item.get("date") or item.get("epoch")),
                extra={"tags": item.get("tags")},
            )
            count += 1
        log.info("remoteok scanned (%d candidate postings)", count)


class ArbeitnowSource(Source):
    """https://www.arbeitnow.com/api/job-board-api - paginated JSON, free."""

    name = "arbeitnow"
    ats = None
    LIST_URL = "https://www.arbeitnow.com/api/job-board-api?page={page}"

    def fetch(self) -> Iterator[JobPosting]:
        for page in range(1, max(1, self.config.sources.arbeitnow.max_pages) + 1):
            try:
                data = self.client.get_json(self.LIST_URL.format(page=page))
            except Exception as exc:
                log.warning("arbeitnow page %d failed: %s", page, exc)
                break
            items = (data or {}).get("data") or []
            if not items:
                break
            for item in items:
                title = item.get("title") or ""
                if not title_prefilter(self.config, title):
                    continue
                location = item.get("location")
                yield JobPosting(
                    source=self.name,
                    ats=None,
                    external_id=str(item.get("slug")),
                    company=item.get("company_name") or "Unknown",
                    title=title,
                    location=location,
                    remote=bool(item.get("remote")) or looks_remote(location),
                    url=item.get("url") or "",
                    apply_url=item.get("url"),
                    description=html_to_text(item.get("description")),
                    posted_at=parse_iso(item.get("created_at")),
                    extra={"tags": item.get("tags"), "job_types": item.get("job_types")},
                )
            log.info("arbeitnow page %d scanned", page)


class HimalayasSource(Source):
    """https://himalayas.app/jobs/api - remote roles with region restrictions."""

    name = "himalayas"
    ats = None
    LIST_URL = "https://himalayas.app/jobs/api?limit={limit}&offset={offset}"

    def fetch(self) -> Iterator[JobPosting]:
        limit = min(100, max(10, self.config.sources.himalayas.limit))
        for page in range(max(1, self.config.sources.himalayas.max_pages)):
            url = self.LIST_URL.format(limit=limit, offset=page * limit)
            try:
                data = self.client.get_json(url)
            except Exception as exc:
                log.warning("himalayas offset %d failed: %s", page * limit, exc)
                break
            items = (data or {}).get("jobs") or []
            if not items:
                break
            for item in items:
                title = item.get("title") or ""
                if not title_prefilter(self.config, title):
                    continue
                restrictions = item.get("locationRestrictions") or []
                location = ", ".join(restrictions) if restrictions else "Remote (worldwide)"
                yield JobPosting(
                    source=self.name,
                    ats=None,
                    external_id=str(item.get("guid") or item.get("id") or item.get("applicationLink")),
                    company=item.get("companyName") or "Unknown",
                    title=title,
                    location=location,
                    remote=True,
                    url=item.get("applicationLink") or item.get("url") or "",
                    apply_url=item.get("applicationLink"),
                    description=html_to_text(item.get("description")),
                    posted_at=parse_iso(item.get("pubDate") or item.get("publishedDate")),
                    extra={"seniority": item.get("seniority")},
                )
            log.info("himalayas offset %d scanned", page * limit)


class JobicySource(Source):
    """https://jobicy.com/api/v2/remote-jobs - free, filterable by geo."""

    name = "jobicy"
    ats = None
    LIST_URL = "https://jobicy.com/api/v2/remote-jobs?count={count}&industry={industry}"

    def fetch(self) -> Iterator[JobPosting]:
        industries = self.config.sources.jobicy.filters or ["engineering"]
        count = min(50, max(10, self.config.sources.jobicy.limit))
        for industry in industries:
            try:
                data = self.client.get_json(self.LIST_URL.format(count=count, industry=industry))
            except Exception as exc:
                log.warning("jobicy industry %r failed: %s", industry, exc)
                continue
            for item in (data or {}).get("jobs") or []:
                title = item.get("jobTitle") or ""
                if not title_prefilter(self.config, title):
                    continue
                location = item.get("jobGeo") or "Remote"
                yield JobPosting(
                    source=self.name,
                    ats=None,
                    external_id=str(item.get("id")),
                    company=item.get("companyName") or "Unknown",
                    title=title,
                    location=location,
                    remote=True,
                    url=item.get("url") or "",
                    apply_url=item.get("url"),
                    description=html_to_text(item.get("jobDescription") or item.get("jobExcerpt")),
                    posted_at=parse_iso(item.get("pubDate")),
                    extra={"industry": industry, "level": item.get("jobLevel")},
                )
            log.info("jobicy:%s scanned", industry)


class WeWorkRemotelySource(Source):
    """WeWorkRemotely publishes per-category RSS; the JD lives in the item body."""

    name = "weworkremotely"
    ats = None
    FEED_URL = "https://weworkremotely.com/categories/{category}.rss"
    DEFAULT_CATEGORIES = ["remote-programming-jobs", "remote-back-end-programming-jobs"]

    @staticmethod
    def _split_title(raw: str) -> tuple[str, str]:
        """WWR titles read ``Company: Role``."""
        if ":" in raw:
            company, _, title = raw.partition(":")
            return company.strip(), title.strip()
        return "Unknown", raw.strip()

    def fetch(self) -> Iterator[JobPosting]:
        categories = self.config.sources.weworkremotely.filters or self.DEFAULT_CATEGORIES
        for category in categories:
            try:
                raw = self.client.get_text(self.FEED_URL.format(category=category))
                root = ElementTree.fromstring(raw)
            except Exception as exc:
                log.warning("weworkremotely %r failed: %s", category, exc)
                continue
            for item in root.iter("item"):
                raw_title = (item.findtext("title") or "").strip()
                company, title = self._split_title(raw_title)
                if not title or not title_prefilter(self.config, title):
                    continue
                link = (item.findtext("link") or "").strip()
                description = html_to_text(item.findtext("description"))
                region = item.findtext("region") or ""
                location = region.strip() or "Remote"
                yield JobPosting(
                    source=self.name,
                    ats=None,
                    external_id=re.sub(r"^https?://", "", link)[:180] or raw_title[:180],
                    company=company,
                    title=title,
                    location=location,
                    remote=True,
                    url=link,
                    apply_url=link,
                    description=description,
                    posted_at=parse_iso(_rfc822_to_iso(item.findtext("pubDate"))),
                    extra={"category": category},
                )
            log.info("weworkremotely:%s scanned", category)


_RFC822 = re.compile(
    r"^\w{3},\s+(\d{1,2})\s+(\w{3})\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})"
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
    )
}


def _rfc822_to_iso(value: str | None) -> str | None:
    """RSS dates are RFC 822; ``parse_iso`` only speaks ISO 8601."""
    if not value:
        return None
    match = _RFC822.match(value.strip())
    if not match:
        return None
    day, mon, year, hour, minute, second = match.groups()
    month = _MONTHS.get(mon)
    if not month:
        return None
    return f"{year}-{month:02d}-{int(day):02d}T{hour}:{minute}:{second}+00:00"


FEED_SOURCES: list[type[Source]] = [
    RemoteOKSource,
    ArbeitnowSource,
    HimalayasSource,
    JobicySource,
    WeWorkRemotelySource,
]
