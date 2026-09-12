"""Command line interface.

    python -m jobagent.cli init                  # one-time setup
    python -m jobagent.cli doctor                # check everything is wired up
    python -m jobagent.cli serve                 # dashboard on localhost:8000
    python -m jobagent.cli crawl                 # warm the shared cache (cron)
    python -m jobagent.cli users                 # who has an account
    python -m jobagent.cli schedule              # daily cron, stays in the foreground

Day-to-day use is the dashboard; searching is per user and needs a logged-in
account, so it has no CLI equivalent. ``crawl`` is what a scheduler calls.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from sqlalchemy import func, select

from .config import PROJECT_ROOT, load_config
from .db import init_db, session_scope
from .fetch import backfill_descriptions, crawl
from .models import Job, Search, User
from .sources import ALL_SOURCES

app = typer.Typer(
    add_completion=False,
    help="Find every opening worth your time, on every board. You apply.",
)
console = Console()


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=verbose)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    _setup_logging(verbose)


# --------------------------------------------------------------------------

@app.command()
def init() -> None:
    """Create data directories, copy example configs, initialise the database."""
    for relative in ("data", "data/resumes", "data/uploads"):
        (PROJECT_ROOT / relative).mkdir(parents=True, exist_ok=True)

    copied = []
    for example, target in (
        ("config/profile.example.yaml", "config/profile.yaml"),
        (".env.example", ".env"),
    ):
        source_path, target_path = PROJECT_ROOT / example, PROJECT_ROOT / target
        if source_path.exists() and not target_path.exists():
            shutil.copy(source_path, target_path)
            copied.append(target)

    init_db(load_config())

    console.print("[green]✓[/] database ready")
    for name in copied:
        console.print(f"[green]✓[/] created {name}")
    console.print()
    console.print("[bold]Next:[/]")
    console.print("  1. [cyan]python -m jobagent.cli serve[/] and open http://127.0.0.1:8000")
    console.print("  2. Create an account, upload your resume, describe the role you want.")


@app.command("resume")
def resume_command(
    path: Path = typer.Argument(..., help="Your resume: PDF, DOCX, TXT or MD."),
    email: str = typer.Option(None, "--email", "-e", help="Attach it to this account."),
) -> None:
    """Parse a resume. With --email, store it against that account."""
    from .matching.skills import derive_skills
    from .resume.ingest import ingest_file, parse_upload

    config = load_config()
    if not email:
        result = ingest_file(path, config)
        console.print(f"[green]✓[/] parsed with {result.engine} — {result.summary_line}")
        console.print(f"  written to [cyan]{config.resume.base_path.relative_to(PROJECT_ROOT)}[/]")
    else:
        result = parse_upload(path.name, path.read_bytes(), config)
        skills = derive_skills(result.resume)
        init_db(config)
        with session_scope(config) as session:
            user = session.scalar(select(User).where(User.email == email.strip().lower()))
            if user is None:
                console.print(f"[red]no account for {email}[/]")
                raise typer.Exit(code=1)
            user.resume = result.resume
            user.skills = skills
            user.resume_filename = result.source_file
        console.print(
            f"[green]✓[/] parsed with {result.engine} — {result.summary_line}, "
            f"{len(skills)} skills, stored for {email}"
        )
    for warning in result.warnings:
        console.print(f"  [yellow]note:[/] {warning}")


@app.command()
def doctor() -> None:
    """Check config, database, sources and LLM availability."""
    from .resume.llm import select_provider

    config = load_config()
    table = Table(show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")

    def add(name: str, ok: bool | None, detail: str) -> None:
        icon = "[green]ok[/]" if ok else ("[yellow]warn[/]" if ok is None else "[red]missing[/]")
        table.add_row(name, icon, detail)

    resume_path = config.resume.base_path
    add(
        "master resume",
        resume_path.exists(),
        str(resume_path.relative_to(PROJECT_ROOT)) if resume_path.exists()
        else "upload one in the dashboard, or `cli resume <file.pdf>`",
    )
    profile_path = config.resume.profile_path
    add("profile", profile_path.exists(), str(profile_path.relative_to(PROJECT_ROOT)))

    enabled = [
        cls.name for cls in ALL_SOURCES
        if getattr(config.sources, cls.name, None) and getattr(config.sources, cls.name).enabled
    ]
    add("sources enabled", bool(enabled), f"{len(enabled)}: " + ", ".join(enabled))
    add(
        "workday tenants",
        bool(config.sources.workday.tenants),
        f"{len(config.sources.workday.tenants)} employers",
    )

    for name, settings in (("adzuna", config.sources.adzuna), ("jooble", config.sources.jooble)):
        if not settings.enabled:
            add(f"{name} key", None, "source disabled")
        else:
            add(
                f"{name} key",
                settings.credentials_present,
                "present" if settings.credentials_present
                else "enabled but no API key in .env, so it will be skipped",
            )

    provider = select_provider(config)
    add(
        "llm",
        True if provider else None,
        f"{provider.name}:{provider.model}" if provider
        else "none reachable - role expansion falls back to the built-in list",
    )

    url = config.storage.resolved_url()
    add(
        "database",
        True,
        "sqlite (local file)" if config.storage.is_sqlite
        else f"postgres at {url.split('@')[-1].split('/')[0]}",
    )
    add(
        "session key",
        bool(os.getenv("SECRET_KEY")),
        "from SECRET_KEY" if os.getenv("SECRET_KEY")
        else "generated locally - set SECRET_KEY before deploying",
    )
    add("auto apply", None, "not supported by design - you apply on the employer's site")

    console.print(table)


@app.command("sources")
def sources_command() -> None:
    """List every source and whether it is switched on."""
    config = load_config()
    table = Table(show_header=True, header_style="bold")
    table.add_column("source")
    table.add_column("kind")
    table.add_column("enabled")
    table.add_column("targets", justify="right")
    for cls in ALL_SOURCES:
        settings = getattr(config.sources, cls.name, None)
        if settings is None:
            continue
        targets = (
            getattr(settings, "boards", None)
            or getattr(settings, "companies", None)
            or getattr(settings, "tenants", None)
            or getattr(settings, "queries", None)
            or []
        )
        table.add_row(
            cls.name,
            cls.ats or "aggregator",
            "[green]yes[/]" if settings.enabled else "[dim]no[/]",
            str(len(targets)),
        )
    console.print(table)


@app.command("crawl")
def crawl_command(
    source: list[str] = typer.Option(None, "--source", "-s", help="Limit to specific sources."),
    backfill: int = typer.Option(60, "--backfill", help="Descriptions to fill in afterwards."),
) -> None:
    """Warm the shared cache. This is what a scheduler should call.

    Covers the union of every user's active searches, plus the boards addressed
    by company slug that no interactive search can reach.
    """
    stats = crawl(load_config(), only=list(source) if source else None, backfill=backfill)
    console.print(
        f"[green]{stats.discovered}[/] seen, [green]{stats.new}[/] new "
        f"in {stats.seconds}s across {len(stats.sources_ok)} sources"
    )
    for name, error in stats.sources_failed.items():
        console.print(f"  [yellow]{name}[/]: {error}")


@app.command("backfill")
def backfill_command(
    limit: int = typer.Option(100, "--limit", "-n"),
) -> None:
    """Fetch full descriptions for cached postings that have none."""
    filled = backfill_descriptions(load_config(), limit=limit)
    console.print(f"[green]{filled}[/] descriptions filled in")


@app.command("users")
def users_command() -> None:
    """List accounts, and whether each has a usable resume."""
    config = load_config()
    init_db(config)
    with session_scope(config) as session:
        users = session.scalars(select(User).order_by(User.id)).all()
        table = Table(show_header=True, header_style="bold")
        for column in ("id", "email", "resume", "skills", "searches", "joined"):
            table.add_column(column)
        for user in users:
            searches = session.scalar(
                select(func.count()).select_from(Search).where(Search.user_id == user.id)
            ) or 0
            table.add_row(
                str(user.id),
                user.email,
                "[green]yes[/]" if user.has_resume else "[yellow]no[/]",
                str(len(user.skills or {})),
                str(searches),
                user.created_at.strftime("%d %b %Y"),
            )
    console.print(table)


@app.command("stats")
def stats_command() -> None:
    """Size of the shared cache, by source."""
    config = load_config()
    init_db(config)
    with session_scope(config) as session:
        rows = session.execute(
            select(Job.source, func.count(Job.id)).group_by(Job.source)
        ).all()
        total = session.scalar(select(func.count()).select_from(Job)) or 0
        described = session.scalar(
            select(func.count()).select_from(Job).where(
                Job.description.is_not(None), Job.description != ""
            )
        ) or 0
    table = Table(show_header=True, header_style="bold")
    table.add_column("source")
    table.add_column("openings", justify="right")
    for source, count in sorted(rows, key=lambda pair: -pair[1]):
        table.add_row(source, str(count))
    console.print(table)
    console.print(f"{total} cached, {described} with a full description")


@app.command("serve")
def serve_command(
    host: str = typer.Option(os.environ.get("JOBAGENT_HOST", "127.0.0.1")),
    port: int = typer.Option(int(os.environ.get("JOBAGENT_PORT", "8000"))),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Start the dashboard."""
    from .web.app import serve

    console.print(f"dashboard on [cyan]http://{host}:{port}[/]")
    serve(host=host, port=port, reload=reload)


@app.command("schedule")
def schedule_command() -> None:
    """Run on a daily cron in the foreground."""
    from .scheduler import start

    start(load_config())


if __name__ == "__main__":
    app()
