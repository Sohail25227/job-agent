"""Check the schema and the hot queries against the Postgres dialect.

Deployment uses Postgres while development uses SQLite, and the two disagree
about enough things - JSON handling, boolean literals, LIMIT with parameters -
that "it worked locally" is not evidence. This compiles the real DDL and the
real read-path queries for Postgres without needing a server to connect to.

    python scripts/check_postgres_ready.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from jobagent.config import load_config  # noqa: E402
from jobagent.models import Base, Job, Search, User, UserJob  # noqa: E402

ok = fail = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  ok   {label}" + (f" — {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    dialect = postgresql.dialect()

    print("\n1. schema compiles for Postgres")
    for table in Base.metadata.sorted_tables:
        try:
            ddl = str(CreateTable(table).compile(dialect=dialect))
            for index in table.indexes:
                str(CreateIndex(index).compile(dialect=dialect))
            check(f"table {table.name}", "CREATE TABLE" in ddl,
                  f"{len(table.columns)} columns")
        except Exception as exc:
            check(f"table {table.name}", False, str(exc)[:120])

    print("\n2. the read path compiles for Postgres")
    from jobagent.results import _location_clauses, _title_clauses

    config = load_config()
    config.search.titles = ["Backend Engineer", "SDE II"]
    config.search.must_have_any = ["java", "spring"]
    config.search.locations = ["Gurugram", "Noida"]
    config.search.remote_ok = True

    from sqlalchemy import or_

    queries = {
        "candidate lookup": select(Job)
        .where(or_(*_title_clauses(config)))
        .where(or_(*_location_clauses(config)))
        .order_by(Job.last_seen_at.desc())
        .limit(1500),
        "user decisions": select(UserJob).where(UserJob.user_id == 1),
        "user searches": select(Search).where(Search.user_id == 1),
        "login lookup": select(User).where(User.email == "a@b.com"),
    }
    for label, query in queries.items():
        try:
            sql = str(query.compile(dialect=dialect,
                                    compile_kwargs={"literal_binds": True}))
            check(label, bool(sql), f"{len(sql)} chars of SQL")
        except Exception as exc:
            check(label, False, str(exc)[:160])

    print("\n3. connection strings are normalised")
    cases = [
        ("postgres://u:p@host/db", "postgresql+psycopg://"),
        ("postgresql://u:p@host/db", "postgresql+psycopg://"),
        ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://"),
    ]
    original = os.environ.get("DATABASE_URL")
    try:
        for given, expected in cases:
            os.environ["DATABASE_URL"] = given
            resolved = load_config().storage.resolved_url()
            check(f"{given.split('://')[0]}:// accepted",
                  resolved.startswith(expected), resolved.split("@")[0] + "@...")
        os.environ.pop("DATABASE_URL", None)
        check("no DATABASE_URL falls back to sqlite",
              load_config().storage.is_sqlite)
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original

    print("\n4. the driver is installed")
    try:
        import psycopg  # noqa: F401

        check("psycopg importable", True, "Postgres deploys will connect")
    except ImportError:
        check("psycopg importable", False, "pip install -r requirements.txt")

    print(f"\n{ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
