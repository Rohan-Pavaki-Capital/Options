"""
Durable result cache for the GET /api/excel/options endpoint.

Why this exists: Render's free plan gives the container an EPHEMERAL filesystem —
it is wiped on every deploy and every ~15-minute idle spin-down — so the local
on-disk `cache` module (.cache/) is cold most of the time there. This module
stores the endpoint's result in NeonDB/Postgres instead, which survives
restarts, deploys and spin-downs, so identical requests skip the LLM for real.

Robustness: the database is best-effort. If the DSN is missing or the DB is
unreachable, every call falls back to the local on-disk `cache` module, so the
endpoint NEVER breaks and still benefits from within-instance caching. A cache
miss simply means the caller re-runs the pipeline.

Value stored = the endpoint's JSON payload dict; a TTL (seconds) is enforced on
read. Only SUCCESSFUL payloads should be stored (the caller decides that).
"""

import os
import time

import cache as _disk   # existing SHA-256 on-disk cache (fallback + accelerator)

_NS = "excel_options"          # disk-cache namespace (fallback)
_TABLE = "excel_options_cache"  # Postgres table
_schema_ready = False           # per-process guard for CREATE TABLE


def _dsn():
    """Resolve a Postgres DSN from any of the env vars this project / Render use.
    connection.py reads db_string/DB_STRING; render.yaml sets DATABASE_URL — so
    we check all three. Returns None if none are set (-> disk fallback)."""
    for k in ("db_string", "DB_STRING", "DATABASE_URL"):
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip().strip('"').strip("'")
    return None


def _connect():
    """Open a psycopg connection, or return None if unavailable. Never raises."""
    dsn = _dsn()
    if not dsn:
        return None
    try:
        import psycopg
        return psycopg.connect(dsn)
    except Exception:
        return None


def _ensure_schema(conn):
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                    cache_key  TEXT PRIMARY KEY,
                    cached_at  DOUBLE PRECISION NOT NULL,
                    payload    JSONB NOT NULL
                )"""
        )
    conn.commit()
    _schema_ready = True


def _key(parts):
    # Reuse the disk cache's SHA-256 hashing so DB + disk keys stay identical.
    return _disk._hash(*parts)


def get(parts, ttl):
    """Return the cached payload dict (fresh within `ttl` seconds) or None.

    DB is authoritative when reachable: a DB hit/miss is returned as-is and the
    disk is NOT consulted. The disk fallback is used ONLY when the DB is
    unavailable. ttl <= 0 means 'never expires'."""
    k = _key(parts)
    conn = _connect()
    if conn is not None:
        try:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT cached_at, payload FROM {_TABLE} WHERE cache_key = %s",
                    (k,),
                )
                row = cur.fetchone()
            if row is None:
                return None                      # DB reachable, real miss
            cached_at, payload = row[0], row[1]
            if ttl > 0 and (time.time() - cached_at) > ttl:
                return None                      # expired
            return payload if isinstance(payload, dict) else None
        except Exception:
            pass                                 # DB error -> disk fallback
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ── DB unavailable: local disk fallback (per-instance, ephemeral) ──
    entry = _disk.get(_NS, *parts)
    if isinstance(entry, dict):
        payload = entry.get("payload")
        ts = entry.get("_cached_at", 0)
        if isinstance(payload, dict) and (ttl <= 0 or (time.time() - ts) <= ttl):
            return payload
    return None


def set(parts, payload):
    """Store `payload` durably in Postgres AND in the local disk cache. Both are
    best-effort — failures never raise."""
    k = _key(parts)
    now = time.time()

    conn = _connect()
    if conn is not None:
        try:
            _ensure_schema(conn)
            from psycopg.types.json import Jsonb
            with conn.cursor() as cur:
                cur.execute(
                    f"""INSERT INTO {_TABLE} (cache_key, cached_at, payload)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (cache_key)
                        DO UPDATE SET cached_at = EXCLUDED.cached_at,
                                      payload   = EXCLUDED.payload""",
                    (k, now, Jsonb(payload)),
                )
            conn.commit()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # Also write the disk cache: a cheap within-instance accelerator that still
    # serves hits if the DB later becomes unreachable mid-instance.
    try:
        _disk.set(_NS, {"_cached_at": now, "payload": payload}, *parts)
    except Exception:
        pass
