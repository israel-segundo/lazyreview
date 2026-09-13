# TICKET-XXX: PR Analysis gate + shared PR worktrees

## Status

- Overall: **IMPLEMENTED** (local; PR slices not split yet)
- Last updated: 2026-09-12 (implemented end-to-end on main working tree)
- Issue: none — use `TICKET-XXX` placeholder (rename when ticket exists)
- Primary repo: `/home/segundo/repos/lazyreview`
- Plan store: `/home/segundo/repos/lazyreview/docs/plans`

## PR map

| PR | Status | Scope | Repo(s) | Depends on | Parallel |
|----|--------|-------|---------|------------|----------|
| 1 | DONE | Data model + shared PR workspace (bare mirror + worktree) | `lazyreview` | — | — |
| 2 | DONE | PR Analysis job runner + DB rows + enqueue (sync / head move / manual) | `lazyreview` | 1 | — |
| 3 | DONE | Gate reviews/consolidations on successful analysis for current head | `lazyreview` | 2 | — |
| 4 | DONE | Cancel on head move; Overview brief; home + PR-detail status balls + Run analysis button | `lazyreview` | 2, 3 | — |
| 5 | DONE | Cleanup + plan archive | `lazyreview` | 4 | — |

## Context

When a PR enters review, reviewers need an **orientation brief** before agentic code reviews: ticket intent, AC coverage, reading order, diagrams, blast radius (see `src/lazyreview/prompts/pr_analysis.md`). Today the app:

- Polls GitHub for pending-review PRs (`services/pr_sync.py` → `upsert_pr`)
- On head SHA change: **only** marks prior runs `deprecated` (`deprecate_runs_for_pr`) — **does not cancel** in-flight jobs
- Spins a **throwaway shallow clone** per review run (`services/clone.py` `clone_pr_head`) under `runs/{id}/repo`, deleted in `finally`
- Has **PR Analysis settings** (agent/model/effort/prompt) but **no job, table, or gate**

This plan adds a first-class **PR Analysis** workflow that must succeed for the current `prs.head_sha` before any review run or consolidation can start, and replaces per-run clones with a **shared checkout per PR** that analysis and reviews both use.

**Decided defaults:**

| Decision | Choice |
|----------|--------|
| When analysis starts | Eager on sync/`head_sha` change **and** manual **Run analysis** for PRs already in the DB (or never analyzed) |
| Checkout shape | One **bare mirror per GitHub repo** + one **`git worktree` per PR** under `{data_dir}/workspaces/` — **no throwaway per-run clones** |
| Analysis concurrency | **At most 2** analyses running at once (rest stay `queued`). Same pattern as `JobRunner` / `max_concurrent_jobs`; add `max_concurrent_analyses` (default `2`) |
| Workspace lifetime | Keep worktree while PR is open; when PR reaches a **terminal** GitHub state (`closed` / `merged`), **queue the workspace for deletion** (async/safe teardown). A manual “delete workspace” control on PR metadata is **out of this change** |
| Failed analysis | Block New Review / consolidations; surface error + “Retry analysis” on Overview. No silent bypass |
| Analysis persistence | Every analysis is a `pr_analyses` row (status, brief, errors, agent snapshot) — source of truth for UI and gate |
| Status UX | Jenkins-style **status balls** (`status_ball` / `lr-status-ball`) on **home** and **PR detail** for analysis `queued`/`running` (pulse) / `succeeded` / `failed` / missing |
| Ticket tooling | Rely on agent tools (`jira` CLI / MCP) as the prompt already requires — no new Jira integration in-app for v1 |

**Dependencies:**

- PR Analysis settings already landed (`pr_analysis_*` keys, Settings UI, `get_pr_analysis_config`)
- Existing concurrency pattern: `JobRunner` / `ConsolidationRunner` use `ThreadPoolExecutor(max_workers=settings.max_concurrent_jobs)` *(pre-checked 2026-09-12)*

**Out of scope:**

- Changing consolidator prompt / consolidation UX beyond the gate
- Webhooks (remain poll-based sync)
- Multi-tenant workspace isolation beyond existing data_dir layout
- Manual “delete workspace” / delete-metadata button on the PR page (follow-up)

