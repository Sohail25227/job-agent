"""Aggregators with real India coverage, steered by keyword and city.

Adzuna and Jooble both index Naukri, Indeed, Shine, TimesJobs and company career
pages, and both hand out a free API key on signup. That makes them the honest
answer to "cover every platform" for the Indian market: instead of scraping
sites that forbid it, read the aggregator that already licensed the data. Both
return a short snippet rather than the full JD, so scoring leans on the title.

The Muse and JobDataAPI need no key at all and, unlike the two above, ship the
*whole* description in the list response - which is the expensive part
everywhere else. That makes them unusually good value: one request yields
postings the scorer can judge properly.

They disagree sharply on how to treat a city they do not recognise, and that
difference decides how each is used:

* JobDataAPI returns zero, so an unknown city is self-correcting and its own
  canonical spelling can be passed straight through.
* The Muse silently ignores the filter and answers with a default set instead.
  A guessed label therefore looks like a working source while quietly filling
  the cache with the wrong city, so only labels verified against the live API
  are ever sent - see ``MUSE_CITIES``.
"""

from __future__ import annotations

import json
import logging
from typing import Iterator
from urllib.parse import quote_plus

from ..matching.filters import title_prefilter
from .base import JobPosting, Source, html_to_text, looks_remote, parse_iso

log = logging.getLogger(__name__)


class AdzunaSource(Source):
    """https://developer.adzuna.com - free tier, 250 calls/day, ``in`` = India."""

    name = "adzuna"
    ats = None
    LIST_URL = (
        "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
        "?app_id={app_id}&app_key={app_key}&results_per_page={per_page}"
        "&what={what}&content-type=application/json&max_days_old={max_days}"
        "&sort_by=date"
    )

    def fetch(self) -> Iterator[JobPosting]:
        settings = self.config.sources.adzuna
        if not settings.credentials_present:
            log.warning(
                "adzuna is enabled but ADZUNA_APP_ID/ADZUNA_APP_KEY are unset; "
                "get a free key at https://developer.adzuna.com and add them to .env"
            )
            return
        max_days = max(1, self.config.search.posted_within_days)
        for query in settings.queries:
            what = query.get("what") or query.get("keywords") or ""
            where = query.get("where") or ""
            for page in range(1, max(1, settings.max_pages) + 1):
                url = self.LIST_URL.format(
                    country=settings.country,
                    page=page,
                    app_id=settings.app_id,
                    app_key=settings.api_key,
                    per_page=min(50, settings.results_per_page),
                    what=quote_plus(what),
                    max_days=max_days,
                )
                if where:
                    url += f"&where={quote_plus(where)}"
                try:
                    data = self.client.get_json(url)
                except Exception as exc:
                    log.warning("adzuna %r/%r page %d failed: %s", what, where, page, exc)
                    break
                results = (data or {}).get("results") or []
                if not results:
                    break
                for item in results:
                    title = item.get("title") or ""
                    if not title_prefilter(self.config, title):
                        continue
                    location = (item.get("location") or {}).get("display_name")
                    link = item.get("redirect_url") or ""
                    yield JobPosting(
                        source=self.name,
                        ats=None,
                        external_id=str(item.get("id")),
                        company=(item.get("company") or {}).get("display_name") or "Unknown",
                        title=html_to_text(title) or title,
                        location=location,
                        remote=looks_remote(location, title, item.get("description")),
                        url=link,
                        apply_url=link,
                        description=html_to_text(item.get("description")),
                        posted_at=parse_iso(item.get("created")),
                        extra={"query": what, "where": where, "snippet_only": True},
                    )
                log.info("adzuna:%s/%s page %d scanned", what or "*", where or "IN", page)


