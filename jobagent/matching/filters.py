"""Hard filters: cheap, deterministic rejections applied before scoring."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from rapidfuzz import fuzz

from ..config import Config
from .skills import parse_required_experience

GENERIC_ROLE_WORDS = re.compile(
    r"\b(engineer|developer|programmer|sde|swe|architect|specialist|consultant)\b", re.I
)

# "Engineer" is not the same as "software engineer". These titles are engineering
# roles in disciplines the candidate cannot apply for at all.
NON_SOFTWARE_DISCIPLINE = re.compile(
    r"\b(electrical|electronics|mechanical|hardware|manufactur\w*|pcba|actuator|optical"
    r"|thermal|civil|chemical|industrial|structural|silicon|asic|rf|antenna|battery"
    r"|technician|machinist|process engineer|field engineer|sales engineer)\b",
    re.I,
)

# A geographic remote posting is only useful if we are allowed to sit in it.
GLOBAL_REMOTE = re.compile(r"\b(anywhere|global|worldwide|international)\b", re.I)

# A posting that names only the country says nothing about which city, so it is
# ambiguous rather than out of scope, and gets the same benefit of the doubt as
# a posting with no location at all.
COUNTRY_ONLY = re.compile(r"^\s*india\s*$", re.I)

# Lookarounds instead of \b: a trailing \b never matches after an abbreviation's
# final dot, which silently let every "Remote U.S." posting through.
FOREIGN_REGION = re.compile(
    r"(?<!\w)(?:"
    r"united states|u\.?s\.?a?|americas|amer|latam|north america|canada"
    r"|brazil|mexico|argentina|colombia|chile|costa rica"
    r"|emea|europe|united kingdom|u\.?k\.?|ireland|germany|france|netherlands|poland"
    r"|spain|portugal|romania|sweden|switzerland|denmark|norway|israel|turkey"
    r"|apac|singapore|japan|korea|china|taiwan|hong kong|australia|new zealand"
    r"|philippines|vietnam|indonesia|malaysia|thailand"
    r"|nigeria|kenya|south africa|egypt|uae|dubai|saudi|qatar"
    r")(?!\w)",
    re.I,
)


def title_prefilter(config: Config, title: str | None) -> bool:
    """Loose gate used by sources before spending a request on a full JD.

    Errs towards keeping: the real decision happens in the scorer, this only
    avoids fetching descriptions for obviously irrelevant postings.
    """
    if not title:
        return False
    lowered = title.lower()
    for bad in config.search.exclude_title_keywords:
        if bad.lower() in lowered:
            return False
    if not GENERIC_ROLE_WORDS.search(lowered):
        return False
    if NON_SOFTWARE_DISCIPLINE.search(title):
        return False
    for wanted in config.search.titles:
        if fuzz.partial_ratio(wanted.lower(), lowered) >= 80:
            return True
    return any(keyword.lower() in lowered for keyword in config.search.must_have_any)


def in_target_geography(config: Config, location: str | None) -> bool:
    if not location:
        return False
    lowered = location.lower()
    return any(city.lower() in lowered for city in config.search.locations)


def remote_is_eligible(config: Config, location: str | None) -> bool:
    """Can someone sitting in India actually take this remote role?

    "Remote - US" and "EMEA Remote" are remote jobs you cannot have, so a bare
    remote flag is not enough: the location text has to either name a place we
    target or be genuinely open ("Remote", "Anywhere").
    """
    if not location:
        return True  # unqualified remote: give it the benefit of the doubt
    if in_target_geography(config, location):
        return True
    if FOREIGN_REGION.search(location):
        return False
    return bool(GLOBAL_REMOTE.search(location) or re.search(r"\bremote\b", location, re.I))


def location_intent(config: Config, location: str | None) -> str:
    """Classify a posting's location before spending a request on its JD.

    Returns ``"target"`` (somewhere we want), ``"foreign"`` (definitely not) or
    ``"unknown"``. Sources use this to spend a limited description budget on the
    postings that could actually matter: Workday and LinkedIn both hand out the
    location in the cheap list response, long before the JD costs anything.
    """
    if not location:
        return "unknown"
    if in_target_geography(config, location):
        return "target"
    # "2 Locations" is Workday collapsing a multi-city posting; one of them may
    # well be the one we want, so it is not a rejection.
    if re.match(r"^\s*\d+\s+locations", location, re.I):
        return "unknown"
    if FOREIGN_REGION.search(location):
        return "foreign"
    if GLOBAL_REMOTE.search(location) or re.search(r"\bremote\b", location, re.I):
        return "target" if config.search.remote_ok else "foreign"
    return "unknown"


def location_matches(config: Config, location: str | None, remote: bool) -> bool:
    if not location:
        return True  # unknown location: let the score decide instead of dropping it
    if in_target_geography(config, location):
        return True
    if COUNTRY_ONLY.match(location):
        return True
    is_remote = remote or bool(re.search(r"\bremote\b", location, re.I))
    if is_remote and config.search.remote_ok:
        return remote_is_eligible(config, location)
    return False


def hard_filter_reason(config: Config, job) -> str | None:
    """Return a human-readable rejection reason, or None if the job is viable.

    ``job`` is anything with title/location/remote/description/posted_at.
    """
    title = job.title or ""
    lowered = title.lower()

    for bad in config.search.exclude_title_keywords:
        if bad.lower() in lowered:
            return f"excluded title keyword: {bad}"

    discipline = NON_SOFTWARE_DISCIPLINE.search(title)
    if discipline:
        return f"not a software role: {discipline.group(0)}"

    # Location before keywords: it is the more decisive test, and checking it
    # first stops an unreadable description from taking the blame for a job that
    # was out of scope anyway.
    if not location_matches(config, job.location, bool(job.remote)):
        return f"location out of scope: {job.location}"

    description = (job.description or "").strip()
    haystack = f"{title}\n{description}".lower()
    if config.search.must_have_any and not any(
        keyword.lower() in haystack for keyword in config.search.must_have_any
    ):
        if not description:
            # Distinct reason: the job may well be relevant, we just never got
            # its text. Worth knowing when a source's description budget is too
            # tight rather than blaming the posting.
            return "no job description could be read"
        return "no backend keyword in title or description"

    if job.posted_at and config.search.posted_within_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.search.posted_within_days)
        posted = job.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        if posted < cutoff:
            return f"posted {(datetime.now(timezone.utc) - posted).days}d ago"

    min_required, _ = parse_required_experience(job.description)
    if min_required is not None and min_required > config.search.max_experience_years:
        return f"needs {min_required}+ years experience"

    return None
