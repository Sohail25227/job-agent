"""Workday career sites.

Most large employers hiring in India (product companies, GCCs, banks) run their
careers page on Workday. Every one of those pages is a thin client over a JSON
endpoint at ``/wday/cxs/{tenant}/{site}/jobs``, which is what the browser itself
calls - so this reads the same public API the page does.

Workday's location facets are tenant-specific GUIDs that cannot be hardcoded,
so keywords go through ``searchText`` and location filtering happens downstream
in the matching layer.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Iterator

from ..matching.filters import location_intent, title_prefilter
from ..models import utcnow
from .base import JobPosting, Source, html_to_text, looks_remote, parse_iso

log = logging.getLogger(__name__)

PAGE_SIZE = 20

_RELATIVE = re.compile(r"(\d+)\+?\s*(day|hour|minute|week|month)", re.I)
_UNIT_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30}


def _parse_posted(posted_on: str | None, start_date: str | None):
    """Prefer the ISO start date; fall back to Workday's "Posted 3 Days Ago"."""
    absolute = parse_iso(start_date)
    if absolute:
        return absolute
    if not posted_on:
        return None
    text = posted_on.strip().lower()
    if "today" in text or "just posted" in text:
        return utcnow()
    if "yesterday" in text:
        return utcnow() - timedelta(days=1)
    match = _RELATIVE.search(text)
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    return utcnow() - timedelta(days=amount * _UNIT_DAYS.get(unit, 1))


class WorkdaySource(Source):
    name = "workday"
    ats = "workday"

    def _search(self, tenant, query: str, offset: int) -> dict:
        url = f"https://{tenant.host}/wday/cxs/{tenant.tenant}/{tenant.site}/jobs"
        payload = {
            "appliedFacets": {},
            "limit": PAGE_SIZE,
            "offset": offset,
            "searchText": query,
        }
        data = self.client.post_json(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        return data if isinstance(data, dict) else {}

    def _description(self, tenant, external_path: str) -> tuple[str, str | None]:
        """Fetch the JD body. Returns (description, canonical apply url)."""
        url = f"https://{tenant.host}/wday/cxs/{tenant.tenant}/{tenant.site}{external_path}"
        try:
            data = self.client.get_json(url)
        except Exception as exc:
            log.debug("workday detail %s failed: %s", external_path, exc)
            return "", None
        info = (data or {}).get("jobPostingInfo") or {}
        return html_to_text(info.get("jobDescription")), info.get("externalUrl")

    def _fetch_tenant(self, tenant, queries: list[str]) -> list[JobPosting]:
        settings = self.config.sources.workday
        label = tenant.label or tenant.tenant
        found: list[JobPosting] = []
        seen: set[str] = set()
        budget = settings.descriptions_per_tenant

        for query in queries:
            for page in range(max(1, settings.max_pages)):
                try:
                    data = self._search(tenant, query, page * PAGE_SIZE)
                except Exception as exc:
                    log.warning("workday %s query %r failed: %s", label, query, exc)
                    break
                postings = data.get("jobPostings") or []
                if not postings:
                    break
                for item in postings:
                    title = item.get("title") or ""
                    external_path = item.get("externalPath") or ""
                    if not external_path or external_path in seen:
                        continue
                    seen.add(external_path)
                    if not title_prefilter(self.config, title):
                        continue
                    location = item.get("locationsText")
                    # A tenant like NVIDIA posts mostly in the US. Classify from
                    # the free list response so the description budget is spent
                    # on postings that could actually be taken, instead of being
                    # exhausted on Santa Clara.
                    if location_intent(self.config, location) == "foreign":
                        continue
                    public_url = f"https://{tenant.host}/en-US/{tenant.site}{external_path}"
                    description, canonical = "", None
                    if budget > 0:
                        description, canonical = self._description(tenant, external_path)
                        budget -= 1
                    req_id = (item.get("bulletFields") or [None])[0]
                    found.append(
                        JobPosting(
                            source=self.name,
                            ats=self.ats,
                            external_id=f"{tenant.tenant}:{req_id or external_path}",
                            company=label,
                            title=title,
                            location=location,
                            remote=looks_remote(location, title),
                            url=canonical or public_url,
                            apply_url=canonical or public_url,
                            description=description,
                            posted_at=_parse_posted(item.get("postedOn"), item.get("startDate")),
                            extra={"tenant": tenant.tenant, "req_id": req_id, "query": query},
                        )
                    )
                if len(postings) < PAGE_SIZE:
                    break
        log.info("workday:%s scanned (%d postings)", label, len(found))
        return found

    def fetch(self) -> Iterator[JobPosting]:
        """Scan every tenant, several at a time.

        Each tenant is a different host, so they do not contend for the same
        politeness window - and there are enough of them that doing this one at
        a time made Workday alone longer than every other source combined.
        """
        settings = self.config.sources.workday
        queries = settings.queries or [""]
        tenants = list(settings.tenants)
        if not tenants:
            return

        workers = min(settings.concurrency, len(tenants))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._fetch_tenant, tenant, queries) for tenant in tenants]
            for future in as_completed(futures):
                try:
                    yield from future.result()
                except Exception as exc:
                    log.warning("workday tenant failed entirely: %s", exc)


WORKDAY_SOURCES: list[type[Source]] = [WorkdaySource]
