"""Engine/session plumbing and job upserts.

Runs on SQLite locally and on managed Postgres when deployed; the only
difference that leaks out is the connect-time tuning below.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Config, load_config
from .models import Base, Job, utcnow

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine(config: Config | None = None) -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        config = config or load_config()
        url = config.storage.resolved_url()
        kwargs: dict = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            # A fetch writes for a while and the dashboard wants to read at the
            # same time; background threads need check_same_thread off.
            kwargs["connect_args"] = {"timeout": 30, "check_same_thread": False}
        else:
            # Free Postgres tiers cap connections hard, and idle ones are killed
            # from the server side, so stay small and recycle before they do.
            kwargs.update(pool_size=5, max_overflow=2, pool_recycle=280)
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _configure_sqlite(connection, _record):  # noqa: ANN001
                cursor = connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db(config: Config | None = None) -> None:
    Base.metadata.create_all(get_engine(config))


def reset_engine() -> None:
    """Drop the cached engine. Only needed by tests that switch databases."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


@contextmanager
def session_scope(config: Config | None = None) -> Iterator[Session]:
    get_engine(config)
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def upsert_job(session: Session, posting) -> tuple[Job, bool]:
    """Insert a posting, or refresh the mutable parts of one already cached.

    The cache is shared, so this only ever touches facts about the posting -
    there is no per-user state here to preserve.
    """
    existing = session.scalar(
        select(Job).where(Job.source == posting.source, Job.external_id == posting.external_id)
    )
    if existing:
        existing.last_seen_at = utcnow()
        existing.title = posting.title or existing.title
        existing.location = posting.location or existing.location
        existing.apply_url = posting.apply_url or existing.apply_url
        # Keep the fuller text: list endpoints return a snippet, detail endpoints
        # the whole description, and either may arrive first.
        if posting.description and len(posting.description) > len(existing.description or ""):
            existing.description = posting.description
        if posting.posted_at and not existing.posted_at:
            existing.posted_at = posting.posted_at
        return existing, False

    job = Job(
        source=posting.source,
        external_id=posting.external_id,
        ats=posting.ats,
        company=posting.company,
        title=posting.title,
        location=posting.location,
        remote=posting.remote,
        url=posting.url,
        apply_url=posting.apply_url,
        description=posting.description,
        posted_at=posting.posted_at,
    )
    session.add(job)
    return job, True
