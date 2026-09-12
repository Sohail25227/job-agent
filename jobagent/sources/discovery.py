"""Discovery-only sources for LinkedIn and Naukri.

READ THIS BEFORE ENABLING EITHER OF THESE.

Both platforms' Terms of Service prohibit automated access. These adapters
exist because an opening you never see is an opening you cannot apply to, and
they are deliberately limited:

  * read-only - they only look at publicly reachable search results
  * no login - no session cookie, no credentials, no account risk from auth
  * read-only - nothing is ever posted, applied to or saved on their side
  * conservative paging and the global politeness delay from HttpClient

Treat what they return as leads: you open the link and apply yourself, or find
the same role on the company's own ATS, which usually exists. Both use
unofficial endpoints and will break without notice; failures are logged and
never abort a run. Enabling them is your decision and your risk.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterator
from urllib.parse import quote_plus

from ..matching.filters import location_intent, title_prefilter
from .base import JobPosting, Source, html_to_text, looks_remote, parse_iso

log = logging.getLogger(__name__)

TOS_WARNING = (
    "%s discovery is enabled. This uses an unofficial endpoint and is against "
    "the platform's Terms of Service; it is read-only and never auto-applies. "
    "Disable it in config.yaml if you are not comfortable with that."
)


class LinkedInSource(Source):
    name = "linkedin"
    ats = None  # never auto-applied to
    SEARCH_URL = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        "?keywords={keywords}&location={location}&start={start}"
    )
    DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    # The guest endpoint returns 10 cards per request regardless of what you
    # ask for, and pages cleanly on ``start``. Assuming 25 here meant every
    # query stopped after its first page and skipped 15 results per step.
    PAGE_SIZE = 10

    CARD_SPLIT = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
    TITLE_RE = re.compile(r'base-search-card__title"[^>]*>\s*(.*?)\s*<', re.S)
    COMPANY_RE = re.compile(r'base-search-card__subtitle"[^>]*>.*?<a[^>]*>\s*(.*?)\s*</a>', re.S)
    LOCATION_RE = re.compile(r'job-search-card__location"[^>]*>\s*(.*?)\s*<', re.S)
    DATE_RE = re.compile(r'datetime="(\d{4}-\d{2}-\d{2})"')
    DESC_RE = re.compile(
        r'show-more-less-html__markup[^>]*>(.*?)</div>', re.S
    )

    def _description(self, job_id: str) -> str:
        try:
            html_text = self.client.get_text(self.DETAIL_URL.format(job_id=job_id))
        except Exception as exc:
            log.debug("linkedin detail %s failed: %s", job_id, exc)
            return ""
        match = self.DESC_RE.search(html_text)
        return html_to_text(match.group(1)) if match else ""

    def fetch(self) -> Iterator[JobPosting]:
        source_config = self.config.sources.linkedin
        if not source_config.queries:
            return
        log.warning(TOS_WARNING, "LinkedIn")

        seen: set[str] = set()
        for query in source_config.queries:
            keywords = query.get("keywords", "")
            location = query.get("location", "")
            budget = source_config.descriptions_per_query
            found = 0
            start = 0
            for _ in range(source_config.max_pages):
                url = self.SEARCH_URL.format(
                    keywords=quote_plus(keywords),
                    location=quote_plus(location),
                    start=start,
                )
                try:
                    html_text = self.client.get_text(url)
                except Exception as exc:
                    log.warning("linkedin search %r start %d failed: %s", keywords, start, exc)
                    break

                chunks = self.CARD_SPLIT.split(html_text)
                if len(chunks) < 3:
                    break  # no cards: end of results or markup changed

                # split() gives [pre, id, body, id, body, ...]
                pairs = list(zip(chunks[1::2], chunks[2::2]))
                # Advance by what the page actually held, not by an assumed size.
                start += len(pairs)

                fresh = 0
                for job_id, body in pairs:
                    if job_id in seen:
                        continue  # the same role surfaces under several keywords
                    seen.add(job_id)
                    fresh += 1
                    title_match = self.TITLE_RE.search(body)
                    title = html_to_text(title_match.group(1)) if title_match else ""
                    if not title_prefilter(self.config, title):
                        continue
                    company_match = self.COMPANY_RE.search(body)
                    location_match = self.LOCATION_RE.search(body)
                    date_match = self.DATE_RE.search(body)
                    job_location = html_to_text(location_match.group(1)) if location_match else None
                    if location_intent(self.config, job_location) == "foreign":
                        continue
                    description = ""
                    if budget > 0:
                        description = self._description(job_id)
                        budget -= 1
                    yield JobPosting(
                        source=self.name,
                        ats=None,
                        external_id=job_id,
                        company=html_to_text(company_match.group(1)) if company_match else "Unknown",
                        title=title,
                        location=job_location,
                        remote=looks_remote(job_location, title),
                        url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                        apply_url=None,  # apply is manual by design
                        description=description,
                        posted_at=parse_iso(date_match.group(1)) if date_match else None,
                        extra={"query": keywords, "discovery_only": True},
                    )
                    found += 1
                if not fresh:
                    break  # everything on this page was already seen
            log.info("linkedin:%r scanned (%d kept)", keywords, found)


RELATIVE_AGE = re.compile(r"(\d+)\s*(day|days|hour|hours|week|weeks|month|months)", re.I)


def _age_to_datetime(label: str | None) -> datetime | None:
    """Turn Naukri's '3 Days Ago' style label into a timestamp."""
    if not label:
        return None
    match = RELATIVE_AGE.search(label)
    if not match:
        if re.search(r"just now|today|few hours", label, re.I):
            return datetime.now(timezone.utc)
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    days = {"hour": 0, "hours": 0, "day": 1, "days": 1,
            "week": 7, "weeks": 7, "month": 30, "months": 30}[unit]
    return datetime.now(timezone.utc) - timedelta(days=amount * days)


