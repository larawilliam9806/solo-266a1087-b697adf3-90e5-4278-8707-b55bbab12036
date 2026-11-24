"""SQLite 连接、建表与事务。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'pending')),
    source_filename TEXT,
    row_count INTEGER NOT NULL,
    channel_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    activated_report_no TEXT,
    UNIQUE (month, version)
);
CREATE INDEX IF NOT EXISTS idx_batches_month_status ON batches (month, status);

CREATE TABLE IF NOT EXISTS batch_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches (id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    day TEXT NOT NULL,
    amount REAL,
    conversions REAL,
    UNIQUE (batch_id, channel, day)
);
CREATE INDEX IF NOT EXISTS idx_batch_rows_batch_channel
    ON batch_rows (batch_id, channel);

CREATE TABLE IF NOT EXISTS batch_keys (
    batch_id INTEGER NOT NULL REFERENCES batches (id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    day TEXT NOT NULL,
    PRIMARY KEY (batch_id, channel, day)
);

CREATE TABLE IF NOT EXISTS batch_channel_stats (
    batch_id INTEGER NOT NULL REFERENCES batches (id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    key_count INTEGER NOT NULL,
    amount_nonempty INTEGER NOT NULL,
    amount_null_rate REAL NOT NULL,
    amount_bins TEXT NOT NULL,
    conversions_nonempty INTEGER NOT NULL,
    conversions_null_rate REAL NOT NULL,
    conversions_bins TEXT NOT NULL,
    PRIMARY KEY (batch_id, channel)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_no TEXT NOT NULL UNIQUE,
    month TEXT NOT NULL,
    baseline_batch_id INTEGER NOT NULL REFERENCES batches (id),
    baseline_version INTEGER NOT NULL,
    candidate_batch_id INTEGER NOT NULL REFERENCES batches (id),
    candidate_version INTEGER NOT NULL,
    thresholds TEXT NOT NULL,
    details TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('passed', 'rejected')),
    reasons TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'rejected', 'confirmed', 'stale')),
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_month ON reports (month);
CREATE INDEX IF NOT EXISTS idx_reports_candidate ON reports (candidate_batch_id);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        connection = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,
            timeout=30,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        g.db = connection
    return g.db


def close_db(_error=None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db() -> None:
    get_db().executescript(SCHEMA)


@contextmanager
def transaction():
    """显式 BEGIN IMMEDIATE 事务；异常时回滚。不支持嵌套。"""
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
