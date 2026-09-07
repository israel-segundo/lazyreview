"""SQLite schema and connection helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner TEXT NOT NULL,
  repo TEXT NOT NULL,
  number INTEGER NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '',
  base_ref TEXT NOT NULL DEFAULT '',
  head_ref TEXT NOT NULL DEFAULT '',
  head_sha TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'open',
  is_draft INTEGER NOT NULL DEFAULT 0,
  additions INTEGER,
  deletions INTEGER,
  changed_files INTEGER,
  review_requested INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(owner, repo, number)
);

CREATE TABLE IF NOT EXISTS review_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
  agent TEXT NOT NULL,
  model TEXT NOT NULL,
  effort TEXT,
  skill TEXT NOT NULL DEFAULT 'pr-code-review-batch',
  head_sha TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',
  started_at TEXT,
  finished_at TEXT,
  duration_ms INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  total_tokens INTEGER,
  cost_usd REAL,
  consolidated INTEGER NOT NULL DEFAULT 0,
  deprecated INTEGER NOT NULL DEFAULT 0,
  findings_sev1 INTEGER NOT NULL DEFAULT 0,
  findings_sev2 INTEGER NOT NULL DEFAULT 0,
  findings_sev3 INTEGER NOT NULL DEFAULT 0,
  workdir_path TEXT,
  prompt_path TEXT,
  log_stdout_path TEXT,
  log_stderr_path TEXT,
  report_md_path TEXT,
  report_html_path TEXT,
  cli_command TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (pr_id) REFERENCES prs(id)
);

CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES review_runs(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,
  severity INTEGER NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  path TEXT,
  line INTEGER,
  disposition TEXT,
  raw_json TEXT,
  UNIQUE(run_id, external_id)
);

CREATE TABLE IF NOT EXISTS feedback_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
  action TEXT NOT NULL,
  source_run_ids TEXT NOT NULL DEFAULT '[]',
  body TEXT NOT NULL DEFAULT '',
  gh_comment_url TEXT,
  gh_review_id TEXT,
  dedup_summary TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS consolidations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
  head_sha TEXT NOT NULL DEFAULT '',
  source_run_ids TEXT NOT NULL DEFAULT '[]',
  agent TEXT NOT NULL,
  model TEXT NOT NULL,
  effort TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  body TEXT,
  findings_json TEXT,
  dedup_summary TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  total_tokens INTEGER,
  cost_usd REAL,
  workdir_path TEXT,
  prompt_path TEXT,
  log_stdout_path TEXT,
  log_stderr_path TEXT,
  cli_command TEXT,
  error_message TEXT,
  feedback_provided INTEGER NOT NULL DEFAULT 0,
  gh_action TEXT,
  gh_comment_url TEXT,
  gh_review_id TEXT,
  started_at TEXT,
  finished_at TEXT,
  duration_ms INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (pr_id) REFERENCES prs(id)
);

CREATE TABLE IF NOT EXISTS agent_model_cache (
  agent TEXT PRIMARY KEY,
  models_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_runs_pr ON review_runs(pr_id);
CREATE INDEX IF NOT EXISTS idx_runs_combo ON review_runs(agent, model, effort);
CREATE INDEX IF NOT EXISTS idx_runs_status ON review_runs(status);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);
CREATE INDEX IF NOT EXISTS idx_consolidations_pr ON consolidations(pr_id);
CREATE INDEX IF NOT EXISTS idx_consolidations_status ON consolidations(status);
"""

DEFAULT_SETTINGS = {
    "run_retention_value": "1",
    "run_retention_unit": "weeks",
    "pr_sync_interval_minutes": "15",
    "consolidator_agent": "claude",
    "consolidator_model": "haiku",
    "consolidator_effort": "low",
    # consolidator_prompt seeded in init_db from prompts/consolidate.md
}

SCHEMA_VERSION = 7


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the API thread and multiple job workers share the DB safely.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(prs)").fetchall()}
    alters = [
        ("state", "ALTER TABLE prs ADD COLUMN state TEXT NOT NULL DEFAULT 'open'"),
        ("review_requested", "ALTER TABLE prs ADD COLUMN review_requested INTEGER NOT NULL DEFAULT 0"),
        ("author", "ALTER TABLE prs ADD COLUMN author TEXT NOT NULL DEFAULT ''"),
        ("is_draft", "ALTER TABLE prs ADD COLUMN is_draft INTEGER NOT NULL DEFAULT 0"),
        ("additions", "ALTER TABLE prs ADD COLUMN additions INTEGER"),
        ("deletions", "ALTER TABLE prs ADD COLUMN deletions INTEGER"),
        ("changed_files", "ALTER TABLE prs ADD COLUMN changed_files INTEGER"),
    ]
    for name, sql in alters:
        if name not in cols:
            conn.execute(sql)

    run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(review_runs)").fetchall()}
    if "cost_usd" not in run_cols:
        conn.execute("ALTER TABLE review_runs ADD COLUMN cost_usd REAL")
    if "consolidated" not in run_cols:
        conn.execute(
            "ALTER TABLE review_runs ADD COLUMN consolidated INTEGER NOT NULL DEFAULT 0"
        )
    if "deprecated" not in run_cols:
        conn.execute(
            "ALTER TABLE review_runs ADD COLUMN deprecated INTEGER NOT NULL DEFAULT 0"
        )

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    else:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    defaults = dict(DEFAULT_SETTINGS)
    prompt_seed = _default_consolidator_prompt()
    if prompt_seed:
        defaults["consolidator_prompt"] = prompt_seed
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()


def _default_consolidator_prompt() -> str:
    path = Path(__file__).resolve().parent.parent / "prompts" / "consolidate.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@contextmanager
def db_session(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()
