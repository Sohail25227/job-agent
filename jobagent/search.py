"""Turn one saved search into queries for every source.

The dashboard collects a role and a few cities. Each source wants that in its
own shape: LinkedIn needs "Pune, Maharashtra, India", Adzuna wants "Pune",
Workday only takes free text, and the ATS boards take nothing at all because
they are addressed by company. This module owns those translations so the
sources stay dumb and the UI stays simple.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select

from .config import Config, load_config
from .matching.roles import expand_role
from .models import Search

log = logging.getLogger(__name__)

# Keep the query matrix bounded: every combination is an HTTP round trip.
MAX_KEYWORDS_PER_SOURCE = 4
MAX_CITY_QUERIES = 24

REMOTE_LABEL = "Remote"

# City -> the string LinkedIn's geo autocomplete expects. Anything not listed
# still works; it just gets " , India" appended.
INDIA_CITIES: dict[str, str] = {
    "Bengaluru": "Bengaluru, Karnataka, India",
    "Hyderabad": "Hyderabad, Telangana, India",
    "Pune": "Pune, Maharashtra, India",
    "Gurugram": "Gurugram, Haryana, India",
    "Noida": "Noida, Uttar Pradesh, India",
    "Delhi": "Delhi, India",
    "Mumbai": "Mumbai, Maharashtra, India",
    "Chennai": "Chennai, Tamil Nadu, India",
    "Kolkata": "Kolkata, West Bengal, India",
    "Ahmedabad": "Ahmedabad, Gujarat, India",
    "Jaipur": "Jaipur, Rajasthan, India",
    "Indore": "Indore, Madhya Pradesh, India",
    "Chandigarh": "Chandigarh, India",
    "Kochi": "Kochi, Kerala, India",
    "Coimbatore": "Coimbatore, Tamil Nadu, India",
    "Bhubaneswar": "Bhubaneswar, Odisha, India",
    "India (anywhere)": "India",
}

# The Muse answers an unrecognised location with a default set rather than an
# error, so a guessed label is worse than no query at all: it looks like
# coverage while delivering the wrong city. Every entry below was checked
# against the live API by scripts/probe_new_sources.py. Note that this is the
# one place the *older* spellings are correct - "Gurugram, India" and
# "Delhi, India" are both silently ignored there.
MUSE_CITIES: dict[str, str] = {
    "Gurugram": "Gurgaon, India",
    "Delhi": "New Delhi, India",
    "Noida": "Noida, India",
    "Bengaluru": "Bangalore, India",
    "Hyderabad": "Hyderabad, India",
    "Pune": "Pune, India",
    "Mumbai": "Mumbai, India",
    "Chennai": "Chennai, India",
}

# Cities that are really one market; searching both wastes a round trip.
CITY_ALIASES = {"Gurgaon": "Gurugram", "Bangalore": "Bengaluru", "New Delhi": "Delhi"}

# Employers use both spellings freely, so a search for Gurugram has to accept a
# posting that says Gurgaon. Built from CITY_ALIASES so the two cannot drift.
CITY_SPELLINGS: dict[str, list[str]] = {}
for _alias, _canonical in CITY_ALIASES.items():
    CITY_SPELLINGS.setdefault(_canonical, [_canonical]).append(_alias)

# Sources addressed by company slug rather than by keyword. A saved search
# cannot steer them, but they are still worth scanning on every run.
COMPANY_ADDRESSED = {
    "greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee",
}

# Sources a keyword *can* steer, but which must still stay off the live path
# because their request budget is too small to hand to whoever clicks first.
# jobdataapi allows roughly ten anonymous requests an hour for the whole app,
# so one impatient user could throttle the scheduled crawl for everybody.
CRAWL_ONLY = {"jobdataapi"}


def canonical_city(city: str) -> str:
    city = (city or "").strip()
    return CITY_ALIASES.get(city, city)


def linkedin_location(city: str) -> str:
    city = canonical_city(city)
    if city.lower() in {"remote", "anywhere"}:
        return "India"
    return INDIA_CITIES.get(city) or (city if city.endswith("India") else f"{city}, India")


def muse_location(city: str) -> str | None:
    """The Muse's own label for a city, or None if it has no verified one."""
    return MUSE_CITIES.get(canonical_city(city))


