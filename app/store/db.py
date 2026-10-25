"""SQLite schema and connection setup."""

from pathlib import Path

import aiosqlite

# GitHub logins are case-insensitive, so every login column uses NOCASE.
SCHEMA = """
CREATE TABLE IF NOT EXISTS contributors (
    login             TEXT PRIMARY KEY COLLATE NOCASE,
    status            TEXT NOT NULL CHECK (status IN ('fetching', 'ready', 'error')),
    progress          REAL NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    covered_since     INTEGER,
    prs_watermark     INTEGER,
    reviews_watermark INTEGER,
    last_sync_at      INTEGER,
    error             TEXT
);

CREATE TABLE IF NOT EXISTS pull_requests (
    number       INTEGER PRIMARY KEY,
    author_login TEXT NOT NULL COLLATE NOCASE,
    title        TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    state        TEXT NOT NULL,
    additions    INTEGER NOT NULL,
    deletions    INTEGER NOT NULL,
    merged_at    INTEGER,
    closed_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_prs_author_merged ON pull_requests (author_login, merged_at);

-- pr_author_login is stored on the review so self-reviews can be excluded
-- without the reviewed PR (usually authored by someone else) being stored.
CREATE TABLE IF NOT EXISTS reviews (
    review_id       TEXT PRIMARY KEY,
    pr_number       INTEGER NOT NULL,
    pr_author_login TEXT NOT NULL COLLATE NOCASE,
    reviewer_login  TEXT NOT NULL COLLATE NOCASE,
    state           TEXT NOT NULL,
    submitted_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_reviewer ON reviews (reviewer_login, submitted_at);
CREATE INDEX IF NOT EXISTS idx_reviews_pr ON reviews (pr_number, reviewer_login);

CREATE TABLE IF NOT EXISTS narratives (
    login         TEXT NOT NULL COLLATE NOCASE,
    period        TEXT NOT NULL,
    data_hash     TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (login, period, data_hash)
);
"""


async def connect(db_path: Path) -> aiosqlite.Connection:
    """Open a connection and make sure the schema exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.executescript(SCHEMA)
    await conn.commit()
    return conn
