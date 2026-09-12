"""Fetching openings into the shared cache.

Two paths, because one shape cannot serve both:

* :func:`live` runs while a user waits. It only asks the sources that can be
  steered by a keyword, runs them concurrently, and never opens a posting to
  read its full description - a per-posting detail request is what turned a
  LinkedIn query from two seconds into a minute. Skills are matched against the
  title and whatever snippet the list endpoint returned.
* :func:`crawl` runs on a schedule with nobody waiting. It adds the boards
  addressed by company slug, pages deeper, and backfills the descriptions the
  live path skipped, so the cache keeps getting richer between searches.

Both write into the same ``jobs`` table, so a search by one user warms the cache
for every user.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from sqlalchemy import select

from .config import Config, load_config
from .db import init_db, session_scope, upsert_job
from .http import HttpClient
from .models import Job, RunLog, utcnow
from .search import COMPANY_ADDRESSED, CRAWL_ONLY, build_search_config
from .sources import SOURCES_BY_NAME, enabled_sources

log = logging.getLogger(__name__)

# Wide enough to cover every live source at once - the work is all waiting on
# other people's servers, and per-host politeness still serialises each host.
LIVE_WORKERS = 8
CRAWL_WORKERS = 6

# A hard ceiling on the interactive path. Whatever has arrived by then is what
# the user sees; the rest lands in the cache on the next crawl.
LIVE_BUDGET_SECONDS = 45

# Per-request limits for the interactive path only. A source that has not
# answered in this long is not going to answer usefully before the budget above
# runs out, and every second it holds is a second the other nine cannot use.
LIVE_HTTP_TIMEOUT = 8
LIVE_HTTP_RETRIES = 1


@dataclass
class FetchStats:
    discovered: int = 0
    new: int = 0
    seconds: float = 0.0
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: dict[str, str] = field(default_factory=dict)
    timed_out: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def _live_sources(config: Config) -> list[str]:
    """Keyword-steerable sources only: the rest cannot answer this search.

    Sources whose request budget is too small to spend on one impatient user
    are left to the crawl as well, even though a keyword would steer them.
    """
    names = []
    for cls in enabled_sources(config):
        if cls.name in COMPANY_ADDRESSED or cls.name in CRAWL_ONLY:
            continue
        names.append(cls.name)
    return names


def live_config(base: Config, search, skills: dict[str, float] | None = None) -> Config:
    """A config tuned for a user who is watching a spinner.

    The description budgets go to zero and paging is shallow. That is the whole
    trick behind an interactive search: fetching the full text of every card is
    ten to twenty times the requests, for detail that only matters once the user
    opens the posting.

    The HTTP timeout comes down hard too. The crawl can afford to wait 30s for a
    struggling host, but here the whole search has 45s, so one slow source would
    spend two thirds of the budget and still likely return nothing. Waiting
    ``LIVE_HTTP_TIMEOUT`` and moving on costs only that source's results, and
    the crawl picks it up later anyway.
    """
    config, _ = build_search_config(base, search, skills)
    config.sources.linkedin.descriptions_per_query = 0
    config.sources.linkedin.max_pages = min(config.sources.linkedin.max_pages, 2)
    config.sources.workday.descriptions_per_tenant = 0
    config.http.timeout_seconds = min(config.http.timeout_seconds, LIVE_HTTP_TIMEOUT)
    config.http.max_retries = min(config.http.max_retries, LIVE_HTTP_RETRIES)
    return config


def _run_source(name: str, config: Config, client: HttpClient) -> list:
    cls = SOURCES_BY_NAME[name]
    return list(cls(config, client).fetch())


def _gather(config: Config, names: list[str], *, workers: int,
            budget: float | None, on_progress=None) -> tuple[list, dict[str, str], bool]:
    """Run sources in parallel, returning postings plus per-source failures.

    One source failing or hanging must never lose the results of the others, so
    each is isolated and the whole fan-out is bounded by ``budget``.
    """
    postings: list = []
    failures: dict[str, str] = {}
    timed_out = False
    deadline = time.monotonic() + budget if budget else None

    with HttpClient(config) as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_source, name, config, client): name for name in names}
            for future in as_completed(futures):
                name = futures[future]
                remaining = deadline - time.monotonic() if deadline else None
                if remaining is not None and remaining <= 0:
                    timed_out = True
                    break
                try:
                    found = future.result(timeout=remaining)
                except TimeoutError:
                    timed_out = True
                    failures[name] = "timed out"
                    break
                except Exception as exc:
                    failures[name] = str(exc)[:200]
                    log.warning("source %s failed: %s", name, exc)
                    continue
                postings.extend(found)
                if on_progress:
                    on_progress(f"{name}: {len(found)} found")
            if timed_out:
                # Nothing left to wait for; the threads are daemonic HTTP waits
                # and their results are no longer wanted.
                pool.shutdown(wait=False, cancel_futures=True)

    return postings, failures, timed_out


def _persist(config: Config, postings: list) -> tuple[int, int]:
    """Write postings from a single thread, so there is one writer at a time."""
    discovered = new = 0
    with session_scope(config) as session:
        for posting in postings:
            _job, created = upsert_job(session, posting)
            discovered += 1
            new += 1 if created else 0
    return discovered, new


def live(config: Config, *, user_id: int | None = None, search_id: int | None = None,
         on_progress=None) -> FetchStats:
    """Fetch what the sources list right now for one search, while a user waits."""
    init_db(config)
    started = time.monotonic()
    names = _live_sources(config)
    if on_progress:
        on_progress(f"asking {len(names)} sources")

    postings, failures, timed_out = _gather(
        config, names, workers=LIVE_WORKERS,
        budget=LIVE_BUDGET_SECONDS, on_progress=on_progress,
    )
    discovered, new = _persist(config, postings)

    stats = FetchStats(
        discovered=discovered,
        new=new,
        seconds=round(time.monotonic() - started, 1),
        sources_ok=[n for n in names if n not in failures],
        sources_failed=failures,
        timed_out=timed_out,
    )
    _log_run(config, stats, trigger="live", user_id=user_id, search_id=search_id)
    log.info("live fetch: %s", stats.as_dict())
    return stats


def crawl_config(base: Config) -> Config:
    """A config covering the union of every user's active searches.

    The company-addressed boards need no query, but LinkedIn and the
    aggregators do, and the only queries worth spending on are the ones some
    user is actually waiting for. So the crawl serves the union of saved
    searches; a cache warmed for one user is warm for the next.
    """
    from .models import Search

    with session_scope(base) as session:
        searches = list(
            session.scalars(select(Search).where(Search.active.is_(True))).all()
        )
    if not searches:
        return base

    merged = build_search_config(base, searches[0])[0]
    seen_queries = {
        "linkedin": list(merged.sources.linkedin.queries),
        "adzuna": list(merged.sources.adzuna.queries),
        "jooble": list(merged.sources.jooble.queries),
        "themuse": list(merged.sources.themuse.queries),
        "jobdataapi": list(merged.sources.jobdataapi.queries),
    }
    workday = list(merged.sources.workday.queries)

    for search in searches[1:]:
        scoped = build_search_config(base, search)[0]
        for name in seen_queries:
            for query in getattr(scoped.sources, name).queries:
                if query not in seen_queries[name]:
                    seen_queries[name].append(query)
        for query in scoped.sources.workday.queries:
            if query not in workday:
                workday.append(query)

    # The crawl has no user waiting, so the per-search keyword and location
    # gates must not be applied here - they belong to whoever reads the cache.
    merged.search.must_have_any = base.search.must_have_any
    merged.search.exclude_title_keywords = base.search.exclude_title_keywords
    merged.search.locations = base.search.locations

    for name, queries in seen_queries.items():
        getattr(merged.sources, name).queries = queries
    merged.sources.workday.queries = workday
    log.info(
        "crawl plan from %d searches: %s",
        len(searches),
        ", ".join(f"{len(queries)} {name}" for name, queries in seen_queries.items()),
    )
    return merged


def crawl(config: Config | None = None, *, only: list[str] | None = None,
          backfill: int = 60) -> FetchStats:
    """The scheduled pass: every source, deeper paging, descriptions filled in."""
    config = crawl_config(config or load_config())
    init_db(config)
    started = time.monotonic()

    names = only or [cls.name for cls in enabled_sources(config)]
    postings, failures, _ = _gather(config, names, workers=CRAWL_WORKERS, budget=None)
    discovered, new = _persist(config, postings)

    filled = backfill_descriptions(config, limit=backfill) if backfill else 0

    stats = FetchStats(
        discovered=discovered,
        new=new,
        seconds=round(time.monotonic() - started, 1),
        sources_ok=[n for n in names if n not in failures],
        sources_failed=failures,
    )
    stats_dict = stats.as_dict() | {"descriptions_filled": filled}
    _log_run(config, stats, trigger="crawl", extra={"descriptions_filled": filled})
    log.info("crawl: %s", stats_dict)
    return stats


def backfill_descriptions(config: Config, *, limit: int = 60) -> int:
    """Fetch full text for recent postings the live path left description-less.

    Scoring is much sharper with the real JD, so this is where the cache earns
    its keep between searches.
    """
    from .sources.discovery import LinkedInSource

    with session_scope(config) as session:
        pending = list(
            session.scalars(
                select(Job)
                .where(
                    Job.source == "linkedin",
                    (Job.description.is_(None)) | (Job.description == ""),
                    Job.last_seen_at >= utcnow() - timedelta(days=7),
                )
                .order_by(Job.last_seen_at.desc())
                .limit(limit)
            ).all()
        )
        targets = [(job.id, job.external_id) for job in pending]

    if not targets:
        return 0

    filled = 0
    with HttpClient(config) as client:
        source = LinkedInSource(config, client)
        for job_id, external_id in targets:
            try:
                text = source._description(external_id)
            except Exception as exc:
                log.debug("backfill %s failed: %s", external_id, exc)
                continue
            if not text:
                continue
            with session_scope(config) as session:
                job = session.get(Job, job_id)
                if job is not None:
                    job.description = text
                    filled += 1
    log.info("backfilled %d descriptions", filled)
    return filled


def _log_run(config: Config, stats: FetchStats, *, trigger: str,
             user_id: int | None = None, search_id: int | None = None,
             extra: dict | None = None) -> None:
    with session_scope(config) as session:
        session.add(
            RunLog(
                started_at=utcnow(),
                finished_at=utcnow(),
                trigger=trigger,
                user_id=user_id,
                search_id=search_id,
                stats=stats.as_dict() | (extra or {}),
                error="; ".join(f"{k}: {v}" for k, v in stats.sources_failed.items()) or None,
            )
        )