def city_spellings(city: str) -> list[str]:
    """Every spelling a posting might use for a typed city.

    Someone filtering for "Gurgaon" means the same market as a posting that
    says "Gurugram", so matching the literal string would hide half the
    results for no reason the reader could guess.
    """
    canonical = canonical_city(city)
    return CITY_SPELLINGS.get(canonical, [canonical])


# Words that appear in every engineering title and so say nothing about what the
# job is. Keeping them would make the keyword gate match anything at all.
GENERIC_TITLE_WORDS = {
    "developer", "development", "engineer", "engineering", "software", "senior",
    "sr", "junior", "jr", "lead", "principal", "staff", "member", "technical",
    "associate", "specialist", "consultant", "analyst", "manager", "programmer",
    "professional", "experienced", "years", "sde", "swe", "i", "ii", "iii", "iv",
    "end", "side", "and", "the", "for", "with", "of",
}


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _accepted_locations(cities: list[str]) -> list[str]:
    """Every spelling of the chosen cities that a posting might use."""
    accepted: list[str] = []
    for city in cities:
        for spelling in CITY_SPELLINGS.get(city, [city]):
            if spelling not in accepted:
                accepted.append(spelling)
    return sorted(accepted)


def _topic_terms(titles: list[str], keywords: list[str]) -> list[str]:
    """The words in a search that actually name the discipline."""
    terms: list[str] = []
    seen: set[str] = set()
    for phrase in [*titles, *keywords]:
        for token in re.split(r"[^a-z0-9+#.\-]+", phrase.lower()):
            token = token.strip(".-")
            if len(token) < 2 or token in GENERIC_TITLE_WORDS or token in seen:
                continue
            seen.add(token)
            terms.append(token)
    return terms


def _relevant_excludes(excludes: list[str], titles: list[str],
                       keywords: list[str]) -> list[str]:
    """Drop the hard rejects that describe the very role being searched for."""
    vocabulary = [_squash(phrase) for phrase in [*titles, *keywords]]
    return [
        exclude for exclude in excludes
        if not any(_squash(exclude) in phrase for phrase in vocabulary)
    ]


@dataclass(slots=True)
class SearchPlan:
    titles: list[str]
    keywords: list[str]
    cities: list[str]
    query_count: int