class JoobleSource(Source):
    """https://jooble.org/api/about - free key, POST-based search, India-aware."""

    name = "jooble"
    ats = None
    SEARCH_URL = "https://jooble.org/api/{key}"

    def fetch(self) -> Iterator[JobPosting]:
        settings = self.config.sources.jooble
        if not settings.credentials_present:
            log.warning(
                "jooble is enabled but JOOBLE_API_KEY is unset; "
                "request a free key at https://jooble.org/api/about"
            )
            return
        url = self.SEARCH_URL.format(key=settings.api_key)
        for query in settings.queries:
            keywords = query.get("keywords") or query.get("what") or ""
            location = query.get("location") or query.get("where") or ""
            for page in range(1, max(1, settings.max_pages) + 1):
                payload = {"keywords": keywords, "location": location, "page": str(page)}
                try:
                    data = self.client.post_json(
                        url, json=payload, headers={"Content-Type": "application/json"}
                    )
                except Exception as exc:
                    log.warning("jooble %r/%r page %d failed: %s", keywords, location, page, exc)
                    break
                jobs = (data or {}).get("jobs") or []
                if not jobs:
                    break
                for item in jobs:
                    title = item.get("title") or ""
                    if not title_prefilter(self.config, title):
                        continue
                    job_location = item.get("location")
                    link = item.get("link") or ""
                    yield JobPosting(
                        source=self.name,
                        ats=None,
                        external_id=str(item.get("id") or link)[:180],
                        company=item.get("company") or "Unknown",
                        title=title,
                        location=job_location,
                        remote=looks_remote(job_location, title, item.get("snippet")),
                        url=link,
                        apply_url=link,
                        description=html_to_text(item.get("snippet")),
                        posted_at=parse_iso(item.get("updated")),
                        extra={
                            "query": keywords,
                            "board": item.get("source"),
                            "snippet_only": True,
                        },
                    )
                log.info("jooble:%s/%s page %d scanned", keywords or "*", location or "IN", page)


class TheMuseSource(Source):
    """https://www.themuse.com/developers/api/v2 - keyless, full JD inline."""

    name = "themuse"
    ats = None
    LIST_URL = "https://www.themuse.com/api/public/jobs?page={page}&category={category}"
    DEFAULT_CATEGORIES = ["Software Engineering", "Data Science", "IT"]

    def fetch(self) -> Iterator[JobPosting]:
        settings = self.config.sources.themuse
        categories = settings.filters or self.DEFAULT_CATEGORIES
        locations = [
            q["location"] for q in settings.queries if q.get("location")
        ]
        if not locations:
            # Every remote label this API offers is one it ignores, so a search
            # with no city has nothing to ask it for.
            log.info("themuse skipped: no recognised city in this search")
            return

        for location in locations:
            for category in categories:
                for page in range(1, max(1, settings.max_pages) + 1):
                    url = self.LIST_URL.format(page=page, category=quote_plus(category))
                    url += f"&location={quote_plus(location)}"
                    try:
                        data = self._get(url)
                    except Exception as exc:
                        log.warning("themuse %r/%r page %d failed: %s",
                                    location, category, page, exc)
                        break
                    results = (data or {}).get("results") or []
                    if not results:
                        break
                    yield from self._postings(results, location, category)
                    log.info("themuse:%s/%s page %d scanned", location, category, page)

    def _get(self, url: str) -> dict:
        """The Muse embeds raw control characters in job HTML.

        ``response.json()`` is strict and rejects the whole payload over one
        stray character in one posting, so parse leniently instead of losing
        every job on the page.
        """
        return json.loads(self.client.get_text(url), strict=False)

    def _postings(self, results: list, location: str, category: str) -> Iterator[JobPosting]:
        for item in results:
            title = item.get("name") or ""
            if not title_prefilter(self.config, title):
                continue
            names = [
                place.get("name") for place in (item.get("locations") or [])
                if place.get("name")
            ]
            # The posting's own locations, not the one that was asked for: this
            # API returns multi-city roles under any of their cities, and the
            # scorer has to see where the job actually is.
            job_location = ", ".join(names) or None
            link = (item.get("refs") or {}).get("landing_page") or ""
            yield JobPosting(
                source=self.name,
                ats=None,
                external_id=str(item.get("id")),
                company=(item.get("company") or {}).get("name") or "Unknown",
                title=title,
                location=job_location,
                remote=looks_remote(job_location),
                url=link,
                apply_url=link,
                description=html_to_text(item.get("contents")),
                posted_at=parse_iso(item.get("publication_date")),
                extra={
                    "category": category,
                    "asked_for": location,
                    "levels": [lv.get("name") for lv in (item.get("levels") or [])],
                },
            )


