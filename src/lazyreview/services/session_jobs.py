"""Interactive PR chat sessions (Explore + Challenge)."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Any

from lazyreview.agents import get_adapter
from lazyreview.agents.base import which_binary
from lazyreview.agents.codex import codex_exec_argv, last_agent_message
from lazyreview.agents.names import AgentName, ClaudeModelAlias, EffortLevel
from lazyreview.config import Settings
from lazyreview.db import (
    add_chat_turn,
    create_chat_session,
    get_chat_session,
    get_consolidation,
    get_current_pr_analysis,
    get_pr,
    get_setting,
    list_chat_turns,
    update_chat_session,
    update_consolidation,
    utcnow,
)
from lazyreview.findings_taxonomy import (
    coerce_findings_for_persist,
    findings_to_json,
    severity_word,
)
from lazyreview.services.agent_eligibility import require_selectable_agent
from lazyreview.services.credentials import subprocess_env
from lazyreview.services.procutil import close_popen_pipes, pump_stream_to_file
from lazyreview.services.pr_workspace import hold_worktree
from lazyreview.services.session_safety import (
    SESSION_ALLOWED_TOOLS,
    preflight_user_message,
    redact_session_reply,
    session_allow_path_prefixes,
)

logger = logging.getLogger(__name__)

_JSON_TRAILER = re.compile(
    r"```json\s*(\{.*?\})\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)

# Whole-message commands that apply working findings without an LLM round.
_APPLY_FINDINGS_COMMANDS = frozenset(
    {
        "apply",
        "apply findings",
        "apply the findings",
        "apply these findings",
        "update findings",
        "update the findings",
        "update consolidation",
        "update the consolidation",
        "incorporate findings",
        "incorporate the findings",
        "save findings",
        "write findings",
    }
)


def is_apply_findings_command(text: str) -> bool:
    """True when the user message is only a request to apply working findings."""
    t = (text or "").strip().lower()
    t = re.sub(r"[.!?]+$", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    return t in _APPLY_FINDINGS_COMMANDS


_FINDINGS_EDIT_INTENT_RE = re.compile(
    r"("
    r"\b(add|drop|remove|delete|merge|keep|include|exclude)\b.{0,40}\bfindings?\b"
    r"|\bfindings?\b.{0,40}\b(add|drop|remove|delete|merge|keep|include|exclude)\b"
    r"|\b(change|set|make|bump|lower)\b.{0,40}\b(severity|sev(?:erity)?)\b"
    r"|\b(update|edit|rewrite|soften|harden)\b.{0,40}\b(consolidation|finding|verdict|draft)\b"
    r"|\b(apply|save|write|incorporate)\b.{0,40}\bfindings?\b"
    r"|\badd\b.{0,20}\b(finding|C\d+)\b"
    r"|\bdrop\b.{0,20}\bC\d+\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def looks_like_findings_edit_intent(text: str) -> bool:
    """Heuristic: user wants to mutate consolidation findings from Explore chat."""
    t = (text or "").strip()
    if not t:
        return False
    if is_apply_findings_command(t):
        return True
    return bool(_FINDINGS_EDIT_INTENT_RE.search(t))


def challenge_ready_consolidation_for_pr(
    conn: Any, pr_id: int
) -> dict[str, Any] | None:
    """Latest succeeded, unposted consolidation for Challenge mode (UI gate)."""
    from lazyreview.db import list_consolidations_for_pr

    for row in list_consolidations_for_pr(conn, int(pr_id)):
        if (row.get("status") or "") != "succeeded":
            continue
        if int(row.get("feedback_provided") or 0):
            continue
        return row
    return None


def _working_differs_from_consolidation(
    session: dict[str, Any], consol: dict[str, Any]
) -> bool:
    """Whether challenge working copy differs from the saved consolidation."""
    try:
        working = json.loads(session.get("working_findings_json") or "[]")
    except json.JSONDecodeError:
        working = []
    try:
        saved = json.loads(consol.get("findings_json") or "[]")
    except json.JSONDecodeError:
        saved = []
    if working != saved:
        return True
    w_action = (session.get("working_recommended_action") or "").strip()
    c_action = (consol.get("recommended_action") or "").strip()
    if w_action and w_action != c_action:
        return True
    w_rat = (session.get("working_recommended_action_rationale") or "").strip()
    c_rat = (consol.get("recommended_action_rationale") or "").strip()
    if w_rat and w_rat != c_rat:
        return True
    w_body = (session.get("working_body_md") or "").strip()
    c_body = (consol.get("body") or "").strip()
    if w_body and w_body != c_body:
        return True
    return False


def sessions_dir(settings: Settings) -> Path:
    path = settings.data_dir / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_prompt(name: str) -> str:
    try:
        pkg = resources.files("lazyreview.prompts")
        return pkg.joinpath(name).read_text(encoding="utf-8")
    except Exception:
        return ""


def _session_defaults(conn: Any) -> tuple[str, str, str]:
    agent = (
        get_setting(conn, "session_agent", AgentName.CLAUDE.value)
        or AgentName.CLAUDE.value
    )
    model = (
        get_setting(conn, "session_model", ClaudeModelAlias.OPUS.value)
        or ClaudeModelAlias.OPUS.value
    )
    effort = (
        get_setting(conn, "session_effort", EffortLevel.HIGH.value)
        or EffortLevel.HIGH.value
    )
    return agent.strip().lower(), model.strip(), effort.strip()


def parse_structured_trailer(text: str) -> tuple[str, dict[str, Any] | None]:
    """Split assistant markdown from optional trailing ```json findings block."""
    raw = text or ""
    m = _JSON_TRAILER.search(raw.strip())
    if not m:
        return raw.strip(), None
    body = raw[: m.start()].rstrip()
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return raw.strip(), None
    if not isinstance(data, dict):
        return raw.strip(), None
    return body, data


def findings_to_body_md(
    *,
    pr: dict[str, Any],
    findings: list[dict[str, Any]],
    recommended_action: str,
    rationale: str,
    pass_label: str = "Challenge",
) -> str:
    """Build a minimal internal report from working findings (no second agent)."""
    title = pr.get("title") or f"PR #{pr.get('number')}"
    lines = [
        f"## Review ({pass_label}): {title}",
        "",
        (
            f"Stance after interactive challenge. Recommended action: "
            f"**{recommended_action}**. {rationale}".strip()
        ),
        "",
    ]
    if not findings:
        lines.append("_No kept findings._")
        lines.append("")
    else:
        for i, f in enumerate(findings, start=1):
            claim = (f.get("title") or f.get("id") or f"Finding {i}").strip()
            body = (f.get("body") or "").strip()
            fid = f.get("id") or f"C{i}"
            cat = str(f.get("category") or "").strip()
            labels = f.get("labels") if isinstance(f.get("labels"), list) else []
            tax = ""
            if cat and labels:
                tax = f" · {cat}/{'+'.join(str(x) for x in labels)}"
            elif cat:
                tax = f" · {cat}"
            lines.append(
                f"**{fid} · {severity_word(f.get('severity'))}{tax} — {claim}.** {body}".strip()
            )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


CODEX_CHAT_NOTE = (
    "\n## Tool access\n"
    "You have no shell or file access in this chat. Answer from the context above; "
    "if you need code that isn't here, name the file and ask the user to paste it.\n"
)


def _extract_cli_session_id(stdout: str) -> str | None:
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        for key in ("session_id", "sessionId"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        data = obj.get("data")
        if isinstance(data, dict):
            for key in ("session_id", "sessionId"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return None


def _extract_assistant_text(stdout: str) -> str:
    """Best-effort plain text from stream-json / result events."""
    texts: list[str] = []
    last_result = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        t = obj.get("type")
        if t == "assistant":
            msg = obj.get("message") or obj.get("content")
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            texts.append(str(part.get("text") or ""))
                elif isinstance(content, str):
                    texts.append(content)
            elif isinstance(msg, str):
                texts.append(msg)
        if t == "result" or obj.get("subtype") == "success":
            r = obj.get("result")
            if isinstance(r, str) and r.strip():
                last_result = r
            elif isinstance(obj.get("content"), str):
                last_result = str(obj["content"])
    if last_result.strip():
        return last_result.strip()
    joined = "\n\n".join(t for t in texts if t.strip())
    if joined.strip():
        return joined.strip()
    # Codex JSONL has no result event; its last agent message is the reply.
    return last_agent_message(stdout).strip()


class SessionRunner:
    """One agent process per chat round; durable state in SQLite."""

    def __init__(self, settings: Settings, conn: Any) -> None:
        self.settings = settings
        self._conn = conn
        self._lock = threading.Lock()
        self._procs: dict[int, subprocess.Popen[str]] = {}
        self._busy: set[int] = set()

    def create_explore_session(
        self,
        pr_id: int,
        *,
        agent: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        force_new: bool = False,
    ) -> dict[str, Any]:
        conn = self._conn
        pr = get_pr(conn, pr_id)
        if not pr:
            raise ValueError("PR not found")
        head = (pr.get("head_sha") or "").strip()
        if not force_new:
            from lazyreview.db import get_open_chat_session

            existing = get_open_chat_session(
                conn, pr_id=pr_id, mode="explore", head_sha=head or None
            )
            if existing:
                return existing
        d_agent, d_model, d_effort = _session_defaults(conn)
        chosen_agent = (agent or d_agent).strip().lower()
        require_selectable_agent(self.settings, conn, chosen_agent)
        work = sessions_dir(self.settings) / "pending"
        work.mkdir(parents=True, exist_ok=True)
        row = create_chat_session(
            conn,
            pr_id=pr_id,
            head_sha=head,
            mode="explore",
            agent=chosen_agent,
            model=(model or d_model).strip(),
            effort=(effort if effort is not None else d_effort) or None,
        )
        workdir = sessions_dir(self.settings) / str(row["id"])
        workdir.mkdir(parents=True, exist_ok=True)
        return update_chat_session(conn, int(row["id"]), workdir_path=str(workdir)) or row

    def create_challenge_session(
        self,
        consolidation_id: int,
        *,
        agent: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        force_new: bool = False,
    ) -> dict[str, Any]:
        conn = self._conn
        consol = get_consolidation(conn, consolidation_id)
        if not consol:
            raise ValueError("Consolidation not found")
        pr_id = int(consol["pr_id"])
        pr = get_pr(conn, pr_id)
        if not pr:
            raise ValueError("PR not found")
        if not force_new:
            from lazyreview.db import get_open_chat_session

            existing = get_open_chat_session(
                conn,
                pr_id=pr_id,
                mode="challenge",
                consolidation_id=consolidation_id,
            )
            if existing:
                return existing
        d_agent, d_model, d_effort = _session_defaults(conn)
        chosen_agent = (agent or d_agent).strip().lower()
        require_selectable_agent(self.settings, conn, chosen_agent)
        findings_json = consol.get("findings_json") or "[]"
        body_md = (consol.get("body") or "").strip() or None
        row = create_chat_session(
            conn,
            pr_id=pr_id,
            head_sha=(consol.get("head_sha") or pr.get("head_sha") or "").strip(),
            mode="challenge",
            consolidation_id=consolidation_id,
            agent=chosen_agent,
            model=(model or d_model).strip(),
            effort=(effort if effort is not None else d_effort) or None,
            working_findings_json=findings_json
            if isinstance(findings_json, str)
            else json.dumps(findings_json),
            working_recommended_action=consol.get("recommended_action") or "comment",
            working_recommended_action_rationale=consol.get(
                "recommended_action_rationale"
            )
            or "",
            working_body_md=body_md,
        )
        workdir = sessions_dir(self.settings) / str(row["id"])
        workdir.mkdir(parents=True, exist_ok=True)
        # Do not clear publish_body / publish-body.md on open alone — that made
        # opening Challenge (with zero turns) wipe a ready GitHub draft. Draft is
        # invalidated when the working copy first diverges (see _invalidate_publish).
        return (
            update_chat_session(conn, int(row["id"]), workdir_path=str(workdir)) or row
        )

    def patch_session(
        self,
        session_id: int,
        *,
        agent: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if agent is not None:
            chosen = agent.strip().lower()
            require_selectable_agent(self.settings, self._conn, chosen)
            fields["agent"] = chosen
            fields["cli_session_id"] = None
        if model is not None:
            fields["model"] = model.strip()
            fields["cli_session_id"] = None
        if effort is not None:
            fields["effort"] = effort.strip()
            fields["cli_session_id"] = None
        row = update_chat_session(self._conn, session_id, **fields)
        if not row:
            raise ValueError("Session not found")
        return row

    def send_message(self, session_id: int, text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ValueError("Message is empty")
        block, warn = preflight_user_message(text)
        if block:
            raise ValueError(block)

        session = get_chat_session(self._conn, session_id)
        if not session:
            raise ValueError("Session not found")
        if (
            session.get("mode") == "challenge"
            and is_apply_findings_command(text)
        ):
            with self._lock:
                if session_id in self._busy:
                    raise ValueError("Session already has a round in progress")
                self._busy.add(session_id)
            try:
                return self._apply_findings_from_chat(session_id, text)
            finally:
                with self._lock:
                    self._busy.discard(session_id)

        if (
            session.get("mode") == "explore"
            and looks_like_findings_edit_intent(text)
        ):
            ready = challenge_ready_consolidation_for_pr(
                self._conn, int(session["pr_id"])
            )
            if ready:
                with self._lock:
                    if session_id in self._busy:
                        raise ValueError("Session already has a round in progress")
                    self._busy.add(session_id)
                try:
                    return self._offer_challenge_switch(
                        session_id, text, ready
                    )
                finally:
                    with self._lock:
                        self._busy.discard(session_id)

        with self._lock:
            if session_id in self._busy:
                raise ValueError("Session already has a round in progress")
            self._busy.add(session_id)
        try:
            return self._run_round(session_id, text, soft_warn=warn)
        finally:
            with self._lock:
                self._busy.discard(session_id)

    def _offer_challenge_switch(
        self,
        session_id: int,
        user_text: str,
        consol: dict[str, Any],
    ) -> dict[str, Any]:
        """Ask the human to switch Explore → Challenge (no LLM round)."""
        cid = int(consol["id"])
        add_chat_turn(self._conn, session_id=session_id, role="user", text=user_text)
        reply = (
            f"Findings edits need **Challenge** mode for consolidation #{cid}. "
            "Switch to Challenge on the Findings tab (or confirm below) so I can "
            "update the working findings — that does **not** post to GitHub."
        )
        turn = add_chat_turn(
            self._conn,
            session_id=session_id,
            role="assistant",
            text=reply,
        )
        return {
            "session": get_chat_session(self._conn, session_id),
            "turn": turn,
            "exit_code": 0,
            "offer_challenge": {
                "consolidation_id": cid,
                "pending_text": user_text,
            },
        }

    def _apply_findings_from_chat(
        self, session_id: int, user_text: str
    ) -> dict[str, Any]:
        """Record the apply command, write findings, enqueue publish — no LLM round."""
        add_chat_turn(self._conn, session_id=session_id, role="user", text=user_text)
        applied = self.apply_findings(session_id)
        reply = (
            "Applied working findings to the consolidation and queued GitHub draft "
            "regeneration. The findings list and draft will refresh when that finishes. "
            "This does **not** post to GitHub."
        )
        turn = add_chat_turn(
            self._conn,
            session_id=session_id,
            role="assistant",
            text=reply,
        )
        return {
            "session": applied.get("session") or get_chat_session(self._conn, session_id),
            "turn": turn,
            "exit_code": 0,
            "applied_findings": True,
            "consolidation_id": applied.get("consolidation_id"),
            "consolidation": applied.get("consolidation"),
        }

    def _run_round(
        self, session_id: int, user_text: str, *, soft_warn: str | None
    ) -> dict[str, Any]:
        conn = self._conn
        session = get_chat_session(conn, session_id)
        if not session:
            raise ValueError("Session not found")
        if session.get("status") != "open":
            raise ValueError("Session is not open")
        pr = get_pr(conn, int(session["pr_id"]))
        if not pr:
            raise ValueError("PR not found")

        add_chat_turn(conn, session_id=session_id, role="user", text=user_text)

        workdir = Path(
            session.get("workdir_path") or sessions_dir(self.settings) / str(session_id)
        )
        workdir.mkdir(parents=True, exist_ok=True)

        head = (session.get("head_sha") or pr.get("head_sha") or "").strip()
        prompt = self._build_prompt(session, pr, user_text, soft_warn=soft_warn)
        prompt_path = workdir / "round-prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        stdout_path = workdir / "stdout.log"
        stderr_path = workdir / "stderr.log"

        agent = (session.get("agent") or AgentName.CLAUDE.value).lower()
        model = session.get("model") or ClaudeModelAlias.OPUS.value
        effort = session.get("effort") or EffortLevel.HIGH.value

        allow_paths = session_allow_path_prefixes(workdir)
        with hold_worktree(self.settings, pr, head, conn=conn) as worktree:
            allow_paths = session_allow_path_prefixes(workdir, worktree)
            argv, stdin_text = self._build_command(
                agent=agent,
                model=model,
                effort=effort,
                worktree=worktree,
                workdir=workdir,
                prompt=prompt,
                cli_session_id=session.get("cli_session_id"),
            )
            update_chat_session(
                conn,
                session_id,
                error_message=None,
            )
            rc = self._run_proc(
                session_id,
                argv,
                cwd=worktree,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                stdin_text=stdin_text,
            )

        stdout = (
            stdout_path.read_text(encoding="utf-8", errors="replace")
            if stdout_path.exists()
            else ""
        )
        assistant = _extract_assistant_text(stdout)
        if not assistant.strip():
            err = (
                stderr_path.read_text(encoding="utf-8", errors="replace")[-1500:]
                if stderr_path.exists()
                else ""
            )
            assistant = (
                f"(No assistant text returned; exit {rc}.)\n\n```\n{err}\n```"
                if err
                else f"(No assistant text returned; exit {rc}.)"
            )

        reply_body, structured = parse_structured_trailer(assistant)
        reply_body = redact_session_reply(
            reply_body, allow_path_prefixes=allow_paths
        )
        if structured and isinstance(structured.get("working_body_md"), str):
            structured["working_body_md"] = redact_session_reply(
                structured["working_body_md"], allow_path_prefixes=allow_paths
            )
        if structured and isinstance(structured.get("publish_guidance"), str):
            structured["publish_guidance"] = redact_session_reply(
                structured["publish_guidance"], allow_path_prefixes=allow_paths
            )
        findings_delta = None
        action = None
        rationale = None
        fields: dict[str, Any] = {}
        if structured:
            if "working_findings" in structured and isinstance(
                structured["working_findings"], list
            ):
                prior_raw = (session or {}).get("working_findings_json")
                normalized = coerce_findings_for_persist(
                    structured["working_findings"],
                    default_origin="human",
                    prior=prior_raw,
                )
                findings_delta = findings_to_json(normalized, indent=None)
                fields["working_findings_json"] = findings_delta
            if structured.get("recommended_action"):
                action = str(structured["recommended_action"]).strip()
                if action in ("approve", "request_changes", "comment"):
                    fields["working_recommended_action"] = action
                    rationale = str(
                        structured.get("recommended_action_rationale") or ""
                    )
                    fields["working_recommended_action_rationale"] = rationale
            if "working_body_md" in structured and structured["working_body_md"] is not None:
                body_md = structured["working_body_md"]
                if isinstance(body_md, str):
                    fields["working_body_md"] = body_md
            if "publish_guidance" in structured and structured["publish_guidance"] is not None:
                guidance = structured["publish_guidance"]
                if isinstance(guidance, str):
                    fields["working_publish_guidance"] = guidance

        cli_sid = _extract_cli_session_id(stdout)
        if cli_sid:
            fields["cli_session_id"] = cli_sid
        if fields:
            update_chat_session(conn, session_id, **fields)

        turn = add_chat_turn(
            conn,
            session_id=session_id,
            role="assistant",
            text=reply_body,
            findings_delta=findings_delta,
            recommended_action=action,
            recommended_action_rationale=rationale,
        )
        session = get_chat_session(conn, session_id)
        out: dict[str, Any] = {"session": session, "turn": turn, "exit_code": rc}

        if session and session.get("mode") == "challenge" and fields:
            self._invalidate_publish_if_working_diverged(session)

        want_apply = bool(
            structured and structured.get("apply_findings") is True
        )
        if want_apply and session and session.get("mode") == "challenge":
            applied = self.apply_findings(session_id)
            out["session"] = applied.get("session") or get_chat_session(
                conn, session_id
            )
            out["applied_findings"] = True
            out["consolidation_id"] = applied.get("consolidation_id")
            out["consolidation"] = applied.get("consolidation")
        return out

    def _invalidate_publish_if_working_diverged(self, session: dict[str, Any]) -> None:
        """Clear publish draft once Challenge edits diverge from the consolidation."""
        consol_id = session.get("consolidation_id")
        if not consol_id:
            return
        conn = self._conn
        consol = get_consolidation(conn, int(consol_id))
        if not consol:
            return
        if not _working_differs_from_consolidation(session, consol):
            return
        if not (
            consol.get("publish_body")
            or int(consol.get("ready_to_post") or 0)
            or (consol.get("publish_status") or "").strip()
        ):
            # Nothing to clear — also remove stale file if present.
            pass
        consol_work = Path(
            consol.get("workdir_path")
            or (self.settings.data_dir / "consolidations" / str(consol_id))
        )
        stale_publish = consol_work / "publish-body.md"
        if stale_publish.exists():
            try:
                stale_publish.unlink()
            except OSError:
                logger.exception(
                    "failed removing stale publish-body.md for consolidation %s",
                    consol_id,
                )
        update_consolidation(
            conn,
            int(consol_id),
            ready_to_post=0,
            publish_body=None,
            publish_status=None,
            publish_finished_at=None,
        )

    def _build_prompt(
        self,
        session: dict[str, Any],
        pr: dict[str, Any],
        user_text: str,
        *,
        soft_warn: str | None,
    ) -> str:
        mode = session.get("mode") or "explore"
        base = _load_prompt(
            "session_challenge.md" if mode == "challenge" else "session_explore.md"
        )
        base = (
            base.replace("{owner}", str(pr.get("owner") or ""))
            .replace("{repo}", str(pr.get("repo") or ""))
            .replace("{number}", str(pr.get("number") or ""))
        )
        analysis = get_current_pr_analysis(self._conn, int(pr["id"]))
        brief = ""
        analysis_head = ""
        analysis_status = ""
        if analysis:
            analysis_head = str(analysis.get("head_sha") or "")
            analysis_status = str(analysis.get("status") or "")
            if (analysis.get("brief_markdown") or "").strip():
                brief = str(analysis["brief_markdown"])

        turns = list_chat_turns(self._conn, int(session["id"]))
        # Exclude the user turn we just inserted from the "prior" block — it's below.
        prior = turns[:-1] if turns and turns[-1].get("role") == "user" else turns
        history_lines: list[str] = []
        for t in prior[-40:]:
            role = t.get("role") or "?"
            history_lines.append(f"### {role}\n{t.get('text') or ''}\n")

        pr_head = str(pr.get("head_sha") or "")
        brief_note = ""
        if analysis_head and pr_head and analysis_head != pr_head:
            brief_note = (
                f"⚠️ Brief is for `{analysis_head[:12]}`, PR head is `{pr_head[:12]}` "
                f"(stale). Chat cannot refresh it — user must use Analysis on the PR page."
            )
        elif analysis_status and analysis_status not in ("succeeded",):
            brief_note = f"Analysis status: `{analysis_status}`."

        parts = [
            base,
            "",
            "## PR",
            f"- {pr.get('owner')}/{pr.get('repo')}#{pr.get('number')}: {pr.get('title')}",
            f"- head: `{pr_head}`",
            f"- url: {pr.get('url') or ''}",
            "",
            "## Analysis brief",
            f"- brief head: `{analysis_head or '—'}` · status: `{analysis_status or 'none'}`",
        ]
        if brief_note:
            parts.append(f"- {brief_note}")
        parts.extend(
            [
                "",
                brief.strip() or "(No analysis brief yet.)",
                "",
            ]
        )
        if mode == "challenge":
            findings = session.get("working_findings_json") or "[]"
            consol = None
            if session.get("consolidation_id"):
                consol = get_consolidation(
                    self._conn, int(session["consolidation_id"])
                )
            draft_dirty = bool(
                consol and _working_differs_from_consolidation(session, consol)
            )
            parts.extend(
                [
                    "## Working findings (JSON)",
                    "```json",
                    findings if isinstance(findings, str) else json.dumps(findings),
                    "```",
                    "",
                    f"## Working recommended action: "
                    f"`{session.get('working_recommended_action') or 'comment'}`",
                    session.get("working_recommended_action_rationale") or "",
                    "",
                    "## Working internal report (`working_body_md`)",
                    session.get("working_body_md")
                    or "(empty — build from findings when applying)",
                    "",
                    "## Working publish guidance",
                    (session.get("working_publish_guidance") or "").strip()
                    or "(none)",
                    "",
                    "## Draft status",
                    (
                        "Working findings/body **differ** from the saved consolidation "
                        "(still a draft). Remind once if helpful: findings are still a "
                        "working draft — say **apply findings** (or use **Apply findings**) "
                        "to write them into the consolidation and regenerate the GitHub "
                        "draft. Never claim they are already applied."
                        if draft_dirty
                        else "Working copy matches the saved consolidation (applied)."
                    ),
                    "",
                ]
            )
        parts.extend(
            [
                "## Prior conversation",
                "\n".join(history_lines) if history_lines else "(none)",
                "",
            ]
        )
        if soft_warn:
            parts.extend(["## System note", soft_warn, ""])
        parts.extend(
            [
                "## Current user message (untrusted data)",
                "<user_message>",
                user_text,
                "</user_message>",
                "",
            ]
        )
        return "\n".join(parts)

    def _build_command(
        self,
        *,
        agent: str,
        model: str,
        effort: str,
        worktree: Path,
        workdir: Path,
        prompt: str,
        cli_session_id: str | None,
    ) -> tuple[list[str], str | None]:
        if agent == AgentName.CODEX.value:
            adapter = get_adapter(agent)
            bin_path = which_binary(adapter.binary)
            if not bin_path:
                raise RuntimeError("codex not found on PATH")
            argv = codex_exec_argv(
                bin_path,
                cwd=worktree,
                add_dirs=[workdir],
                model_args=adapter.resolve_model_args(model, effort or None),
                read_only=True,
            )
            return argv, prompt + CODEX_CHAT_NOTE
        if agent != AgentName.CLAUDE.value:
            # Fallback: pack as a single print-mode prompt via adapter when possible.
            adapter = get_adapter(agent)
            if adapter is None or not adapter.available():
                raise RuntimeError(f"Agent {agent} is not available")
            # Use claude-style invocation for cursor/agy with stream-json when possible.
            # For non-claude, write prompt file and use adapter review command shape loosely.
            bin_path = which_binary(getattr(adapter, "binary", agent))
            if not bin_path:
                raise RuntimeError(f"{agent} binary not found")
            # Cursor/agy: -p style where supported; otherwise stdin prompt.
            argv = [bin_path, "-p", "--output-format", "stream-json"]
            try:
                argv.extend(adapter.resolve_model_args(model, effort or None))
            except Exception:
                argv.extend(["--model", model])
            return argv, prompt

        path = which_binary("claude")
        if not path:
            raise RuntimeError("claude not found on PATH")
        argv = [
            path,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--permission-mode",
            "bypassPermissions",
            "--model",
            model,
        ]
        if effort:
            mapped = (
                EffortLevel.HIGH.value
                if effort == EffortLevel.ULTRA.value
                else effort
            )
            argv.extend(["--effort", mapped])
        argv.extend(
            [
                "--allowedTools",
                SESSION_ALLOWED_TOOLS,
                "--add-dir",
                str(worktree),
                "--add-dir",
                str(workdir),
            ]
        )
        if cli_session_id:
            argv.extend(["--resume", cli_session_id])
        return argv, prompt

    def _run_proc(
        self,
        session_id: int,
        cmd: list[str],
        *,
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
        stdin_text: str | None,
    ) -> int:
        timeout = self.settings.job_timeout_seconds
        deadline = time.monotonic() + timeout if timeout else None
        argv = list(cmd)
        if shutil.which("stdbuf"):
            argv = ["stdbuf", "-oL", "-eL", *argv]
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=subprocess_env(cmd),
        )
        with self._lock:
            self._procs[session_id] = proc

        t_out = threading.Thread(
            target=pump_stream_to_file,
            args=(proc.stdout, stdout_path),
            kwargs={"log_label": "session log pump"},
            name=f"lr-sess-out-{session_id}",
            daemon=True,
        )
        t_err = threading.Thread(
            target=pump_stream_to_file,
            args=(proc.stderr, stderr_path),
            kwargs={"log_label": "session log pump"},
            name=f"lr-sess-err-{session_id}",
            daemon=True,
        )
        t_out.start()
        t_err.start()
        try:
            if stdin_text is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(stdin_text)
                    proc.stdin.close()
                except OSError:
                    logger.exception("failed writing stdin for session %s", session_id)

            while proc.poll() is None:
                if deadline is not None and time.monotonic() > deadline:
                    proc.terminate()
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise TimeoutError(f"Session round exceeded {timeout}s timeout")
                time.sleep(0.4)

            t_out.join(timeout=30)
            t_err.join(timeout=30)
            with self._lock:
                self._procs.pop(session_id, None)
            return int(proc.returncode if proc.returncode is not None else -1)
        finally:
            close_popen_pipes(proc)

    def apply_findings(self, session_id: int) -> dict[str, Any]:
        """Write working findings into the consolidation and enqueue publish draft regen.

        Keeps the challenge session open so chat can continue. Does not post to GitHub.
        """
        conn = self._conn
        session = get_chat_session(conn, session_id)
        if not session:
            raise ValueError("Session not found")
        if session.get("mode") != "challenge":
            raise ValueError("Apply findings is only available in challenge mode")

        consol_id = session.get("consolidation_id")
        if not consol_id:
            raise ValueError("Challenge session has no consolidation")
        consol = get_consolidation(conn, int(consol_id))
        if not consol:
            raise ValueError("Consolidation not found")
        pr = get_pr(conn, int(session["pr_id"]))
        if not pr:
            raise ValueError("PR not found")

        findings_json = session.get("working_findings_json") or "[]"
        action = session.get("working_recommended_action") or "comment"
        rationale = session.get("working_recommended_action_rationale") or ""
        findings = coerce_findings_for_persist(
            findings_json,
            default_origin="human",
            prior=consol.get("findings_json"),
        )
        findings_json_out = findings_to_json(findings)

        working_body = (session.get("working_body_md") or "").strip()
        if working_body:
            body = working_body if working_body.endswith("\n") else working_body + "\n"
        else:
            body = findings_to_body_md(
                pr=pr,
                findings=findings,
                recommended_action=action,
                rationale=rationale,
            )
        guidance = (session.get("working_publish_guidance") or "").strip()
        work_dir = Path(
            consol.get("workdir_path")
            or (self.settings.data_dir / "consolidations" / str(consol_id))
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "body.md").write_text(body, encoding="utf-8")
        (work_dir / "findings.json").write_text(findings_json_out, encoding="utf-8")
        (work_dir / "meta.json").write_text(
            json.dumps(
                {"recommended_action": action, "rationale": rationale}, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        guidance_path = work_dir / "publish-guidance.md"
        if guidance:
            guidance_path.write_text(guidance + "\n", encoding="utf-8")
        elif guidance_path.exists():
            try:
                guidance_path.unlink()
            except OSError:
                pass
        stale_publish = work_dir / "publish-body.md"
        if stale_publish.exists():
            try:
                stale_publish.unlink()
            except OSError:
                pass

        updated = update_consolidation(
            conn,
            int(consol_id),
            body=body,
            findings_json=findings_json_out,
            recommended_action=action,
            recommended_action_rationale=rationale,
            ready_to_post=0,
            publish_body=None,
            publish_status="queued",
            publish_started_at=utcnow(),
            publish_finished_at=None,
        )
        update_chat_session(
            conn,
            session_id,
            working_findings_json=findings_json_out,
            updated_at=utcnow(),
        )

        runner = getattr(self, "_consolidation_runner", None)
        if runner is not None:
            runner.enqueue_publish(int(consol_id))
            updated = get_consolidation(conn, int(consol_id))
        else:
            # No worker bound (tests): leave draft stale rather than pretend queued.
            updated = update_consolidation(
                conn,
                int(consol_id),
                publish_status=None,
                publish_started_at=None,
            )

        return {
            "session": get_chat_session(conn, session_id),
            "consolidation_id": int(consol_id),
            "consolidation": updated,
        }

    def finish_session(self, session_id: int) -> dict[str, Any]:
        """Alias for apply_findings (legacy /done route)."""
        return self.apply_findings(session_id)

    def bind_consolidation_runner(self, runner: Any) -> None:
        self._consolidation_runner = runner

    def has_active_rounds(self) -> bool:
        with self._lock:
            return bool(self._busy) or bool(self._procs)
