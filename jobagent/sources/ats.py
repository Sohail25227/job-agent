"""Public ATS job-board APIs.

These endpoints are the same ones the companies' own careers pages call from
the browser. They are public, documented by usage, require no credentials, and
returning JSON is their intended purpose - so there is no scraping involved and
nothing to break when a page's markup changes.
"""

from __future__ import annotations

import logging
from typing import Iterable, Iterator

from ..matching.filters import title_prefilter
from .base import JobPosting, Source, html_to_text, looks_remote, parse_iso

log = logging.getLogger(__name__)


def _titleize(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


class GreenhouseSource(Source):
    name = "greenhouse"
    ats = "greenhouse"
    LIST_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
    META_URL = "https://boards-api.greenhouse.io/v1/boards/{board}"

    def _company_name(self, board: str) -> str:
        try:
            meta = self.client.get_json(self.META_URL.format(board=board))
            if isinstance(meta, dict) and meta.get("name"):
                return str(meta["name"])
        except Exception:
            pass
        return _titleize(board)

    def fetch(self) -> Iterator[JobPosting]:
        for board in self.config.sources.greenhouse.boards:
            try:
                data = self.client.get_json(self.LIST_URL.format(board=board))
            except Exception as exc:
                log.warning("greenhouse board %r unavailable: %s", board, exc)
                continue
            jobs = data.get("jobs", []) if isinstance(data, dict) else []
            if not jobs:
                continue
            company = self._company_name(board)
            for item in jobs:
                title = item.get("title") or ""
                if not title_prefilter(self.config, title):
                    continue
                location = (item.get("location") or {}).get("name")
                description = html_to_text(item.get("content"))
                url = item.get("absolute_url") or ""
                yield JobPosting(
                    source=self.name,
                    ats=self.ats,
                    external_id=f"{board}:{item.get('id')}",
                    company=company,
                    title=title,
                    location=location,
                    remote=looks_remote(location, title),
                    url=url,
                    apply_url=url,
                    description=description,
                    posted_at=parse_iso(item.get("first_published") or item.get("updated_at")),
                    extra={"board": board},
                )
            log.info("greenhouse:%s scanned", board)


class LeverSource(Source):
    name = "lever"
    ats = "lever"
    LIST_URL = "https://api.lever.co/v0/postings/{board}?mode=json"

    @staticmethod
    def _description(item: dict) -> str:
        parts = [item.get("descriptionPlain") or html_to_text(item.get("description"))]
        for block in item.get("lists") or []:
            heading = block.get("text") or ""
            body = html_to_text(block.get("content"))
            parts.append(f"\n{heading}\n{body}" if heading else body)
        parts.append(item.get("additionalPlain") or html_to_text(item.get("additional")))
        return "\n".join(p for p in parts if p).strip()

    def fetch(self) -> Iterator[JobPosting]:
        for board in self.config.sources.lever.boards:
            try:
                data = self.client.get_json(self.LIST_URL.format(board=board))
            except Exception as exc:
                log.warning("lever board %r unavailable: %s", board, exc)
                continue
            if not isinstance(data, list):
                continue
            for item in data:
                title = item.get("text") or ""
                if not title_prefilter(self.config, title):
                    continue
                categories = item.get("categories") or {}
                location = categories.get("location")
                url = item.get("hostedUrl") or ""
                yield JobPosting(
                    source=self.name,
                    ats=self.ats,
                    external_id=f"{board}:{item.get('id')}",
                    company=_titleize(board),
                    title=title,
                    location=location,
                    remote=looks_remote(location, categories.get("commitment"), title),
                    url=url,
                    apply_url=item.get("applyUrl") or (f"{url}/apply" if url else None),
                    description=self._description(item),
                    posted_at=parse_iso(item.get("createdAt")),
                    extra={"board": board, "team": categories.get("team")},
                )
            log.info("lever:%s scanned", board)


class AshbySource(Source):
    name = "ashby"
    ats = "ashby"
    LIST_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"

    def fetch(self) -> Iterator[JobPosting]:
        for board in self.config.sources.ashby.boards:
            try:
                data = self.client.get_json(self.LIST_URL.format(board=board))
            except Exception as exc:
                log.warning("ashby board %r unavailable: %s", board, exc)
                continue
            if not isinstance(data, dict):
                continue
            for item in data.get("jobs", []):
                title = item.get("title") or ""
                if not title_prefilter(self.config, title):
                    continue
                if item.get("isListed") is False:
                    continue
                location = item.get("location") or item.get("locationName")
                url = item.get("jobUrl") or ""
                description = item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml"))
                yield JobPosting(
                    source=self.name,
                    ats=self.ats,
                    external_id=f"{board}:{item.get('id')}",
                    company=data.get("name") or _titleize(board),
                    title=title,
                    location=location,
                    remote=bool(item.get("isRemote")) or looks_remote(location, title),
                    url=url,
                    apply_url=item.get("applyUrl") or (f"{url}/application" if url else None),
                    description=description,
                    posted_at=parse_iso(item.get("publishedAt") or item.get("updatedAt")),
                    extra={"board": board, "department": item.get("department")},
                )
            log.info("ashby:%s scanned", board)


class SmartRecruitersSource(Source):
    name = "smartrecruiters"
    ats = "smartrecruiters"
    LIST_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset={offset}"
    DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}"
    APPLY_URL = "https://jobs.smartrecruiters.com/{company}/{posting_id}"

    def _description(self, company: str, posting_id: str) -> str:
        try:
            detail = self.client.get_json(
                self.DETAIL_URL.format(company=company, posting_id=posting_id)
            )
        except Exception as exc:
            log.debug("smartrecruiters detail %s/%s failed: %s", company, posting_id, exc)
            return ""
        sections = ((detail or {}).get("jobAd") or {}).get("sections") or {}
        chunks = []
        for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
            section = sections.get(key) or {}
            text = html_to_text(section.get("text"))
            if text:
                chunks.append(f"{section.get('title') or key}\n{text}")
        return "\n\n".join(chunks)

    def fetch(self) -> Iterator[JobPosting]:
        for company in self.config.sources.smartrecruiters.companies:
            offset = 0
            while True:
                try:
                    data = self.client.get_json(
                        self.LIST_URL.format(company=company, offset=offset)
                    )
                except Exception as exc:
                    log.warning("smartrecruiters %r unavailable: %s", company, exc)
                    break
                content = data.get("content", []) if isinstance(data, dict) else []
                if not content:
                    break
                for item in content:
                    title = item.get("name") or ""
                    if not title_prefilter(self.config, title):
                        continue
                    posting_id = str(item.get("id"))
                    loc = item.get("location") or {}
                    location = ", ".join(
                        p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p
                    )
                    apply_url = self.APPLY_URL.format(company=company, posting_id=posting_id)
                    yield JobPosting(
                        source=self.name,
                        ats=self.ats,
                        external_id=f"{company}:{posting_id}",
                        company=(item.get("company") or {}).get("name") or _titleize(company),
                        title=title,
                        location=location or None,
                        remote=bool(loc.get("remote")) or looks_remote(location, title),
                        url=apply_url,
                        apply_url=apply_url,
                        description=self._description(company, posting_id),
                        posted_at=parse_iso(item.get("releasedDate")),
                        extra={"company_slug": company},
                    )
                offset += len(content)
                total = data.get("totalFound") if isinstance(data, dict) else None
                if total is None or offset >= int(total) or offset >= 300:
                    break
            log.info("smartrecruiters:%s scanned", company)


