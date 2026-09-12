"""The dashboard.

Flow: sign up, upload your resume once, describe the role you want, then press
search. The search goes out to every source that can be steered by a keyword,
in parallel, while the page polls for progress. What comes back is ranked
against your resume, and every row carries the employer's own apply link.

Nothing is ever submitted on your behalf.

Openings are cached in one shared table, so a search by one user warms the cache
for the next. Scores are not cached: they are computed per request from the
reader's own resume, because the same posting is a different opportunity for two
different people.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

from ..config import load_config
from ..db import init_db, session_scope
from ..fetch import live, live_config
from ..matching.roles import expand_role
from ..matching.skills import derive_skills
from ..models import Job, JobStatus, RunLog, Search, User, UserJob, utcnow
from ..resume.ingest import SUPPORTED_SUFFIXES, parse_upload
from ..results import rank, score_one
from ..search import CITY_ALIASES, INDIA_CITIES, city_spellings, create_search
from . import auth, tasks

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

PUBLIC_PATHS = {"/login", "/register", "/health", "/favicon.ico"}


def _blank_to_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


# A number that reaches us from an HTML form, where an empty input still submits
# its name: "min_score=" has to mean "no filter", not a 422 that replaces the
# whole results page with a JSON error.
BlankableInt = Annotated[int | None, BeforeValidator(_blank_to_none)]

STATUS_LABELS = {
    JobStatus.SAVED.value: "Shortlisted",
    JobStatus.APPLIED.value: "Applied",
    JobStatus.REJECTED.value: "Passed",
    JobStatus.HIDDEN.value: "Hidden",
}

# Live searches spend a third-party quota shared by every account.
DAILY_SEARCH_LIMIT = int(os.getenv("DAILY_SEARCH_LIMIT", "25"))

app = FastAPI(title="job-agent", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    init_db(load_config())


# Middleware registration order is counter-intuitive: add_middleware inserts at
# the front of the stack, so whatever is registered LAST runs FIRST. The session
# must be decoded before this can read it, hence the order below.
@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)
    if not request.session.get("user_id"):
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=auth.secret_key(),
    session_cookie="jobagent_session",
    max_age=load_config().web.session_hours * 3600,
    same_site="lax",
    https_only=bool(os.getenv("HTTPS_ONLY")),
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _render(request: Request, name: str, **context):
    context.setdefault("task", tasks.snapshot())
    context.setdefault("status_labels", STATUS_LABELS)
    context.setdefault("email", request.session.get("email"))
    return TEMPLATES.TemplateResponse(request=request, name=name, context=context)


def _current_user(session, request: Request) -> User:
    user = session.get(User, request.session.get("user_id") or 0)
    if user is None:
        # The row is gone (or the cookie predates a reset): make the caller
        # log in again rather than serving a half-broken page.
        raise HTTPException(status_code=401, detail="please log in again")
    return user


def _checkbox(value: str | None) -> bool:
    return str(value or "").lower() in {"on", "true", "1", "yes"}


def _task_key(search_id: int) -> str:
    return f"search:{search_id}"


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return _render(request, "register.html", error=None)


@app.post("/register")
def register(request: Request, email: str = Form(...), password: str = Form(...),
             confirm: str = Form(...)):
    if password != confirm:
        return _render(request, "register.html", error="The two passwords do not match.")
    config = load_config()
    with session_scope(config) as session:
        try:
            user = auth.create_user(session, email, password)
        except ValueError as exc:
            return _render(request, "register.html", error=str(exc))
        request.session["user_id"] = user.id
        request.session["email"] = user.email
    return RedirectResponse(url="/setup", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return _render(request, "login.html", error=None)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    with session_scope(load_config()) as session:
        user = auth.verify(session, email, password)
        if user is None:
            return _render(request, "login.html", error="Wrong email or password.")
        request.session["user_id"] = user.id
        request.session["email"] = user.email
        needs_resume = not user.has_resume
    return RedirectResponse(url="/setup" if needs_resume else "/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# --------------------------------------------------------------------------
# resume
# --------------------------------------------------------------------------

@app.get("/setup", response_class=HTMLResponse)
def setup(request: Request):
    with session_scope(load_config()) as session:
        user = _current_user(session, request)
        resume, filename, skills = user.resume, user.resume_filename, user.skills
    return _render(
        request,
        "setup.html",
        resume=resume,
        resume_filename=filename,
        skills=skills or {},
        formats=sorted(SUPPORTED_SUFFIXES),
        result=request.session.pop("ingest_result", None),
        error=request.session.pop("ingest_error", None),
    )


@app.post("/setup")
async def upload_resume(request: Request, file: UploadFile):
    config = load_config()
    data = await file.read()
    if not data:
        request.session["ingest_error"] = "That file was empty."
        return RedirectResponse(url="/setup", status_code=303)
    if len(data) > MAX_UPLOAD_BYTES:
        request.session["ingest_error"] = "Resume files should be under 8 MB."
        return RedirectResponse(url="/setup", status_code=303)

    try:
        result = parse_upload(file.filename or "resume.pdf", data, config)
        skills = derive_skills(result.resume)
        with session_scope(config) as session:
            user = _current_user(session, request)
            user.resume = result.resume
            user.skills = skills
            user.resume_filename = result.source_file
        request.session["ingest_result"] = {
            "engine": result.engine,
            "summary": result.summary_line,
            "warnings": result.warnings,
            "skills": len(skills),
            "file": result.source_file,
        }
    except Exception as exc:
        log.error("resume ingest failed: %s", exc, exc_info=True)
        request.session["ingest_error"] = str(exc)[:400]
    return RedirectResponse(url="/setup", status_code=303)


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    config = load_config()
    with session_scope(config) as session:
        user = _current_user(session, request)
        if not user.has_resume:
            return RedirectResponse(url="/setup", status_code=303)

        searches = session.scalars(
            select(Search)
            .where(Search.user_id == user.id)
            .order_by(Search.created_at.desc())
        ).all()
        decisions = dict(
            session.execute(
                select(UserJob.status, func.count(UserJob.id))
                .where(UserJob.user_id == user.id)
                .group_by(UserJob.status)
            ).all()
        )
        runs = session.scalars(
            select(RunLog)
            .where(RunLog.user_id == user.id)
            .order_by(RunLog.id.desc())
            .limit(6)
        ).all()
        cached = session.scalar(select(func.count()).select_from(Job)) or 0
        searches_left = max(0, DAILY_SEARCH_LIMIT - (user.searches_today or 0))

    return _render(
        request,
        "dashboard.html",
        searches=searches,
        decisions=decisions,
        runs=runs,
        cached=cached,
        searches_left=searches_left,
        cities=list(INDIA_CITIES),
        city_aliases=CITY_ALIASES,
    )


@app.get("/api/expand")
def api_expand(role: str, experience: BlankableInt = None):
    """Live preview of the related titles a role will actually be searched as."""
    expansion = expand_role(role, use_llm=False, experience_years=experience)
    return JSONResponse(
        {"role": expansion.role, "titles": expansion.titles, "keywords": expansion.keywords}
    )


@app.get("/api/task")
def api_task(key: str | None = None):
    return JSONResponse(tasks.snapshot(key))


# --------------------------------------------------------------------------
# searches
# --------------------------------------------------------------------------

def _start_live_search(user: User, search: Search) -> tasks.TaskState:
    """Kick off a live fetch for one search, in the background."""
    base = load_config()
    config = live_config(base, search, user.skills or {})
    user_id, search_id, label = user.id, search.id, search.role

    def work(note) -> dict:
        note(f"searching every source for {label}")
        stats = live(config, user_id=user_id, search_id=search_id, on_progress=note)
        with session_scope(base) as session:
            found = session.get(Search, search_id)
            if found is not None:
                found.last_run_at = utcnow()
                found.last_stats = stats.as_dict()
        return stats.as_dict()

    return tasks.submit(
        _task_key(search_id), "search", f"Searching for {label}", work
    )


@app.post("/searches")
def create_search_route(
    request: Request,
    role: str = Form(...),
    locations: list[str] = Form(default=[]),
    include_remote: str = Form(default=""),
    remote_only: str = Form(default=""),
    min_experience: int = Form(default=2),
    max_experience: int = Form(default=6),
):
    config = load_config()
    if not role.strip():
        return RedirectResponse(url="/?error=role", status_code=303)

    with session_scope(config) as session:
        user = _current_user(session, request)
        search = create_search(
            session,
            user.id,
            role,
            locations,
            include_remote=_checkbox(include_remote) or _checkbox(remote_only),
            remote_only=_checkbox(remote_only),
            min_experience_years=min_experience,
            max_experience_years=max_experience,
            config=config,
        )
        search_id = search.id
        if not auth.claim_search(user, DAILY_SEARCH_LIMIT):
            return RedirectResponse(
                url=f"/jobs?search_id={search_id}&limit=1", status_code=303
            )
        _start_live_search(user, search)

    return RedirectResponse(url=f"/jobs?search_id={search_id}&live=1", status_code=303)


@app.post("/searches/{search_id}/run")
def run_search_route(request: Request, search_id: int):
    with session_scope(load_config()) as session:
        user = _current_user(session, request)
        search = session.get(Search, search_id)
        if search is None or search.user_id != user.id:
            raise HTTPException(status_code=404, detail="search not found")
        if not auth.claim_search(user, DAILY_SEARCH_LIMIT):
            return RedirectResponse(
                url=f"/jobs?search_id={search_id}&limit=1", status_code=303
            )
        _start_live_search(user, search)
    return RedirectResponse(url=f"/jobs?search_id={search_id}&live=1", status_code=303)


@app.post("/searches/{search_id}/delete")
def delete_search(request: Request, search_id: int):
    with session_scope(load_config()) as session:
        user = _current_user(session, request)
        search = session.get(Search, search_id)
        if search is not None and search.user_id == user.id:
            session.delete(search)
    return RedirectResponse(url="/", status_code=303)


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

@app.get("/jobs", response_class=HTMLResponse)
def jobs_list(request: Request, search_id: BlankableInt = None, status: str = "",
              source: str = "", q: str = "", city: str = "",
              min_score: BlankableInt = None,
              page: BlankableInt = None, remote: BlankableInt = None,
              sort: str = "score", show_all: BlankableInt = None):
    config = load_config()
    page = max(1, page or 1)
    page_size = config.web.page_size

    with session_scope(config) as session:
        user = _current_user(session, request)
        searches = session.scalars(
            select(Search)
            .where(Search.user_id == user.id)
            .order_by(Search.created_at.desc())
        ).all()

        active = next((s for s in searches if s.id == search_id), None)
        if active is None:
            active = searches[0] if searches else None

        ranked = rank(session, user, active, config, include_filtered=bool(show_all))

        # Filters apply to the ranked list rather than the SQL, because the
        # things worth filtering on - the score, whether you already applied -
        # only exist once the rows have been scored for this user.
        if status:
            ranked = [r for r in ranked if r.status == status]
        if source:
            ranked = [r for r in ranked if r.job.source == source]
        if remote:
            ranked = [r for r in ranked if r.job.remote]
        if min_score:
            ranked = [r for r in ranked if r.score >= min_score]
        if q:
            # Role and employer only; where the job is has its own box, so
            # folding location in here would make the two fight each other.
            needle = q.lower()
            ranked = [
                r for r in ranked
                if needle in (r.job.title or "").lower()
                or needle in (r.job.company or "").lower()
            ]
        if city:
            wanted = [s.lower() for s in city_spellings(city)]
            ranked = [
                r for r in ranked
                if any(spelling in (r.job.location or "").lower() for spelling in wanted)
            ]
        if sort == "new":
            # Many sources never report a posting date, so fall back to when the
            # cache first saw the job rather than dropping those rows to the end.
            ranked.sort(
                key=lambda r: (r.job.posted_at or r.job.first_seen_at),
                reverse=True,
            )

        total = len(ranked)
        window = ranked[(page - 1) * page_size : page * page_size]
        sources = sorted({r.job.source for r in ranked})
        task = tasks.snapshot(_task_key(active.id) if active else None)

    return _render(
        request,
        "jobs.html",
        ranked=window,
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, (total + page_size - 1) // page_size),
        sources=sources,
        searches=searches,
        active_search=active,
        statuses=list(STATUS_LABELS),
        # Both spellings are offered because both appear in real postings, and
        # either one finds the same jobs.
        city_options=sorted({*INDIA_CITIES, *CITY_ALIASES} - {"India (anywhere)"}),
        task=task,
        live=request.query_params.get("live"),
        limit_hit=request.query_params.get("limit"),
        filters={
            "search_id": active.id if active else "", "status": status,
            "source": source, "q": q, "city": city, "min_score": min_score,
            "remote": remote, "sort": sort, "show_all": show_all,
        },
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int, search_id: BlankableInt = None):
    config = load_config()
    with session_scope(config) as session:
        user = _current_user(session, request)
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        search = session.get(Search, search_id) if search_id else None
        if search is not None and search.user_id != user.id:
            search = None
        if search is None:
            search = session.scalar(
                select(Search)
                .where(Search.user_id == user.id)
                .order_by(Search.created_at.desc())
            )
        entry = score_one(session, user, search, job, config)
    return _render(request, "job.html", entry=entry, statuses=list(STATUS_LABELS))


@app.post("/jobs/{job_id}/status")
def set_status(request: Request, job_id: int, status: str = Form(...),
               notes: str = Form("")):
    with session_scope(load_config()) as session:
        user = _current_user(session, request)
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        entry = session.scalar(
            select(UserJob).where(UserJob.user_id == user.id, UserJob.job_id == job_id)
        )
        if entry is None:
            entry = UserJob(user_id=user.id, job_id=job_id)
            session.add(entry)
        entry.status = status
        entry.updated_at = utcnow()
        if status == JobStatus.APPLIED.value and not entry.applied_at:
            entry.applied_at = utcnow()
        if notes:
            entry.notes = notes[:2000]
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.get("/health")
def health():
    """Cheap enough to be hit by an uptime pinger every few minutes."""
    with session_scope(load_config()) as session:
        jobs = session.scalar(select(func.count()).select_from(Job)) or 0
        users = session.scalar(select(func.count()).select_from(User)) or 0
    return {"status": "ok", "jobs": jobs, "users": users}


def serve(host: str | None = None, port: int | None = None, reload: bool = False) -> None:
    import uvicorn

    config = load_config()
    uvicorn.run(
        "jobagent.web.app:app" if reload else app,
        # A container gets its port from the environment and must listen on all
        # interfaces; the config default only makes sense on a laptop.
        host=host or os.getenv("HOST") or config.web.host,
        port=port or int(os.getenv("PORT") or config.web.port),
        reload=reload,
        log_level="info",
    )