**Acceptance criteria traceability:**

| AC (short) | PR(s) | Notes |
|------------|-------|-------|
| Analysis runs with PR Analysis prompt when a review PR arrives | 2 | Auto-enqueue on sync |
| Existing DB PRs can start analysis via button | 2, 4 | `POST .../analysis` / Run analysis |
| Analysis results stored in DB (`pr_analyses`) | 2 | Brief + status + errors |
| Home + PR detail show analysis progress via status balls | 4 | Reuse `lr-status-ball` (pulse while queued/running) |
| Brief includes ticket intent / AC coverage / diagrams per prompt | 2, 4 | Agent output; Overview renders markdown |
| Shared local checkout reused across analysis + future reviews | 1, 2, 4 | Bare + worktree; no per-run clones |
| ≤2 analyses run concurrently; extras queue | 2 | `max_concurrent_analyses=2` |
| No reviews or consolidations without successful analysis for current head | 3 | API + UI gate |
| Head move cancels in-flight work and updates checkout | 4 | Cancel + fetch/reset + re-analyze |
| Terminal PR queues workspace for deletion | 1, 4 | No manual delete UI in this change |

## Implementation Plan

This feature is implemented across 5 PRs:

### PR 1: Shared PR workspace (bare mirror + worktree) — **DONE**

**Repo:** `lazyreview`

Introduce a durable checkout layer so analysis and reviews share one tree per PR instead of `clone_pr_head` throwaways.

**Layout (recommended):**

```
{data_dir}/workspaces/
  mirrors/{owner}__{repo}.git          # bare, fetched once / updated
  prs/{pr_id}/                         # git worktree attached to mirror, checked out at head_sha
```

**TDD evidence:**

1. **Red** — workspace helper tests fail (missing module)
2. **Green** — create / ensure_at_sha / remove worktree tests pass with a temp git repo fixture
3. **Refactor** — keep `clone_pr_head` as fallback or delete once jobs migrate (prefer delete in PR 2/4)

Reference files:

- `src/lazyreview/services/clone.py` — current per-run shallow clone *(pre-checked 2026-09-12: no worktree usage)*
- `src/lazyreview/services/jobs.py` — calls `clone_pr_head` then `remove_repo_dir` in `finally`
- `src/lazyreview/config.py` — `data_dir` / runs paths

Tasks:

- [ ] `src/lazyreview/services/pr_workspace.py` (new) — `ensure_mirror`, `ensure_pr_worktree(pr, head_sha)`, locking, `refresh_pr_worktree(pr, head_sha)`, `queue_workspace_deletion(pr_id)` / `prune_queued_workspaces()` — **DONE**
- [ ] `src/lazyreview/config.py` / settings — `workspaces_dir` under data_dir — **DONE**
- [ ] `src/lazyreview/db/schema.py` — columns on `prs`: `workspace_path TEXT`, `workspace_delete_after TEXT NULL` (or `workspace_status`: `active` / `pending_delete` / `deleted`), optional `mirror_key` — **DONE**
- [ ] `tests/test_pr_workspace.py` (new) — create, refresh to new SHA, concurrent lock, queue-for-delete removes worktree safely — **DONE**
- [ ] Do **not** switch JobRunner yet (stay on clone) so this PR is mergeable alone — **DONE**

#### Verification

1. `.venv/bin/pytest tests/test_pr_workspace.py -q` passes
2. **Behavioral check (planned):** create worktree at SHA A, refresh to SHA B → `git rev-parse HEAD` equals B; `queue_workspace_deletion` then prune → path gone
3. **Behavioral check (executed):** *(execute-plan fills)*

---

### PR 2: PR Analysis job + enqueue — **DONE**

**Repo:** `lazyreview`

Persist analysis runs and execute them with `get_pr_analysis_config()`, cwd = PR worktree.

**Data model (`pr_analyses`):**