class WorkableSource(Source):
    name = "workable"
    ats = "workable"
    LIST_URL = "https://apply.workable.com/api/v1/widget/accounts/{company}?details=true"

    def fetch(self) -> Iterator[JobPosting]:
        for company in self.config.sources.workable.companies:
            try:
                data = self.client.get_json(self.LIST_URL.format(company=company))
            except Exception as exc:
                log.warning("workable %r unavailable: %s", company, exc)
                continue
            if not isinstance(data, dict):
                continue
            for item in data.get("jobs", []):
                title = item.get("title") or ""
                if not title_prefilter(self.config, title):
                    continue
                loc = item.get("location") or {}
                location = ", ".join(
                    p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p
                )
                url = item.get("url") or item.get("application_url") or ""
                yield JobPosting(
                    source=self.name,
                    ats=self.ats,
                    external_id=f"{company}:{item.get('shortcode') or item.get('id')}",
                    company=data.get("name") or _titleize(company),
                    title=title,
                    location=location or None,
                    remote=bool(item.get("telecommuting")) or looks_remote(location, title),
                    url=url,
                    apply_url=item.get("application_url") or url,
                    description=html_to_text(item.get("description")) or item.get("description"),
                    posted_at=parse_iso(item.get("published_on") or item.get("created_at")),
                    extra={"company_slug": company},
                )
            log.info("workable:%s scanned", company)


