"""Time a real live search, the way the dashboard runs it.

The interactive path only earns its name if it finishes while somebody is still
willing to watch, so this measures the thing that matters: wall-clock seconds
from click to results, against the live sources, with no description fetches.

    python scripts/time_live_search.py "Java Backend Engineer" Gurugram Noida
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobagent.config import load_config  # noqa: E402
from jobagent.db import init_db, session_scope  # noqa: E402
from jobagent.fetch import _live_sources, live, live_config  # noqa: E402
from jobagent.matching.skills import derive_skills  # noqa: E402
from jobagent.models import Search  # noqa: E402
from jobagent.results import rank  # noqa: E402
from jobagent.web import auth  # noqa: E402

RESUME = {
    "skills": {"languages": ["Java", "SQL"], "frameworks": ["Spring Boot", "Kafka"]},
    "experience": [
        {
            "title": "Backend Engineer",
            "bullets": [
                "Built Spring Boot microservices consuming Kafka events",
                "Tuned Couchbase and PostgreSQL queries, deployed with Jenkins",
            ],
        }
    ],
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    role = sys.argv[1] if len(sys.argv) > 1 else "Java Backend Engineer"
    cities = sys.argv[2:] or ["Gurugram", "Noida", "Delhi"]

    base = load_config()
    init_db(base)

    email = "timing@example.com"
    with session_scope(base) as session:
        user = auth.verify(session, email, "timing-password-123")
        if user is None:
            from sqlalchemy import select

            from jobagent.models import User

            user = session.scalar(select(User).where(User.email == email))
            if user is None:
                user = auth.create_user(session, email, "timing-password-123")
        user.resume, user.skills = RESUME, derive_skills(RESUME)
        search = Search(
            user_id=user.id, role=role, titles=[role], keywords=[role],
            locations=cities, include_remote=True,
            min_experience_years=2, max_experience_years=6,
        )
        session.add(search)
        session.flush()
        user_id, search_id = user.id, search.id
        config = live_config(base, search, user.skills)
        sources = _live_sources(config)
        queries = len(config.sources.linkedin.queries)

    print(f"\nrole: {role}\ncities: {', '.join(cities)}")
    print(f"live sources: {len(sources)} -> {', '.join(sources)}")
    print(f"linkedin queries: {queries}, descriptions per query: "
          f"{config.sources.linkedin.descriptions_per_query}\n")

    started = time.monotonic()
    stats = live(config, user_id=user_id, search_id=search_id,
                 on_progress=lambda m: print(f"  {time.monotonic() - started:5.1f}s  {m}"))
    elapsed = time.monotonic() - started

    print(f"\nfetch finished in {elapsed:.1f}s")
    print(f"  {stats.discovered} postings seen, {stats.new} new")
    print(f"  answered: {', '.join(stats.sources_ok) or 'none'}")
    for name, error in stats.sources_failed.items():
        print(f"  failed  {name}: {error}")
    if stats.timed_out:
        print("  hit the time budget; the rest lands on the next crawl")

    scoring_started = time.monotonic()
    with session_scope(base) as session:
        from jobagent.models import User

        user = session.get(User, user_id)
        search = session.get(Search, search_id)
        ranked = rank(session, user, search, base)
    scoring = time.monotonic() - scoring_started

    print(f"\nranking took {scoring * 1000:.0f}ms -> {len(ranked)} matches")
    for entry in ranked[:8]:
        print(f"  {entry.score:3d}  {entry.job.title[:46]:46s} {entry.job.company[:20]:20s}"
              f" {(entry.job.location or '-')[:18]}")

    verdict = "interactive" if elapsed <= 45 else "too slow for a live wait"
    print(f"\ntotal click-to-results: {elapsed + scoring:.1f}s ({verdict})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
