"""Ranking the shared job cache for one user, at read time.

Scores are not stored. "Is this a good job" is a question about a person, so the
answer belongs to the request, not to the row: two users looking at the same
posting should see different numbers, and editing your resume should re-rank
everything without a rescoring pass that can go stale.

The cost of that is scoring on every page view, so the candidate set is narrowed
in SQL first (city, freshness, title relevance) and only that slice is scored in
Python - where the full description is available for skill matching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import Config, load_config
from .matching.scorer import ScoreResult, score_job
from .models import Job, Search, User, UserJob, utcnow
from .search import build_search_config

log = logging.getLogger(__name__)

# How much of the cache one request is willing to score. Ordered by most
# recently seen, so the cap drops the stalest rows rather than a random slice.
MAX_CANDIDATES = 1500


@dataclass(slots=True)
class Ranked:
    job: Job
    result: ScoreResult
    status: str | None = None
    notes: str | None = None

    @property
    def score(self) -> int:
        return int(round(self.result.total))


def _title_clauses(config: Config):
    """Cheap SQL relevance gate on the title.

    Deliberately looser than the scorer: the expanded title family already
    includes generic phrasings like "Software Engineer", so a posting whose
    title says nothing specific still gets through to be judged on its
    description.
    """
    terms = {t.lower() for t in config.search.titles}
    terms |= {k.lower() for k in config.search.must_have_any}
    return [Job.title.ilike(f"%{term}%") for term in terms if len(term) > 2]


def _location_clauses(config: Config):
    clauses = [Job.location.ilike(f"%{city}%") for city in config.search.locations]
    if config.search.remote_ok:
        clauses.append(Job.remote.is_(True))
        clauses.append(Job.location.ilike("%remote%"))
    # A posting with no location at all is ambiguous, not irrelevant; the scorer
    # decides on the rest of its content.
    clauses.append(Job.location.is_(None))
    return clauses


def candidates(session: Session, config: Config, *, fresh_days: int) -> list[Job]:
    query = select(Job)
    if fresh_days:
        query = query.where(Job.last_seen_at >= utcnow() - timedelta(days=fresh_days))
    titles = _title_clauses(config)
    if titles:
        query = query.where(or_(*titles))
    locations = _location_clauses(config)
    if locations:
        query = query.where(or_(*locations))
    return list(
        session.scalars(
            query.order_by(Job.last_seen_at.desc()).limit(MAX_CANDIDATES)
        ).all()
    )


def rank(session: Session, user: User, search: Search | None,
         base: Config | None = None, *, include_filtered: bool = False,
         fresh_days: int | None = None) -> list[Ranked]:
    """Every opening worth showing this user for this search, best first."""
    base = base or load_config()
    if search is None:
        return []

    config, _ = build_search_config(base, search, user.skills or {})
    fresh = base.search.posted_within_days if fresh_days is None else fresh_days
    jobs = candidates(session, config, fresh_days=fresh)

    decisions = {
        row.job_id: row
        for row in session.scalars(
            select(UserJob).where(UserJob.user_id == user.id)
        ).all()
    }

    ranked: list[Ranked] = []
    for job in jobs:
        decision = decisions.get(job.id)
        if decision and decision.status == "hidden" and not include_filtered:
            continue
        result = score_job(config, job)
        if result.rejected_reason and not include_filtered:
            continue
        if not result.rejected_reason and result.total < base.search.min_score:
            if not include_filtered:
                continue
        ranked.append(
            Ranked(
                job=job,
                result=result,
                status=decision.status if decision else None,
                notes=decision.notes if decision else None,
            )
        )

    ranked.sort(key=lambda r: (r.result.total, r.job.last_seen_at), reverse=True)
    log.debug("ranked %d of %d candidates for user %s", len(ranked), len(jobs), user.id)
    return ranked


def score_one(session: Session, user: User, search: Search | None, job: Job,
              base: Config | None = None) -> Ranked:
    """The same scoring path for a single job, so a detail page cannot disagree."""
    base = base or load_config()
    config = (
        build_search_config(base, search, user.skills or {})[0]
        if search is not None
        else base
    )
    decision = session.scalar(
        select(UserJob).where(UserJob.user_id == user.id, UserJob.job_id == job.id)
    )
    return Ranked(
        job=job,
        result=score_job(config, job),
        status=decision.status if decision else None,
        notes=decision.notes if decision else None,
    )
