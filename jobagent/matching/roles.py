"""Expand one job role into the family of titles employers actually post.

Typing "backend engineer" and searching only that phrase misses "Java Developer",
"SDE II" and "Member of Technical Staff", which are the same job. A curated
family table covers the common cases instantly and offline; an LLM, when one is
available, adds the variants the table has not seen yet.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..config import Config, load_config

log = logging.getLogger(__name__)

MAX_TITLES = 16
MAX_KEYWORDS = 5

# trigger pattern -> titles that describe the same work
ROLE_FAMILIES: list[tuple[re.Pattern[str], list[str]]] = [
    (
        re.compile(r"\b(backend|back-end|java|spring|micro-?services?|api)\b", re.I),
        [
            "Backend Engineer", "Backend Developer", "Java Developer",
            "Java Backend Engineer", "Software Engineer", "Software Engineer II",
            "Software Development Engineer", "SDE", "Senior Software Engineer",
            "Microservices Developer", "API Engineer", "Platform Engineer",
            "Backend Software Engineer", "Server Side Engineer",
        ],
    ),
    (
        re.compile(r"\b(full-?stack|mern|mean)\b", re.I),
        [
            "Full Stack Engineer", "Full Stack Developer", "Software Engineer",
            "Software Development Engineer", "MERN Stack Developer",
            "Backend Engineer", "Web Developer",
        ],
    ),
    (
        re.compile(r"\b(frontend|front-end|react|angular|vue|ui engineer)\b", re.I),
        [
            "Frontend Engineer", "Frontend Developer", "React Developer",
            "UI Engineer", "Web Developer", "Software Engineer, Frontend",
        ],
    ),
    (
        re.compile(r"\b(devops|sre|site reliability|platform|infrastructure|cloud engineer)\b", re.I),
        [
            "DevOps Engineer", "Site Reliability Engineer", "Platform Engineer",
            "Cloud Engineer", "Infrastructure Engineer", "Build and Release Engineer",
            "Kubernetes Engineer",
        ],
    ),
    (
        re.compile(r"\b(data engineer|etl|spark|big ?data|pipeline)\b", re.I),
        [
            "Data Engineer", "Big Data Engineer", "ETL Developer",
            "Analytics Engineer", "Software Engineer, Data", "Spark Developer",
        ],
    ),
    (
        re.compile(r"\b(machine learning|ml engineer|ai engineer|deep learning|llm)\b", re.I),
        [
            "Machine Learning Engineer", "AI Engineer", "MLOps Engineer",
            "Applied Scientist", "Software Engineer, Machine Learning",
        ],
    ),
    (
        re.compile(r"\b(android|ios|mobile|flutter|react native)\b", re.I),
        [
            "Android Developer", "iOS Developer", "Mobile Engineer",
            "React Native Developer", "Flutter Developer",
        ],
    ),
    (
        re.compile(r"\b(qa|sdet|test|automation)\b", re.I),
        [
            "QA Engineer", "SDET", "Automation Test Engineer",
            "Software Test Engineer", "Quality Engineer",
        ],
    ),
    (
        re.compile(r"\b(python|django|fastapi)\b", re.I),
        [
            "Python Developer", "Backend Engineer", "Software Engineer",
            "Django Developer", "Software Development Engineer",
        ],
    ),
    (
        re.compile(r"\b(node|nodejs|typescript|javascript)\b", re.I),
        [
            "Node.js Developer", "Backend Engineer", "Software Engineer",
            "JavaScript Developer", "TypeScript Developer",
        ],
    ),
]

EXPAND_PROMPT = """You know how job titles are actually written on Indian and global job
boards.

Given a role the candidate is looking for, list the OTHER job titles that
describe the same work and would be worth searching for. Include seniority
variants that fit the stated experience, and Indian-market phrasings.

Rules:
- Same job, different wording only. Do not drift to a different discipline.
- No company names, no locations, no seniority the candidate cannot hold.
- 8 to 12 titles.

Return ONLY JSON: {"titles": ["...", "..."], "keywords": ["search phrase", "..."]}
where "keywords" are 3-5 short phrases good for a job-board search box."""


@dataclass(slots=True)
class RoleExpansion:
    role: str
    titles: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    source: str = "curated"
    notes: list[str] = field(default_factory=list)


def _dedupe(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" ,.-")
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            output.append(cleaned)
        if len(output) >= limit:
            break
    return output


def curated_titles(role: str) -> list[str]:
    titles: list[str] = [role]
    for pattern, family in ROLE_FAMILIES:
        if pattern.search(role):
            titles.extend(family)
    return titles


def _fallback_keywords(role: str, titles: list[str], experience_years: int | None) -> list[str]:
    base = [role]
    base.extend(titles[1:4])
    if experience_years is not None and experience_years >= 4:
        base.append(f"senior {role}")
    return _dedupe(base, MAX_KEYWORDS)


def expand_role(
    role: str,
    config: Config | None = None,
    *,
    use_llm: bool = True,
    experience_years: int | None = None,
) -> RoleExpansion:
    config = config or load_config()
    role = re.sub(r"\s+", " ", role or "").strip()
    if not role:
        return RoleExpansion(role="", notes=["No role provided."])

    titles = curated_titles(role)
    keywords: list[str] = []
    source = "curated"
    notes: list[str] = []

    if use_llm:
        from ..resume.llm import get_llm

        llm = get_llm(config)
        if llm is not None:
            try:
                payload = {
                    "role": role,
                    "years_of_experience": experience_years,
                    "titles_already_considered": _dedupe(titles, MAX_TITLES),
                }
                import json

                produced = llm.complete_json(EXPAND_PROMPT, json.dumps(payload))
                extra = [str(t) for t in produced.get("titles") or []]
                keywords = [str(k) for k in produced.get("keywords") or []]
                if extra:
                    titles = titles + extra
                    source = llm.label
            except Exception as exc:
                log.warning("role expansion via LLM failed: %s", exc)
                notes.append(f"Used the built-in title list ({str(exc)[:80]}).")

    return RoleExpansion(
        role=role,
        titles=_dedupe(titles, MAX_TITLES),
        keywords=_dedupe(keywords, MAX_KEYWORDS) or _fallback_keywords(role, titles, experience_years),
        source=source,
        notes=notes,
    )