| Column | Notes |
|--------|-------|
| `id`, `pr_id`, `head_sha` | Unique `(pr_id, head_sha)` preferred |
| `status` | `queued` / `running` / `succeeded` / `failed` / `cancelled` |
| `agent`, `model`, `effort` | Snapshot from settings at start (re-read prompt live like consolidator) |
| `workdir_path` | Analysis artifact dir (logs, `brief.md`), not the git worktree |
| `brief_markdown` / `brief_path` | Stored output for Overview |
| `error_message`, timestamps | |

**Enqueue triggers:**

- After `upsert_pr` when `review_requested=1` and no successful analysis for current `head_sha` (eager)
- After head SHA change (cancel old analysis row; enqueue new)
- **Manual** `POST /api/prs/{id}/analysis` — for PRs already in the DB that never got an analysis row (or retry after failure). Creates/upserts a `pr_analyses` row and queues it; workers drain the queue (max 2 running)

**Persistence:** analysis status and brief live in SQLite (`pr_analyses`), not only on disk. UI and the review gate read this table.

**Runner:** mirror `JobRunner` / `ConsolidationRunner` pattern (`AnalysisRunner`) with **`ThreadPoolExecutor(max_workers=max_concurrent_analyses)`** where `max_concurrent_analyses` defaults to **2** (config.toml `[worker]` + optional settings override). Extra analyses remain `queued` until a slot frees.

Tasks:

- [ ] `src/lazyreview/config.py` / `config.default.toml` — `max_concurrent_analyses = 2` — **DONE**
- [ ] `src/lazyreview/db/schema.py` — `pr_analyses` table + helpers create/list/get/update — **DONE**
- [ ] `src/lazyreview/services/analysis_jobs.py` (new) — queue, execute (cap 2), render prompt, write `brief.md` — **DONE**
- [ ] `src/lazyreview/db/__init__.py` `upsert_pr` — after head change / review_requested, call `ensure_analysis_for_pr` — **DONE**
- [ ] `src/lazyreview/main.py` — start AnalysisRunner in lifespan — **DONE**
- [ ] Wire worktree: analysis job calls `ensure_pr_worktree` then agent with that cwd — **DONE**
- [ ] `tests/test_pr_analyses.py` (new) — seed, enqueue idempotency per head_sha, success stores brief, **never >2 running** under load — **DONE**
- [ ] API: `GET /api/prs/{id}/analysis`, `POST /api/prs/{id}/analysis` (start/enqueue), `POST .../analysis/retry` — **DONE**
- [ ] List/home payload: include current-head analysis status (or null) on each PR for status balls — **DONE**

#### Verification

1. `.venv/bin/pytest tests/test_pr_analyses.py tests/test_pr_workspace.py -q` passes
2. **Behavioral check (planned):** sync/resolve a fixture PR → `pr_analyses` row `queued`→`succeeded` with non-empty brief (agent mocked); enqueue 5 analyses → at most 2 `running` at once
3. **Behavioral check (executed):** *(execute-plan fills)*

---

### PR 3: Gate reviews and consolidations — **DONE**

**Repo:** `lazyreview`

Hard gate: no review run create/batch and no consolidation create unless `pr_analyses` has `status=succeeded` for `prs.head_sha`.

Tasks:

- [ ] `src/lazyreview/services/analysis_gate.py` (new) — `require_analysis(conn, pr_id) -> analysis | raises` — **DONE**
- [ ] `src/lazyreview/api/runs.py` — call gate in create + batch — **DONE**
- [ ] `src/lazyreview/api/consolidations.py` — call gate on create — **DONE**
- [ ] UI: disable New Review / consolidation actions when analysis missing/running/failed; show reason — **DONE**
- [ ] `tests/test_analysis_gate.py` (new) — 409/400 when no success; allowed when succeeded for current head — **DONE**
- [ ] *(pre-checked 2026-09-12: head move only deprecates runs — gate must key off **current** `head_sha`, not any historical success)* — **DONE**

#### Verification

1. `.venv/bin/pytest tests/test_analysis_gate.py -q` passes
2. **Behavioral check (planned):** without analysis → `POST /api/runs` rejected; after mocked success → accepted
3. **Behavioral check (executed):** *(execute-plan fills)*

---

