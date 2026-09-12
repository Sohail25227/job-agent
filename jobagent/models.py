"""Database models.

The shape is deliberately multi-tenant with a single shared job cache:

* ``Job`` is one row per opening that exists in the world. It carries no score
  and no status, because "is this a good job" is a question about a person, not
  about the posting. One user's search warms the cache for everyone.
* ``User`` owns a resume and its derived skill weights.
* ``Search`` is one user's role + cities.
* ``UserJob`` exists only for openings a user actually acted on, so it stays
  small however large the cache grows.

Scores are computed at read time from the user's resume rather than stored. That
keeps the cache shared, makes a resume edit re-rank everything instantly, and
removes a whole rescoring stage that could otherwise go stale.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class JobStatus(str, enum.Enum):
    """What a user did about an opening. Absence of a row means "untouched"."""

    SAVED = "saved"        # shortlisted to apply to
    APPLIED = "applied"    # applied on the employer's site
    REJECTED = "rejected"  # you passed, or they did
    HIDDEN = "hidden"      # not interested, stop showing it


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    salt: Mapped[str] = mapped_column(String(64))

    # The structured resume, and the skill weights derived from it. Cached here
    # so scoring a page of results costs no parsing.
    resume: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    skills: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resume_filename: Mapped[str | None] = mapped_column(String(300), nullable=True)

    min_experience_years: Mapped[int] = mapped_column(Integer, default=0)
    max_experience_years: Mapped[int] = mapped_column(Integer, default=40)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Live searches hit rate-limited third-party APIs on a key shared by every
    # user, so each account gets a daily allowance.
    searches_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    searches_today: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def has_resume(self) -> bool:
        return bool(self.resume)


class Job(Base):
    """One opening, shared across all users. No per-user fields belong here."""

    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_job_source_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    source: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    ats: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    company: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(300))
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    remote: Mapped[bool] = mapped_column(Boolean, default=False)

    url: Mapped[str] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    @property
    def best_link(self) -> str:
        return self.apply_url or self.url

    @property
    def age_days(self) -> int | None:
        if not self.posted_at:
            return None
        posted = self.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return (utcnow() - posted).days


class Search(Base):
    """A role + location combination one user cares about."""

    __tablename__ = "searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    role: Mapped[str] = mapped_column(String(160))
    titles: Mapped[list | None] = mapped_column(JSON, nullable=True)
    keywords: Mapped[list | None] = mapped_column(JSON, nullable=True)
    locations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    include_remote: Mapped[bool] = mapped_column(Boolean, default=True)
    remote_only: Mapped[bool] = mapped_column(Boolean, default=False)
    min_experience_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_experience_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expansion_source: Mapped[str | None] = mapped_column(String(80), nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    @property
    def location_label(self) -> str:
        locations = self.locations or []
        if self.remote_only:
            return "Remote only"
        label = ", ".join(locations[:3])
        if len(locations) > 3:
            label += f" +{len(locations) - 3}"
        return label + (" · remote ok" if self.include_remote else "")


class UserJob(Base):
    """One user's decision about one opening. Only written when they act."""

    __tablename__ = "user_jobs"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_user_job"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)

    status: Mapped[str] = mapped_column(String(20), default=JobStatus.SAVED.value, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunLog(Base):
    """One row per fetch, for the dashboard and for debugging."""

    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    search_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
