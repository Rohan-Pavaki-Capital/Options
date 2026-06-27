"""
Database setup — creates tables in NeonDB.

Run explicitly:
    python -m database.setup

Also called automatically by save_extraction() on first invocation
(idempotent — uses CREATE TABLE IF NOT EXISTS).
"""
import sys
from pathlib import Path

from database.connection import get_connection

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_schema_ensured = False


def ensure_schema() -> None:
    """Idempotently create tables if missing. Cached per process."""
    global _schema_ensured
    if _schema_ensured:
        return
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
    _schema_ensured = True


def run_setup() -> None:
    print("Creating database schema in NeonDB...")
    ensure_schema()
    print("Schema ready: tables 'extractions' and 'plans' exist.")


if __name__ == "__main__":
    try:
        run_setup()
    except Exception as e:
        sys.exit(f"ERROR: {e}")