def build_search_config(base: Config, search: Search,
                        skills: dict[str, float] | None = None) -> tuple[Config, SearchPlan]:
    """Clone the config and point every keyword-driven source at this search.

    ``skills`` are the searching user's, so the same posting can score
    differently for two people. Without them the config's own list is used,
    which is what the single-user CLI still wants.
    """
    config = base.model_copy(deep=True)
    if skills:
        config.matching.my_skills = dict(skills)

    titles = list(search.titles or [search.role])
    keywords = list(search.keywords or titles)[:MAX_KEYWORDS_PER_SOURCE]
    cities = [canonical_city(c) for c in (search.locations or []) if c and c != REMOTE_LABEL]
    anywhere = bool(search.remote_only) or not cities
    if anywhere:
        cities = ["India (anywhere)"]

    config.search.titles = titles
    # The chosen cities are the filter, not just the query. Keeping "India" in
    # this list as well would admit every Indian posting and make the city
    # picker decorative, so the country is only allowed when no city was picked.
    config.search.locations = ["India"] if anywhere else _accepted_locations(cities)
    config.search.remote_ok = bool(search.include_remote or search.remote_only)
    # config.yaml's keyword filters describe one discipline. A saved search for a
    # different one has to bring its own, or every result it finds is thrown away
    # as off-topic - and the excludes would even reject the role being searched.
    topics = _topic_terms(titles, keywords)
    if topics:
        config.search.must_have_any = topics
    config.search.exclude_title_keywords = _relevant_excludes(
        base.search.exclude_title_keywords, titles, keywords
    )
    if search.min_experience_years is not None:
        config.search.min_experience_years = search.min_experience_years
    if search.max_experience_years is not None:
        config.search.max_experience_years = search.max_experience_years

    pairs: list[tuple[str, str]] = []
    for keyword in keywords:
        for city in cities:
            pairs.append((keyword, city))
    pairs = pairs[:MAX_CITY_QUERIES]

    config.sources.linkedin.queries = [
        {"keywords": keyword, "location": linkedin_location(city)} for keyword, city in pairs
    ]
    config.sources.adzuna.queries = [
        {"what": keyword, "where": "" if city.startswith("India") else city}
        for keyword, city in pairs
    ]
    config.sources.jooble.queries = [
        {"keywords": keyword, "location": "" if city.startswith("India") else city}
        for keyword, city in pairs
    ]
    # JobDataAPI returns nothing for a city it does not know, so its own
    # canonical spelling can go straight through without a lookup table.
    config.sources.jobdataapi.queries = [
        {"title": keyword, "location": "" if city.startswith("India") else city}
        for keyword, city in pairs
    ]
    # The Muse takes no keyword at all, so it is addressed by city alone and
    # only for the cities it is known to recognise.
    config.sources.themuse.queries = [
        {"location": label}
        for label in dict.fromkeys(
            filter(None, (muse_location(city) for city in cities))
        )
    ]
    config.sources.workday.queries = keywords
    config.sources.remotive.queries = keywords[:2]

    plan = SearchPlan(titles=titles, keywords=keywords, cities=cities, query_count=len(pairs))
    log.info(
        "search %s: %d titles, %d keywords, %d cities -> %d keyword queries",
        search.role, len(titles), len(keywords), len(cities), plan.query_count,
    )
    return config, plan


def create_search(
    session,
    user_id: int,
    role: str,
    locations: list[str],
    *,
    include_remote: bool = True,
    remote_only: bool = False,
    min_experience_years: int | None = None,
    max_experience_years: int | None = None,
    config: Config | None = None,
    use_llm: bool = True,
) -> Search:
    """Expand the role into a title family and persist the search for one user.

    Submitting the same criteria again returns the existing search rather than
    piling up copies: a fetch takes a while, so it is easy to resubmit the form
    thinking nothing happened.
    """
    config = config or load_config()
    expansion = expand_role(
        role, config, use_llm=use_llm, experience_years=min_experience_years
    )
    cities = [canonical_city(c) for c in locations]

    existing = _find_identical(
        session,
        user_id=user_id,
        role=expansion.role or role,
        cities=cities,
        include_remote=include_remote,
        remote_only=remote_only,
        min_experience_years=min_experience_years,
        max_experience_years=max_experience_years,
    )
    if existing is not None:
        log.info("reusing search %d for user %d (%r)", existing.id, user_id, role)
        return existing

    search = Search(
        user_id=user_id,
        role=expansion.role or role,
        titles=expansion.titles,
        keywords=expansion.keywords,
        locations=cities,
        include_remote=include_remote,
        remote_only=remote_only,
        min_experience_years=min_experience_years,
        max_experience_years=max_experience_years,
        expansion_source=expansion.source,
    )
    session.add(search)
    session.flush()
    return search


def _find_identical(session, *, user_id: int, role: str, cities: list[str],
                    include_remote: bool, remote_only: bool,
                    min_experience_years: int | None,
                    max_experience_years: int | None) -> Search | None:
    wanted = sorted(cities)
    candidates = session.scalars(select(Search).where(Search.user_id == user_id)).all()
    for candidate in candidates:
        if candidate.role.strip().lower() != role.strip().lower():
            continue
        if sorted(candidate.locations or []) != wanted:
            continue
        if bool(candidate.include_remote) != bool(include_remote):
            continue
        if bool(candidate.remote_only) != bool(remote_only):
            continue
        if candidate.min_experience_years != min_experience_years:
            continue
        if candidate.max_experience_years != max_experience_years:
            continue
        return candidate
    return None