class RecruiteeSource(Source):
    """Recruitee boards answer at ``{company}.recruitee.com/api/offers/``."""

    name = "recruitee"
    ats = "recruitee"
    LIST_URL = "https://{company}.recruitee.com/api/offers/"

    @staticmethod
    def _description(item: dict) -> str:
        parts = [html_to_text(item.get("description")), html_to_text(item.get("requirements"))]
        return "\n\n".join(p for p in parts if p).strip()

    def fetch(self) -> Iterator[JobPosting]:
        for company in self.config.sources.recruitee.companies:
            try:
                data = self.client.get_json(self.LIST_URL.format(company=company))
            except Exception as exc:
                log.warning("recruitee %r unavailable: %s", company, exc)
                continue
            offers = (data or {}).get("offers") or []
            for item in offers:
                title = item.get("title") or ""
                if not title_prefilter(self.config, title):
                    continue
                location = item.get("location") or ", ".join(
                    p for p in (item.get("city"), item.get("country")) if p
                )
                url = item.get("careers_url") or item.get("careers_apply_url") or ""
                yield JobPosting(
                    source=self.name,
                    ats=self.ats,
                    external_id=f"{company}:{item.get('id')}",
                    company=item.get("company_name") or _titleize(company),
                    title=title,
                    location=location or None,
                    remote=bool(item.get("remote")) or looks_remote(location, title),
                    url=url,
                    apply_url=item.get("careers_apply_url") or url,
                    description=self._description(item),
                    posted_at=parse_iso(item.get("published_at") or item.get("created_at")),
                    extra={"company_slug": company, "department": item.get("department")},
                )
            log.info("recruitee:%s scanned", company)


class RemotiveSource(Source):
    """Remote-first aggregator with a free public API and generous terms."""

    name = "remotive"
    ats = None
    LIST_URL = "https://remotive.com/api/remote-jobs?search={query}&limit=60"

    def fetch(self) -> Iterator[JobPosting]:
        for query in self.config.sources.remotive.queries:
            try:
                data = self.client.get_json(self.LIST_URL.format(query=query))
            except Exception as exc:
                log.warning("remotive query %r failed: %s", query, exc)
                continue
            for item in (data or {}).get("jobs", []):
                title = item.get("title") or ""
                if not title_prefilter(self.config, title):
                    continue
                location = item.get("candidate_required_location")
                yield JobPosting(
                    source=self.name,
                    ats=None,
                    external_id=str(item.get("id")),
                    company=item.get("company_name") or "Unknown",
                    title=title,
                    location=location,
                    remote=True,
                    url=item.get("url") or "",
                    apply_url=item.get("url"),
                    description=html_to_text(item.get("description")),
                    posted_at=parse_iso(item.get("publication_date")),
                    extra={"query": query, "job_type": item.get("job_type")},
                )
            log.info("remotive:%s scanned", query)


ATS_SOURCES: list[type[Source]] = [
    GreenhouseSource,
    LeverSource,
    AshbySource,
    SmartRecruitersSource,
    WorkableSource,
    RecruiteeSource,
    RemotiveSource,
]
