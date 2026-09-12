"""Migrate a single-user database to the multi-tenant schema.

The cached openings are worth keeping - they cost hours of polite crawling - but
the columns that made them single-user have to go: a score or an application
status on the posting itself cannot mean anything once more than one person is
reading it.

So the postings stay, the per-user columns are dropped, and the new tables are
created alongside. Run once; it is safe to run again.

    python scripts/migrate_to_multiuser.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text  # noqa: E402

from jobagent.config import load_config  # noqa: E402
from jobagent.db import get_engine, init_db  # noqa: E402

# Everything that described one person's relationship to a posting.
PER_USER_COLUMNS = [
    "status", "skip_reason", "score", "score_detail", "matched_skills",
    "missing_skills", "jd_skills", "required_experience_min",
    "required_experience_max", "applied_at", "notes", "search_ids",
    "resume_paths", "cover_letter_path", "tailor_engine", "tailor_warnings",
    "tailored_at",
]


def main() -> int:
    config = load_config()
    engine = get_engine(config)
    inspector = inspect(engine)

    tables = inspector.get_table_names()
    if "jobs" not in tables:
        init_db(config)
        print("fresh database created; nothing to migrate")
        return 0

    existing = {column["name"] for column in inspector.get_columns("jobs")}
    doomed = [name for name in PER_USER_COLUMNS if name in existing]

    # SQLite refuses to drop a column an index still mentions, so every index on
    # the table goes first. init_db recreates the ones the model asks for.
    indexes = [index["name"] for index in inspector.get_indexes("jobs") if index.get("name")]

    with engine.begin() as connection:
        kept = connection.execute(text("SELECT count(*) FROM jobs")).scalar() or 0
        for name in indexes:
            connection.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
        for name in doomed:
            # SQLite rebuilds the table for each drop, so this is slow but
            # avoids hand-writing a copy-and-swap that could lose rows.
            connection.execute(text(f'ALTER TABLE jobs DROP COLUMN "{name}"'))
            print(f"dropped jobs.{name}")
        if "saved_searches" in tables:
            connection.execute(text("DROP TABLE saved_searches"))
            print("dropped saved_searches (replaced by per-user searches)")
        if "run_logs" in tables:
            # Only a log of past fetches, and it now records which user asked.
            # Recreating it is cheaper and clearer than adding the column.
            connection.execute(text("DROP TABLE run_logs"))
            print("dropped run_logs (recreated with a user column)")

    init_db(config)
    print(f"migration done; {kept} cached openings kept")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
