"""SQLite schema and connection helpers."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from lazyreview.agents.names import (
    AgentName,
    ClaudeModelAlias,
    DEFAULT_AGY_MODEL,
    DEFAULT_CURSOR_MODEL,
    EffortLevel,
)

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
  head_updated_at TEXT,
  workspace_path TEXT,
  workspace_status TEXT NOT NULL DEFAULT 'none',
  workspace_delete_after TEXT,
  mirror_key TEXT,
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
  recommended_action TEXT,
  recommended_action_rationale TEXT,
  publish_body TEXT,
  ready_to_post INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  finished_at TEXT,
  duration_ms INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (pr_id) REFERENCES prs(id)
);


CREATE TABLE IF NOT EXISTS pr_analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
  head_sha TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',
  agent TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  effort TEXT,
  workdir_path TEXT,
  brief_markdown TEXT,
  brief_path TEXT,
  error_message TEXT,
  started_at TEXT,
  finished_at TEXT,
  duration_ms INTEGER,
  cli_command TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(pr_id, head_sha),
  FOREIGN KEY (pr_id) REFERENCES prs(id)
);

CREATE INDEX IF NOT EXISTS idx_pr_analyses_pr ON pr_analyses(pr_id);
CREATE INDEX IF NOT EXISTS idx_pr_analyses_status ON pr_analyses(status);

CREATE TABLE IF NOT EXISTS pr_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  agent TEXT,
  model TEXT,
  effort TEXT,
  analysis_id INTEGER,
  meta_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (pr_id) REFERENCES prs(id)
);

CREATE INDEX IF NOT EXISTS idx_pr_events_pr ON pr_events(pr_id, id DESC);

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

DEFAULT_BATCH_REVIEW_DEFAULTS = [
    {"agent": AgentName.AGY.value, "model": DEFAULT_AGY_MODEL, "effort": ""},
    {
        "agent": AgentName.CLAUDE.value,
        "model": ClaudeModelAlias.OPUS.value,
        "effort": EffortLevel.HIGH.value,
    },
    {"agent": AgentName.CURSOR.value, "model": DEFAULT_CURSOR_MODEL, "effort": ""},
]

DEFAULT_SETTINGS = {
    "run_retention_value": "1",
    "run_retention_unit": "weeks",
    "pr_sync_interval_minutes": "15",
    "display_timezone": "America/Mexico_City",
    "consolidator_agent": AgentName.CLAUDE.value,
    "consolidator_model": ClaudeModelAlias.HAIKU.value,
    "consolidator_effort": EffortLevel.LOW.value,
    # consolidator_prompt seeded in init_db from prompts/consolidate.md
    "pr_analysis_agent": AgentName.CLAUDE.value,
    "pr_analysis_model": ClaudeModelAlias.OPUS.value,
    "pr_analysis_effort": EffortLevel.HIGH.value,
    # pr_analysis_prompt seeded in init_db from prompts/pr_analysis.md
    # batch_review_defaults seeded in init_db (JSON list of {agent,model,effort})
}

SCHEMA_VERSION = 21


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the API thread and multiple job workers share the DB safely.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


class ThreadLocalConnection:
    """
    Drop-in Connection proxy: one real sqlite3 connection per OS thread.

    FastAPI runs sync routes in a threadpool, and background services
    (PR sync, retention) use their own threads. A single shared Connection
    raises sqlite3.InterfaceError under concurrent use even with
    check_same_thread=False.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self._db_path)
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn(), name)

    def close(self) -> None:
        with self._lock:
            conns = list(self._all)
            self._all.clear()
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()


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
        ("head_updated_at", "ALTER TABLE prs ADD COLUMN head_updated_at TEXT"),
    ]
    for name, sql in alters:
        if name not in cols:
            conn.execute(sql)
    # Backfill head age anchor from when we last touched the PR row.
    if "head_updated_at" not in cols:
        conn.execute(
            """
            UPDATE prs
            SET head_updated_at = COALESCE(updated_at, created_at)
            WHERE head_updated_at IS NULL OR head_updated_at = ''
            """
        )

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


    pr_cols = {r["name"] for r in conn.execute("PRAGMA table_info(prs)").fetchall()}
    for name, sql in [
        ("workspace_path", "ALTER TABLE prs ADD COLUMN workspace_path TEXT"),
        ("workspace_status", "ALTER TABLE prs ADD COLUMN workspace_status TEXT NOT NULL DEFAULT 'none'"),
        ("workspace_delete_after", "ALTER TABLE prs ADD COLUMN workspace_delete_after TEXT"),
        ("mirror_key", "ALTER TABLE prs ADD COLUMN mirror_key TEXT"),
    ]:
        if name not in pr_cols:
            conn.execute(sql)

    # Ensure pr_analyses exists on upgraded DBs (SCHEMA CREATE IF NOT EXISTS also covers fresh).
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pr_analyses (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          pr_id INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
          head_sha TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'queued',
          agent TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          effort TEXT,
          workdir_path TEXT,
          brief_markdown TEXT,
          brief_path TEXT,
          error_message TEXT,
          started_at TEXT,
          finished_at TEXT,
          duration_ms INTEGER,
          cli_command TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          UNIQUE(pr_id, head_sha),
          FOREIGN KEY (pr_id) REFERENCES prs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_pr_analyses_pr ON pr_analyses(pr_id);
        CREATE INDEX IF NOT EXISTS idx_pr_analyses_status ON pr_analyses(status);

CREATE TABLE IF NOT EXISTS pr_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  agent TEXT,
  model TEXT,
  effort TEXT,
  analysis_id INTEGER,
  meta_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (pr_id) REFERENCES prs(id)
);

