"""SQLite 持久化层。

任务状态全部落库: 页面刷新、服务重启后任务依然可见。
单连接 + RLock, WAL 模式, 配合单进程内的线程模型使用。
"""
import json
import sqlite3
import threading
import time

from . import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT NOT NULL,
    stored_name   TEXT NOT NULL UNIQUE,
    path          TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    duration      REAL,
    sample_rate   INTEGER,
    channels      INTEGER,
    peak_db       REAL,
    codec_name    TEXT,
    bit_rate      INTEGER,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    status        TEXT NOT NULL,              -- queued|running|completed|failed|canceled
    options_json  TEXT NOT NULL,
    output_path   TEXT,
    output_name   TEXT,
    output_size   INTEGER,
    progress      REAL NOT NULL DEFAULT 0,    -- 0~100
    error         TEXT,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# 仍在进行中的状态
ACTIVE_STATUSES = ("queued", "running")
# 终态
FINISHED_STATUSES = ("completed", "failed", "canceled")
VALID_STATUSES = ACTIVE_STATUSES + FINISHED_STATUSES


class Database:
    def __init__(self, path=config.DB_PATH):
        config.ensure_dirs()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---- 基础封装 ----
    def execute(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # ---- sources ----
    def insert_source(self, filename, stored_name, path, size_bytes, info):
        cur = self.execute(
            """INSERT INTO sources
               (filename, stored_name, path, size_bytes, duration, sample_rate,
                channels, peak_db, codec_name, bit_rate, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (filename, stored_name, path, size_bytes,
             info.get("duration"), info.get("sample_rate"),
             info.get("channels"), info.get("peak_db"),
             info.get("codec_name"), info.get("bit_rate"), time.time()),
        )
        return cur.lastrowid

    def get_source(self, source_id):
        return self.query_one("SELECT * FROM sources WHERE id=?", (source_id,))

    def get_source_by_stored_name(self, stored_name):
        return self.query_one(
            "SELECT * FROM sources WHERE stored_name=?", (stored_name,))

    def list_sources(self):
        return self.query("SELECT * FROM sources ORDER BY id DESC")

    # ---- jobs ----
    def insert_job(self, source_id, options):
        cur = self.execute(
            """INSERT INTO jobs (source_id, status, options_json, created_at)
               VALUES (?, 'queued', ?, ?)""",
            (source_id, json.dumps(options, ensure_ascii=False, sort_keys=True),
             time.time()),
        )
        return cur.lastrowid

    def get_job(self, job_id):
        return self.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def get_job_detail(self, job_id):
        """带源文件 join 字段的单行查询, 供 API 序列化使用。"""
        return self.query_one(
            """SELECT j.*, s.filename AS source_filename, s.path AS source_path,
                      s.duration AS source_duration,
                      s.sample_rate AS source_sample_rate,
                      s.channels AS source_channels,
                      s.peak_db AS source_peak_db
               FROM jobs j JOIN sources s ON s.id = j.source_id
               WHERE j.id=?""", (job_id,))

    def list_jobs(self):
        return self.query(
            """SELECT j.*, s.filename AS source_filename, s.path AS source_path,
                      s.duration AS source_duration, s.sample_rate AS source_sample_rate,
                      s.channels AS source_channels, s.peak_db AS source_peak_db
               FROM jobs j JOIN sources s ON s.id = j.source_id
               ORDER BY j.id DESC""")

    def active_jobs_for_source(self, source_id):
        return self.query(
            "SELECT * FROM jobs WHERE source_id=? AND status IN ('queued','running')",
            (source_id,))

    def queued_jobs(self):
        return self.query(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY id ASC")

    def running_count(self):
        row = self.query_one(
            "SELECT COUNT(*) AS c FROM jobs WHERE status='running'")
        return row["c"]

    def update_job(self, job_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values()) + [job_id]
        self.execute(f"UPDATE jobs SET {cols} WHERE id=?", params)

    def mark_stale_active_jobs_failed(self):
        """服务重启时, 上一轮没跑完的任务不可能继续, 统一标记失败。"""
        self.execute(
            """UPDATE jobs SET status='failed',
               error='服务重启, 任务中断(原状态 queued/running)',
               finished_at=?
               WHERE status IN ('queued','running')""",
            (time.time(),))

    # ---- settings ----
    def get_setting(self, key, default=None):
        row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.execute(
            """INSERT INTO settings(key, value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(value)))

    def close(self):
        with self._lock:
            self._conn.close()
