import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "vinyl.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    artist          TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    year            INTEGER,
    format          TEXT,
    label           TEXT,
    catalog_number  TEXT,
    genres          TEXT,
    purchase_price  REAL,
    market_value    REAL,
    average_sell_price REAL,
    cover_url       TEXT,
    discogs_id      INTEGER,
    date_added      TEXT    NOT NULL DEFAULT (date('now'))
);

CREATE INDEX IF NOT EXISTS idx_artist ON records(artist COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_title  ON records(title  COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash      TEXT    NOT NULL UNIQUE,
    csrf_token      TEXT    NOT NULL,
    expires_at      TEXT    NOT NULL,
    last_seen_at    TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    revoked         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_token_hash ON auth_sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires_at ON auth_sessions(expires_at);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)
        try:
            conn.execute("ALTER TABLE records ADD COLUMN spotify_url TEXT")
        except Exception:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE records ADD COLUMN average_sell_price REAL")
        except Exception:
            pass  # column already exists


def row_to_dict(row) -> dict:
    return dict(row)