### PR 4: Head-move cancel + worktree refresh + status UX + Overview — **DONE**

**Repo:** `lazyreview`

When `upsert_pr` detects `old_head != new_head`:

1. Cancel in-flight **review runs**, **consolidations** (queued/running/dormant), and **analyses** for that PR
2. Continue to deprecate stale runs
3. `refresh_pr_worktree(pr, new_head)`
4. Enqueue new analysis for `new_head`

Migrate **JobRunner** from per-run `clone_pr_head` to shared PR worktree (refcount / lock so concurrent reviews on same PR are safe). Stop deleting the shared tree in `finally`.

When sync observes PR `state` become **`closed` or `merged`** (already refreshed in `pr_sync.py`): call `queue_workspace_deletion(pr_id)` so the worktree (and eventually unused mirrors) are torn down asynchronously — **not** a UI delete button in this change.

**Status UX (reuse existing balls):** same Jenkins-style circles as runs/consolidations (`macros.status_ball` / `lr-status-ball` + `--pulse` while `queued`/`running`). Prefer a distinct `analyzing` ball color so home/detail do not confuse analysis with a review run.

**Home (`home.html`):** per-PR analysis status ball (missing / queued / running / succeeded / failed) next to Runs or as its own column; tooltip with label (“Analyzing…”, “Analysis queued”, etc.). Poll or refresh when any visible PR is in-flight.

**PR detail:** analysis status ball in the header/chips row; **Run analysis** button when there is no successful analysis for current `head_sha` (and not already queued/running); **Retry** when failed/cancelled. Overview tab renders the stored brief markdown once succeeded.

Tasks:

- [ ] `upsert_pr` / small `on_pr_head_changed` helper — cancel via JobRunner + ConsolidationRunner + AnalysisRunner handles — **DONE**
- [ ] Inject runner cancel hubs so sync thread can cancel (app.state or CancellationHub) — **DONE**
- [ ] `services/jobs.py` — use `ensure_pr_worktree` instead of clone; remove `remove_repo_dir` of shared path — **DONE**
- [ ] On terminal PR state in sync/upsert — `queue_workspace_deletion(pr_id)`; background prune that skips worktrees still locked by a running job — **DONE**
- [ ] `macros.html` / CSS — `analyzing` (or reuse) status ball + pulse for analysis in-progress — **DONE**
- [ ] `home.html` — analysis status ball per PR; wire list API field — **DONE**
- [ ] `pr_detail.html` — header/chips analysis ball; **Run analysis** / Retry; Overview brief from DB — **DONE**
- [ ] `tests/test_head_move_cancel.py` (new) — head change cancels running run (mock proc) + enqueues new analysis — **DONE**
- [ ] `tests/test_workspace_deletion.py` (new) — closed/merged PR queues delete; prune removes path — **DONE**
- [ ] `tests/test_analysis_ui_api.py` (optional) — start analysis on existing PR returns queued row — **DONE**

#### Verification

1. `.venv/bin/pytest tests/test_head_move_cancel.py tests/test_analysis_gate.py tests/test_workspace_deletion.py -q` passes
2. **Behavioral check (planned):** running run cancelled on head update; worktree `HEAD` matches new sha; Overview shows new brief after analysis; closed PR’s worktree disappears after prune; existing PR without analysis shows Run analysis + balls update home→detail while queued/running
3. **Behavioral check (executed):** *(execute-plan fills)*

---

### PR 5: Cleanup — **DONE**

**Repo:** `lazyreview`

- [ ] Remove dead `clone_pr_head` path if unused; keep thin wrapper only if tests still need it — **DONE**
- [ ] Docs: README note on `{data_dir}/workspaces` layout, analysis concurrency (`max_concurrent_analyses`), and terminal-PR deletion queue — **DONE**
- [ ] Optional: GC bare mirrors with zero remaining worktrees — **DONE** (defer if large)
- [ ] Archive this plan (move/delete per team norm) — **DONE**

#### Verification

1. Full `.venv/bin/pytest -q` passes
2. **Behavioral check (planned):** N/A — cleanup
3. **Behavioral check (executed):** *(execute-plan fills)*