class NaukriSource(Source):
    name = "naukri"
    ats = None  # never auto-applied to
    SEARCH_URL = (
        "https://www.naukri.com/jobapi/v3/search?noOfResults=20&urlType=search_by_keyword"
        "&searchType=adv&keyword={keywords}&location={location}&pageNo={page}"
    )
    HEADERS = {
        "appid": "109",
        "systemid": "jobsearch",
        "Accept": "application/json",
        "Referer": "https://www.naukri.com/",
    }

    def fetch(self) -> Iterator[JobPosting]:
        source_config = self.config.sources.naukri
        if not source_config.queries:
            return
        log.warning(TOS_WARNING, "Naukri")

        for query in source_config.queries:
            keywords = query.get("keywords", "")
            location = query.get("location", "")
            for page in range(1, source_config.max_pages + 1):
                url = self.SEARCH_URL.format(
                    keywords=quote_plus(keywords),
                    location=quote_plus(location),
                    page=page,
                )
                try:
                    data = self.client.get_json(url, headers=self.HEADERS)
                except Exception as exc:
                    log.warning("naukri search %r page %d failed: %s", keywords, page, exc)
                    break
                details = (data or {}).get("jobDetails") or []
                if not details:
                    break
                for item in details:
                    title = item.get("title") or ""
                    if not title_prefilter(self.config, title):
                        continue
                    placeholders = {
                        p.get("type"): p.get("label") for p in item.get("placeholders") or []
                    }
                    job_location = placeholders.get("location")
                    description = html_to_text(item.get("jobDescription"))
                    skills = item.get("tagsAndSkills") or ""
                    posted = parse_iso(item.get("createdDate")) or _age_to_datetime(
                        item.get("footerPlaceholderLabel")
                    )
                    job_url = item.get("jdURL") or item.get("staticUrl") or ""
                    if job_url and not job_url.startswith("http"):
                        job_url = f"https://www.naukri.com{job_url}"
                    yield JobPosting(
                        source=self.name,
                        ats=None,
                        external_id=str(item.get("jobId")),
                        company=item.get("companyName") or "Unknown",
                        title=title,
                        location=job_location,
                        remote=looks_remote(job_location, title),
                        url=job_url,
                        apply_url=None,  # apply is manual by design
                        description=f"{description}\n\nSkills: {skills}".strip(),
                        posted_at=posted,
                        extra={
                            "query": keywords,
                            "experience": placeholders.get("experience"),
                            "salary": placeholders.get("salary"),
                            "discovery_only": True,
                        },
                    )
            log.info("naukri:%r scanned", keywords)


DISCOVERY_SOURCES: list[type[Source]] = [LinkedInSource, NaukriSource]
