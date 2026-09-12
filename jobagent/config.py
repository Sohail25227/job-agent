"""Configuration loading.

All tunables live in ``config/config.yaml``; secrets live in ``.env``. Relative
paths in the config are resolved against the project root so the CLI works
from any working directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


class SearchConfig(BaseModel):
    titles: list[str] = Field(default_factory=list)
    must_have_any: list[str] = Field(default_factory=list)
    exclude_title_keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    min_experience_years: int = 0
    max_experience_years: int = 15
    posted_within_days: int = 21
    min_score: int = 55


class BoardSource(BaseModel):
    enabled: bool = False
    boards: list[str] = Field(default_factory=list)


class CompanySource(BaseModel):
    enabled: bool = False
    companies: list[str] = Field(default_factory=list)


class QuerySource(BaseModel):
    enabled: bool = False
    queries: list[str] = Field(default_factory=list)


class SearchQuerySource(BaseModel):
    enabled: bool = False
    queries: list[dict[str, str]] = Field(default_factory=list)
    max_pages: int = 2
    # Search results carry no job text, so each JD is a separate request and is
    # also what the scorer needs. Results come back by relevance, so a budget
    # spends itself on the best cards first and drops the tail.
    descriptions_per_query: int = 25


class FeedSource(BaseModel):
    """A public JSON/RSS feed that needs no key and no query parameters."""

    enabled: bool = False
    max_pages: int = 1
    limit: int = 100
    filters: list[str] = Field(default_factory=list)


class WorkdayTenant(BaseModel):
    """Workday career sites are addressed by tenant + host + site name.

    For ``https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite``
    that is tenant=nvidia, host=nvidia.wd5.myworkdayjobs.com,
    site=NVIDIAExternalCareerSite.
    """

    tenant: str
    host: str
    site: str
    label: str | None = None


class WorkdaySource(BaseModel):
    enabled: bool = False
    tenants: list[WorkdayTenant] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    max_pages: int = 2
    # Each JD body is a separate request, and scoring is worthless without one,
    # so this caps the cost per employer rather than globally.
    descriptions_per_tenant: int = 25
    # Tenants are independent hosts, so they can be scanned at the same time.
    # Sequential scanning made Workday slower than every other source together.
    concurrency: int = 8


class QueryFeedSource(BaseModel):
    """An aggregator that takes a keyword and a city but needs no API key.

    Shaped like :class:`KeyedQuerySource` minus the credentials, because that is
    the only real difference: the queries still come from the saved search.
    """

    enabled: bool = False
    queries: list[dict[str, str]] = Field(default_factory=list)
    # Deliberately shallow. These are unauthenticated endpoints, so the polite
    # ceiling is low and a 429 costs the whole source, not one page.
    max_pages: int = 2
    filters: list[str] = Field(default_factory=list)


class KeyedQuerySource(BaseModel):
    """Aggregators with a free API key: Adzuna, Jooble, Careerjet."""

    enabled: bool = False
    api_key_env: str | None = None
    app_id_env: str | None = None
    country: str = "in"
    queries: list[dict[str, str]] = Field(default_factory=list)
    max_pages: int = 2
    results_per_page: int = 50

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env or "") or None

    @property
    def app_id(self) -> str | None:
        return os.environ.get(self.app_id_env or "") or None

    @property
    def credentials_present(self) -> bool:
        needs_id = bool(self.app_id_env)
        return bool(self.api_key) and (not needs_id or bool(self.app_id))


class SourcesConfig(BaseModel):
    # Company-addressed ATS boards
    greenhouse: BoardSource = BoardSource()
    lever: BoardSource = BoardSource()
    ashby: BoardSource = BoardSource()
    smartrecruiters: CompanySource = CompanySource()
    workable: CompanySource = CompanySource()
    recruitee: CompanySource = CompanySource()
    workday: WorkdaySource = WorkdaySource()
    # Keyword-addressed aggregators
    linkedin: SearchQuerySource = SearchQuerySource()
    naukri: SearchQuerySource = SearchQuerySource()
    adzuna: KeyedQuerySource = KeyedQuerySource()
    jooble: KeyedQuerySource = KeyedQuerySource()
    themuse: QueryFeedSource = QueryFeedSource()
    jobdataapi: QueryFeedSource = QueryFeedSource()
    # Remote-first feeds
    remotive: QuerySource = QuerySource()
    remoteok: FeedSource = FeedSource()
    arbeitnow: FeedSource = FeedSource()
    himalayas: FeedSource = FeedSource()
    jobicy: FeedSource = FeedSource()
    weworkremotely: FeedSource = FeedSource()


class MatchingWeights(BaseModel):
    skills: float = 55
    title: float = 15
    experience: float = 10
    location: float = 12
    recency: float = 8


class MatchingConfig(BaseModel):
    weights: MatchingWeights = MatchingWeights()
    my_skills: dict[str, float] = Field(default_factory=dict)


class ResumeConfig(BaseModel):
    base: str = "config/resume_base.yaml"
    profile: str = "config/profile.yaml"
    output_dir: str = "data/resumes"
    formats: list[str] = Field(default_factory=lambda: ["md", "docx", "pdf"])
    cover_letter: bool = True

    @property
    def base_path(self) -> Path:
        return resolve_path(self.base)

    @property
    def profile_path(self) -> Path:
        return resolve_path(self.profile)

    @property
    def output_path(self) -> Path:
        return resolve_path(self.output_dir)


class LLMProvider(BaseModel):
    name: str
    model: str
    base_url: str | None = None
    api_key_env: str | None = None

    @property
    def api_key(self) -> str | None:
        """Local providers need no key; the OpenAI SDK still wants a non-empty one."""
        if self.api_key_env is None:
            return "local"
        return os.environ.get(self.api_key_env) or None


class LLMConfig(BaseModel):
    enabled: bool = True
    providers: list[LLMProvider] = Field(default_factory=list)
    temperature: float = 0.2
    max_tokens: int = 4000
    timeout_seconds: int = 120
    # Free tiers rate-limit aggressively; pacing calls is cheaper than waiting
    # out a 60-second 429 backoff on every second job.
    delay_between_calls_seconds: float = 4.0


class ScheduleConfig(BaseModel):
    cron: str = "30 9 * * *"
    timezone: str = "Asia/Kolkata"


class StorageConfig(BaseModel):
    database_url: str = "sqlite:///data/jobs.db"

    def resolved_url(self) -> str:
        """The URL to actually connect to.

        ``DATABASE_URL`` wins, because that is what every host injects. Managed
        Postgres providers still hand out ``postgres://``, which SQLAlchemy 2
        dropped, so normalise the scheme rather than making deployment fail on a
        detail nobody controls.
        """
        url = os.getenv("DATABASE_URL") or self.database_url
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://") :]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://") :]

        prefix = "sqlite:///"
        if url.startswith(prefix):
            # Absolute paths keep the DB stable across working directories.
            path = resolve_path(url[len(prefix) :])
            path.parent.mkdir(parents=True, exist_ok=True)
            return f"{prefix}{path}"
        return url

    @property
    def is_sqlite(self) -> bool:
        return self.resolved_url().startswith("sqlite")


class HttpConfig(BaseModel):
    user_agent: str = "job-agent/1.0"
    timeout_seconds: int = 30
    max_retries: int = 3
    min_delay_seconds: float = 1.5
    # Corporate networks intercept TLS with an internal root CA that lives in
    # the OS keychain rather than in certifi.
    use_system_certs: bool = True
    ca_bundle: str | None = None


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    session_hours: int = 72
    upload_dir: str = "data/uploads"
    page_size: int = 40

    @property
    def upload_path(self) -> Path:
        return resolve_path(self.upload_dir)


class Config(BaseModel):
    search: SearchConfig = SearchConfig()
    sources: SourcesConfig = SourcesConfig()
    matching: MatchingConfig = MatchingConfig()
    resume: ResumeConfig = ResumeConfig()
    llm: LLMConfig = LLMConfig()
    web: WebConfig = WebConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    storage: StorageConfig = StorageConfig()
    http: HttpConfig = HttpConfig()


_cached: Config | None = None


def load_config(path: str | Path | None = None, *, refresh: bool = False) -> Config:
    global _cached
    if _cached is not None and not refresh and path is None:
        return _cached

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config not found at {config_path}. Run `python -m jobagent.cli init` first."
        )
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text()) or {}
    config = Config.model_validate(raw)
    if path is None:
        _cached = config
    return config


def load_yaml(path: str | Path) -> dict[str, Any]:
    resolved = resolve_path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"Expected YAML file at {resolved}")
    return yaml.safe_load(resolved.read_text()) or {}
