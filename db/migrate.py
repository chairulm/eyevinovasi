"""Apply db/migrations/*.sql in order, once each (tracked in schema_migrations).

  DATABASE_URL=postgresql:///cctvmap python -m db.migrate
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
log = logging.getLogger("migrate")


def migrate(url: str) -> list[str]:
    applied_now = []
    with psycopg.connect(url, autocommit=False) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.stem in done:
                continue
            log.info("applying %s", f.name)
            conn.execute(f.read_text())
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.stem,))
            applied_now.append(f.stem)
        conn.commit()
    return applied_now


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    url = os.getenv("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set")
    done = migrate(url)
    log.info("migrations applied: %s", ", ".join(done) or "none (up to date)")


if __name__ == "__main__":
    main()