CREATE INDEX IF NOT EXISTS idx_pr_events_pr ON pr_events(pr_id, id DESC);
        """
    )

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    prev = int(row["version"]) if row is not None else None
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    else:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    defaults = dict(DEFAULT_SETTINGS)
    prompt_seed = _default_consolidator_prompt()
    if prompt_seed:
        defaults["consolidator_prompt"] = prompt_seed
    analysis_prompt_seed = _default_pr_analysis_prompt()
    if analysis_prompt_seed:
        defaults["pr_analysis_prompt"] = analysis_prompt_seed
    publish_prompt_seed = _default_publish_prompt()
    if publish_prompt_seed:
        defaults["publish_prompt"] = publish_prompt_seed
    defaults["batch_review_defaults"] = json.dumps(DEFAULT_BATCH_REVIEW_DEFAULTS)
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    # v8: reshape consolidated comment template (tables at end, no top disclosure).
    # v10: review-pass detection from the authenticated user's prior PR threads.
    # v12: PR analysis defaults → Claude / Opus / high; refresh analysis prompt; pr_events.
    # v13: analysis prompt drops repeated title/branch/diff header; backfill pr_events.
    if prev is not None and prev < 12:
        for key, old, new in (
            ("pr_analysis_model", "haiku", ClaudeModelAlias.OPUS.value),
            ("pr_analysis_effort", "low", EffortLevel.HIGH.value),
        ):
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            if row is not None and (row["value"] or "") == old:
                conn.execute("UPDATE settings SET value = ? WHERE key = ?", (new, key))
        if analysis_prompt_seed:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'pr_analysis_prompt'",
                (analysis_prompt_seed,),
            )

    if prev is not None and prev < 13:
        if analysis_prompt_seed:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'pr_analysis_prompt'",
                (analysis_prompt_seed,),
            )
        # Backfill timeline events so existing PRs/analyses appear in History.
        conn.execute(
            """
            INSERT INTO pr_events (pr_id, kind, message, created_at)
            SELECT p.id, 'pr_created', 'Added to LazyReview', p.created_at
            FROM prs p
            WHERE NOT EXISTS (
              SELECT 1 FROM pr_events e WHERE e.pr_id = p.id AND e.kind = 'pr_created'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO pr_events (
              pr_id, kind, message, agent, model, effort, analysis_id, created_at
            )
            SELECT a.pr_id, 'analysis_triggered',
                   'PR analysis triggered',
                   a.agent, a.model, a.effort, a.id, a.created_at
            FROM pr_analyses a
            WHERE NOT EXISTS (
              SELECT 1 FROM pr_events e
              WHERE e.analysis_id = a.id AND e.kind = 'analysis_triggered'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO pr_events (
              pr_id, kind, message, agent, model, effort, analysis_id, meta_json, created_at
            )
            SELECT a.pr_id, 'analysis_finished',
                   CASE a.status
                     WHEN 'succeeded' THEN 'PR analysis finished'
                     WHEN 'failed' THEN 'PR analysis failed'
                     WHEN 'cancelled' THEN 'PR analysis cancelled'
                     ELSE 'PR analysis finished'
                   END,
                   a.agent, a.model, a.effort, a.id,
                   json_object('status', a.status),
                   COALESCE(a.finished_at, a.created_at)
            FROM pr_analyses a
            WHERE a.status IN ('succeeded', 'failed', 'cancelled')
              AND NOT EXISTS (
                SELECT 1 FROM pr_events e
                WHERE e.analysis_id = a.id AND e.kind = 'analysis_finished'
              )
            """
        )

    if prev is not None and prev < 14:
        # Attach status meta to backfilled analysis_finished events that lack it.
        conn.execute(
            """
            UPDATE pr_events
            SET meta_json = json_object(
              'status',
              CASE
                WHEN message LIKE '%failed%' THEN 'failed'
                WHEN message LIKE '%cancelled%' THEN 'cancelled'
                ELSE 'succeeded'
              END
            )
            WHERE kind = 'analysis_finished'
              AND (meta_json IS NULL OR meta_json = '' OR meta_json = '{}')
            """
        )

    if prev is not None and prev < 15:
        # Refresh analysis prompt: no repeated title/meta header; mermaid fences.
        if analysis_prompt_seed:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'pr_analysis_prompt'",
                (analysis_prompt_seed,),
            )

    if prev is not None and prev < 16:
        # Portal display timezone (timestamps remain UTC in storage).
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("display_timezone", "America/Mexico_City"),
        )

    if prev is not None and prev < 17:
        # Shorter deslopified analysis prompt; before/after TL;DR + paired diagrams/ERDs.
        if analysis_prompt_seed:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'pr_analysis_prompt'",
                (analysis_prompt_seed,),
            )

    if prev is not None and prev < 18:
        # Consolidation recommendation + publish draft columns.
        for col, decl in (
            ("recommended_action", "TEXT"),
            ("recommended_action_rationale", "TEXT"),
            ("publish_body", "TEXT"),
            ("ready_to_post", "INTEGER NOT NULL DEFAULT 0"),
        ):
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(consolidations)").fetchall()
            }
            if col not in cols:
                conn.execute(f"ALTER TABLE consolidations ADD COLUMN {col} {decl}")
        # Refresh consolidator prompt (analysis context + meta.json recommendation).
        if prompt_seed:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'consolidator_prompt'",
                (prompt_seed,),
            )
        publish_prompt_seed = _default_publish_prompt()
        if publish_prompt_seed:
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                ("publish_prompt", publish_prompt_seed),
            )
            # Always refresh default publish prompt content on upgrade.
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'publish_prompt'",
                (publish_prompt_seed,),
            )

    if prev is not None and prev < 19:
        # Publish draft: disposition lead-in; drop agent/dedup tables from GitHub body.
        publish_prompt_seed = _default_publish_prompt()
        if publish_prompt_seed:
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                ("publish_prompt", publish_prompt_seed),
            )
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'publish_prompt'",
                (publish_prompt_seed,),
            )

    if prev is not None and prev < 20:
        # First-person attribution for the current reviewer's own prior feedback.
        if prompt_seed:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'consolidator_prompt'",
                (prompt_seed,),
            )
        publish_prompt_seed = _default_publish_prompt()
        if publish_prompt_seed:
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                ("publish_prompt", publish_prompt_seed),
            )
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'publish_prompt'",
                (publish_prompt_seed,),
            )

    if prev is not None and prev < 21:
        # Analysis prompt: Mermaid syntax guards (no nested quotes / mid-label STR).
        if analysis_prompt_seed:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'pr_analysis_prompt'",
                (analysis_prompt_seed,),
            )

    if prev is not None and prev < 10 and prompt_seed:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'consolidator_prompt'",
            (prompt_seed,),
        )
    conn.commit()


def _default_consolidator_prompt() -> str:
    path = Path(__file__).resolve().parent.parent / "prompts" / "consolidate.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _default_pr_analysis_prompt() -> str:
    path = Path(__file__).resolve().parent.parent / "prompts" / "pr_analysis.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _default_publish_prompt() -> str:
    path = Path(__file__).resolve().parent.parent / "prompts" / "publish_github.md"
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