## Architecture Notes

```mermaid
sequenceDiagram
  participant Sync as PrSyncer / upsert_pr
  participant DB as SQLite
  participant WS as pr_workspace
  participant AR as AnalysisRunner
  participant JR as JobRunner
  participant CR as ConsolidationRunner

  Sync->>DB: upsert_pr(head_sha)
  alt head changed
    Sync->>JR: cancel in-flight runs
    Sync->>CR: cancel consolidations
    Sync->>AR: cancel analysis
    Sync->>DB: deprecate_runs_for_pr
    Sync->>WS: refresh_pr_worktree(new_sha)
  end
  Sync->>AR: ensure_analysis(pr, head_sha)
  AR->>WS: ensure_pr_worktree
  AR->>AR: run PR Analysis prompt → brief.md
  AR->>DB: pr_analyses.status=succeeded

  Note over JR,CR: Gate: require succeeded analysis for current head
  JR->>WS: reuse same worktree (no per-run clone)
  CR->>CR: consolidate (no clone; unchanged)
```

**Architecture decision record:** **Required** — shared workspaces + analysis-as-gate change the durable runtime model (disk layout, job ordering). Capture under `docs/` in PR 5 (or earlier if preferred).

**Why bare + worktree:** one fetch bandwidth pool per GitHub repo; PR-specific trees stay small; head move is `fetch` + worktree reset instead of full re-clone. **No throwaway per-run clones.**

**Analysis concurrency:** `AnalysisRunner` uses the same pool pattern as reviews (`max_concurrent_jobs`), but with **`max_concurrent_analyses = 2`**. Additional analyses stay `queued` until a worker slot opens — yes, this is supported.

**Workspace deletion:** on terminal PR (`closed`/`merged`), mark workspace `pending_delete` and let a sweeper remove the worktree when no job holds the lock. Manual delete-metadata UI is a follow-up, not this change. Bare mirrors may remain until unused (optional GC in PR 5).

**Gate semantics:** success is **per `(pr_id, head_sha)`**. A brief for an old head does not unlock reviews after a push.

**Backfill / existing PRs:** PRs already in SQLite may have no `pr_analyses` row until the next sync enqueue or the user clicks **Run analysis**. That button is required — do not rely on sync alone for the migration window.

**Status indicators:** home and PR detail read `pr_analyses.status` for the current head and render `lr-status-ball` (pulsing while queued/running), matching runs/consolidations.

**Cancellation:** today cancel exists only via API; PR 4 must expose the same terminate path to the sync/upsert thread.

## Risks & Mitigations

| Risk | Mitigation | PR task |
|------|------------|---------|
| Concurrent reviews corrupt shared worktree | File lock / refcount in `pr_workspace`; serialize checkout refresh | PR 1, 4 |
| Sync thread cannot reach JobRunner.cancel | Register runners on app.state; CancellationHub | PR 4 |
| Analysis agent needs Jira/gh tools missing | Fail analysis with clear error; Retry button; still block reviews | PR 2, 3 |
| Many PRs queue analyses | Cap at 2 running; rest wait in `queued` | PR 2 |
| Disk growth under `workspaces/` | Queue delete on terminal PR; document; optional mirror GC | PR 1, 4, 5 |
| Delete while job still running | Prune skips locked/active worktrees; retry later | PR 1, 4 |
| Long analysis blocks all reviews | Show status on Overview; allow only current-head success (no skip) | PR 3, 4 |

## To Be Clarified

- Ticket ID / issue URL for renaming `TICKET-XXX`
- Confirm **eager sync enqueue** vs lazy-on-first-review (plan assumes eager **plus** manual Run analysis for existing PRs)
- Is a human “override gate” ever allowed for emergencies? (plan assumes **no** for v1)
- Immediate prune after terminal vs short grace period (e.g. 1h) before delete? (plan assumes **queue immediately, prune when idle** — no long retention window)

## How to Update This Plan

Update **Last updated**, PR map Status, and matching `### PR N` headings together. Prefer dated notes for decision changes. Implementers: re-verify `*[pre-checked …]*` notes before relying on them.
