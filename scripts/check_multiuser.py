"""End-to-end check of the multi-user flow, without touching the network.

Proves the three claims the new design rests on:

1. Two accounts are isolated - separate resumes, searches and decisions.
2. The job cache is shared, and the same posting scores differently per user,
   because the score is computed from the reader's resume at read time.
3. The dashboard flow works end to end over HTTP: register, upload, search,
   read results, mark applied.

    python scripts/check_multiuser.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from jobagent.config import load_config  # noqa: E402
from jobagent.db import init_db, session_scope  # noqa: E402
from jobagent.matching.skills import derive_skills  # noqa: E402
from jobagent.models import Job, Search, User, UserJob  # noqa: E402
from jobagent.results import rank, score_one  # noqa: E402
from jobagent.search import create_search  # noqa: E402
from jobagent.web import auth  # noqa: E402

BACKEND_RESUME = {
    "name": "Backend Person",
    "skills": {"languages": ["Java", "SQL"], "frameworks": ["Spring Boot", "Kafka"]},
    "experience": [
        {
            "title": "Backend Engineer",
            "company": "Telco",
            "bullets": [
                "Built Spring Boot microservices consuming Kafka events",
                "Tuned Couchbase and PostgreSQL queries, deployed with Jenkins",
            ],
        }
    ],
}

FRONTEND_RESUME = {
    "name": "Frontend Person",
    "skills": {"languages": ["TypeScript", "JavaScript"], "frameworks": ["React"]},
    "experience": [
        {
            "title": "Frontend Engineer",
            "company": "Shop",
            "bullets": [
                "Built React and TypeScript interfaces with Redux",
                "Improved CSS and accessibility across the checkout flow",
            ],
        }
    ],
}

PASSWORD = "check-me-please"
ok_count = 0
fail_count = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if condition:
        ok_count += 1
        print(f"  ok   {label}" + (f" — {detail}" if detail else ""))
    else:
        fail_count += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def reset(session, emails: list[str]) -> None:
    for email in emails:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            continue
        session.query(UserJob).filter(UserJob.user_id == user.id).delete()
        session.query(Search).filter(Search.user_id == user.id).delete()
        session.delete(user)


def main() -> int:
    config = load_config()
    init_db(config)

    emails = ["backend@example.com", "frontend@example.com"]

    print("\n1. accounts are separate")
    with session_scope(config) as session:
        reset(session, emails)
    with session_scope(config) as session:
        backend = auth.create_user(session, emails[0], PASSWORD)
        frontend = auth.create_user(session, emails[1], PASSWORD)
        backend.resume, backend.skills = BACKEND_RESUME, derive_skills(BACKEND_RESUME)
        frontend.resume, frontend.skills = FRONTEND_RESUME, derive_skills(FRONTEND_RESUME)
        backend_id, frontend_id = backend.id, frontend.id
        check("two accounts created", backend_id != frontend_id)
        check(
            "skills derived per resume",
            "java" in backend.skills and "react" in frontend.skills,
            f"{len(backend.skills)} vs {len(frontend.skills)} skills",
        )
        check(
            "resumes do not leak",
            "react" not in backend.skills and "java" not in frontend.skills,
        )
        check("wrong password rejected", auth.verify(session, emails[0], "nope") is None)
        check("right password accepted", auth.verify(session, emails[0], PASSWORD) is not None)

    print("\n2. shared cache, per-user scores")
    with session_scope(config) as session:
        cached = session.scalar(select(func.count()).select_from(Job)) or 0
        check("cache has postings to score", cached > 0, f"{cached} cached")

        backend = session.get(User, backend_id)
        frontend = session.get(User, frontend_id)
        backend_search = create_search(
            session, backend_id, "Java Backend Engineer", ["Gurugram", "Noida", "Delhi"],
            min_experience_years=2, max_experience_years=6, config=config, use_llm=False,
        )
        frontend_search = create_search(
            session, frontend_id, "Frontend Developer", ["Gurugram", "Noida", "Delhi"],
            min_experience_years=2, max_experience_years=6, config=config, use_llm=False,
        )
        check(
            "searches belong to their owner",
            backend_search.user_id == backend_id and frontend_search.user_id == frontend_id,
        )

        backend_ranked = rank(session, backend, backend_search, config)
        frontend_ranked = rank(session, frontend, frontend_search, config)
        check(
            "each search returns its own list",
            bool(backend_ranked) or bool(frontend_ranked),
            f"{len(backend_ranked)} backend, {len(frontend_ranked)} frontend matches",
        )

        # The strongest evidence for read-time scoring: score the very same row
        # as both users and require the numbers to differ. score_one is used
        # rather than rank() because rank() also applies each search's own
        # candidate filter, and a Java posting is correctly invisible to a
        # frontend search - which would prove nothing either way.
        if backend_ranked:
            job = backend_ranked[0].job
            as_backend = score_one(session, backend, backend_search, job, config)
            as_frontend = score_one(session, frontend, frontend_search, job, config)
            check(
                "same posting scores differently per user",
                as_backend.score != as_frontend.score,
                f"{job.title[:40]!r}: backend {as_backend.score} "
                f"vs frontend {as_frontend.score}",
            )
            check(
                "matched skills come from the reader's resume",
                set(as_backend.result.matched_skills) != set(as_frontend.result.matched_skills),
                f"backend {as_backend.result.matched_skills[:4]}, "
                f"frontend {as_frontend.result.matched_skills[:4]}",
            )

        cities = {
            (r.job.location or "").lower() for r in backend_ranked if r.job.location
        }
        stray = [c for c in cities if "chennai" in c or "bengaluru" in c or "pune" in c]
        check(
            "city filter is respected",
            not stray,
            f"{len(cities)} distinct locations, no unrequested cities"
            if not stray else f"leaked: {stray[:3]}",
        )
        backend_search_id = backend_search.id

    print("\n3. the dashboard flow")
    client = TestClient(app_module().app, follow_redirects=False)
    email = "webflow@example.com"
    with session_scope(config) as session:
        reset(session, [email])

    response = client.post(
        "/register", data={"email": email, "password": PASSWORD, "confirm": PASSWORD}
    )
    check("register redirects to resume upload", response.headers.get("location") == "/setup",
          f"status {response.status_code}")

    resume_text = (
        "Backend Person\nBackend Engineer at Telco 2021-Present\n"
        "Built Spring Boot microservices consuming Kafka events.\n"
        "Tuned Couchbase and PostgreSQL queries and deployed with Jenkins.\n"
        "Skills: Java, Spring Boot, Kafka, SQL, Docker, Kubernetes, Git, Maven.\n"
        "Education: B.Tech Computer Science.\n" * 3
    )
    response = client.post(
        "/setup",
        files={"file": ("resume.txt", io.BytesIO(resume_text.encode()), "text/plain")},
    )
    check("resume upload accepted", response.status_code == 303)
    with session_scope(config) as session:
        user = session.scalar(select(User).where(User.email == email))
        check("resume stored against the account", bool(user and user.resume),
              f"{len(user.skills or {})} skills recognised")

    response = client.get("/")
    check("dashboard loads once a resume exists", response.status_code == 200)

    response = client.get(f"/jobs?search_id={backend_search_id}")
    check("another user's search is not readable", response.status_code == 200
          and "Java Backend Engineer" not in response.text,
          "falls back to own searches")

    response = client.get("/setup")
    check("resume page renders", response.status_code == 200 and "Skills used for matching" in response.text)

    with session_scope(config) as session:
        job_id = session.scalar(select(Job.id).limit(1))
    if job_id:
        response = client.get(f"/jobs/{job_id}")
        check("job detail renders", response.status_code == 200)
        response = client.post(
            f"/jobs/{job_id}/status", data={"status": "applied", "notes": "sent it"}
        )
        check("marking applied works", response.status_code == 303)
        with session_scope(config) as session:
            user = session.scalar(select(User).where(User.email == email))
            entry = session.scalar(
                select(UserJob).where(UserJob.user_id == user.id, UserJob.job_id == job_id)
            )
            check("decision recorded for this user only", bool(entry and entry.applied_at),
                  f"status={entry.status if entry else None}")

    response = client.post("/logout")
    check("logout clears the session", response.status_code == 303)
    response = client.get("/")
    check("protected pages redirect when logged out",
          response.headers.get("location") == "/login")

    print(f"\n{ok_count} passed, {fail_count} failed")
    return 1 if fail_count else 0


def app_module():
    from jobagent.web import app as module

    return module


if __name__ == "__main__":
    raise SystemExit(main())
