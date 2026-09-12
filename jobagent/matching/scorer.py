"""Match scoring.

Produces a 0-100 fit score with a breakdown, so a surprising score can always
be explained (and the config tuned) instead of just distrusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from rapidfuzz import fuzz

import re

from ..config import Config
from .filters import hard_filter_reason, in_target_geography, remote_is_eligible
from .skills import extract_skills, parse_required_experience, seniority_from_title

CORE_WEIGHT_THRESHOLD = 0.9

# Titles that actually describe backend software work.
SOFTWARE_TOKEN = re.compile(
    r"\b(software|backend|back-end|back end|server-?side|full-?stack|java|spring"
    r"|micro-?services?|api|platform|infrastructure|distributed|cloud|data|sde|swe"
    r"|developer|development|programmer)\b",
    re.I,
)


@dataclass(slots=True)
class ScoreResult:
    total: float
    detail: dict[str, float] = field(default_factory=dict)
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    jd_skills: list[str] = field(default_factory=list)
    required_experience: tuple[int | None, int | None] = (None, None)
    rejected_reason: str | None = None


def _skills_component(config: Config, jd_skills: set[str]) -> tuple[float, list[str], list[str]]:
    my_skills = {k.lower(): v for k, v in config.matching.my_skills.items()}
    matched = sorted(s for s in jd_skills if s in my_skills)
    missing = sorted(s for s in jd_skills if s not in my_skills)

    if not jd_skills:
        # No extractable skills usually means a thin JD, not a bad one.
        return 0.5, matched, missing

    coverage = sum(my_skills[s] for s in matched) / len(jd_skills)

    core = [s for s, w in my_skills.items() if w >= CORE_WEIGHT_THRESHOLD]
    core_presence = (
        sum(1 for s in core if s in jd_skills) / len(core) if core else 0.0
    )
    return 0.7 * coverage + 0.3 * core_presence, matched, missing


def _title_component(config: Config, title: str) -> float:
    if not title or not config.search.titles:
        return 0.5
    best = max(fuzz.partial_ratio(t.lower(), title.lower()) for t in config.search.titles)
    ratio = best / 100.0
    # partial_ratio is generous: "Electrical Engineer" scores well against
    # "Software Engineer" on the shared word alone. Require the title to name
    # software work before trusting that similarity.
    if not SOFTWARE_TOKEN.search(title):
        ratio *= 0.35
    return ratio


def _experience_component(config: Config, title: str, description: str | None) -> tuple[float, tuple]:
    low, high = parse_required_experience(description)
    band_low = config.search.min_experience_years
    band_high = config.search.max_experience_years

    if low is None:
        implied = seniority_from_title(title)
        if band_low <= implied <= band_high:
            return 0.85, (None, None)
        distance = min(abs(implied - band_low), abs(implied - band_high))
        return max(0.0, 0.85 - 0.2 * distance), (None, None)

    effective_high = high if high is not None else low + 3
    overlaps = low <= band_high and effective_high >= band_low
    if overlaps:
        return 1.0, (low, high)
    distance = low - band_high if low > band_high else band_low - effective_high
    return max(0.0, 1.0 - 0.25 * distance), (low, high)


def _location_component(config: Config, location: str | None, remote: bool) -> float:
    if not location:
        return 0.6
    if in_target_geography(config, location):
        return 1.0
    is_remote = remote or bool(re.search(r"\bremote\b", location, re.I))
    if is_remote and config.search.remote_ok:
        # Open remote is as good as local; region-locked remote is worthless.
        return 0.9 if remote_is_eligible(config, location) else 0.0
    return 0.0


def _recency_component(config: Config, posted_at: datetime | None) -> float:
    if posted_at is None:
        return 0.5
    posted = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - posted).days
    if age_days <= 3:
        return 1.0
    horizon = max(config.search.posted_within_days, 4)
    return max(0.0, 1.0 - (age_days - 3) / (horizon - 3))


def score_job(config: Config, job) -> ScoreResult:
    """Score a job. ``job`` needs title/location/remote/description/posted_at."""
    reason = hard_filter_reason(config, job)

    text = f"{job.title or ''}\n{job.description or ''}"
    jd_skills = extract_skills(text)
    weights = config.matching.weights

    skills_ratio, matched, missing = _skills_component(config, jd_skills)
    title_ratio = _title_component(config, job.title or "")
    experience_ratio, required = _experience_component(config, job.title or "", job.description)
    location_ratio = _location_component(config, job.location, bool(job.remote))
    recency_ratio = _recency_component(config, job.posted_at)

    detail = {
        "skills": round(weights.skills * skills_ratio, 2),
        "title": round(weights.title * title_ratio, 2),
        "experience": round(weights.experience * experience_ratio, 2),
        "location": round(weights.location * location_ratio, 2),
        "recency": round(weights.recency * recency_ratio, 2),
    }
    total = round(sum(detail.values()), 2)

    return ScoreResult(
        total=0.0 if reason else total,
        detail=detail,
        matched_skills=matched,
        missing_skills=missing,
        jd_skills=sorted(jd_skills),
        required_experience=required,
        rejected_reason=reason,
    )