class JobDataApiSource(Source):
    """https://jobdataapi.com - keyless, India-wide, full JD inline.

    Generous with data and stingy with requests. Without an API key - and keys
    are sold, not given - the documented budget is about ten requests an hour,
    a page is capped at twenty postings, and page two is refused outright. So
    one query is one request is at most twenty jobs, and ``MAX_QUERIES`` keeps
    a run inside the hourly allowance no matter how many saved searches the
    crawl merged together.

    Exceeding it costs far more than the extra page: the throttle is per client
    and lasts the best part of an hour, taking the scheduled crawl down with
    it. That is also why this source never runs on the live path.
    """

    name = "jobdataapi"
    ats = None
    LIST_URL = (
        "https://jobdataapi.com/api/jobs/?country_code={country}"
        "&max_age={max_days}&page={page}"
    )
    COUNTRY = "IN"
    MAX_QUERIES = 5

    def fetch(self) -> Iterator[JobPosting]:
        settings = self.config.sources.jobdataapi
        max_days = max(1, self.config.search.posted_within_days)
        queries = settings.queries[: self.MAX_QUERIES]
        if len(settings.queries) > self.MAX_QUERIES:
            log.info(
                "jobdataapi: using %d of %d queries to stay inside the hourly limit",
                len(queries), len(settings.queries),
            )
        for query in queries:
            title = query.get("title") or ""
            location = query.get("location") or ""
            for page in range(1, max(1, settings.max_pages) + 1):
                url = self.LIST_URL.format(
                    country=self.COUNTRY, max_days=max_days, page=page
                )
                if title:
                    url += f"&title={quote_plus(title)}"
                if location:
                    url += f"&location={quote_plus(location)}"
                try:
                    data = self.client.get_json(url)
                except Exception as exc:
                    log.warning("jobdataapi %r/%r page %d failed: %s",
                                title or "*", location or "IN", page, exc)
                    if "429" in str(exc):
                        log.warning("jobdataapi rate limited; skipping its remaining queries")
                        return
                    break
                results = (data or {}).get("results") or []
                if not results:
                    break
                yield from self._postings(results, title, location)
                log.info("jobdataapi:%s/%s page %d scanned",
                         title or "*", location or "IN", page)
                if not (data or {}).get("next"):
                    break

    def _postings(self, results: list, title: str, location: str) -> Iterator[JobPosting]:
        for item in results:
            job_title = item.get("title") or ""
            if not title_prefilter(self.config, job_title):
                continue
            job_location = item.get("location")
            link = item.get("application_url") or ""
            yield JobPosting(
                source=self.name,
                ats=None,
                external_id=str(item.get("id")),
                company=(item.get("company") or {}).get("name") or "Unknown",
                title=job_title,
                location=job_location,
                remote=bool(item.get("has_remote")) or looks_remote(job_location),
                url=link,
                apply_url=link,
                description=html_to_text(item.get("description")),
                posted_at=parse_iso(item.get("published")),
                extra={
                    "query": title,
                    "asked_for": location,
                    "experience_level": item.get("experience_level"),
                },
            )


AGGREGATOR_SOURCES: list[type[Source]] = [
    AdzunaSource,
    JoobleSource,
    TheMuseSource,
    JobDataApiSource,
]
