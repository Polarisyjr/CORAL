"""OpenCode CLI subprocess lifecycle."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from coral.agent.runtime import AgentHandle, write_coral_log_entry
from coral.workspace.repo import _clean_env

logger = logging.getLogger(__name__)


def _is_completed_turn(line: str) -> bool:
    """Whether one OpenCode JSON-stream record completes a model turn."""
    try:
        return json.loads(line).get("type") == "step_finish"
    except (json.JSONDecodeError, AttributeError):
        return False


def _tee_and_limit(
    proc: subprocess.Popen,
    log_f,
    agent: str,
    log_path: Path,
    max_turns: int,
    verbose: bool,
) -> None:
    turns = 0
    limit_sent = False
    marker = log_path.with_suffix(".turn-limit.json")
    try:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, b""):
            decoded = line.decode("utf-8", errors="replace")
            if verbose:
                sys.stdout.write(f"[{agent}] {decoded}")
                sys.stdout.flush()
            log_f.write(decoded)
            log_f.flush()
            if _is_completed_turn(decoded):
                turns += 1
            if max_turns > 0 and turns >= max_turns and not limit_sent:
                limit_sent = True
                marker.write_text(
                    json.dumps(
                        {
                            "agent_id": agent,
                            "max_turns": max_turns,
                            "turns_completed": turns,
                            "terminal_reason": "max_turns",
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                logger.info(f"OpenCode agent {agent} reached max_turns={max_turns}")
                try:
                    # OpenCode installs handlers for SIGINT/SIGTERM and can begin
                    # another model step after either signal.  The limit is a hard
                    # request budget, so terminate the isolated agent process group.
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
    except Exception as e:
        logger.error(f"OpenCode stream thread error: {e}")
    finally:
        log_f.close()
        if proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass


def _extract_opencode_session_id(log_path: Path) -> str | None:
    """Extract session_id from an OpenCode JSON log.

    OpenCode `run --format json` emits JSON events. Session IDs appear
    in events with a "session_id" or "sessionId" field.
    """
    try:
        lines = log_path.read_text().strip().splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                sid = data.get("session_id") or data.get("sessionId")
                if sid:
                    return sid
            except json.JSONDecodeError:
                continue
    except Exception as e:
        logger.debug(f"Failed to extract session_id from {log_path}: {e}")
    return None


class OpenCodeRuntime:
    """Spawn and manage OpenCode CLI agent subprocesses.

    Uses `opencode run` for non-interactive operation.
    Resume uses `opencode run --continue --session <id>`.
    """

    @property
    def instruction_filename(self) -> str:
        return "AGENTS.md"

    @property
    def shared_dir_name(self) -> str:
        return ".opencode"

    def extract_session_id(self, log_path: Path) -> str | None:
        return _extract_opencode_session_id(log_path)

    def start(
        self,
        worktree_path: Path,
        coral_md_path: Path,
        model: str = "gpt-5",
        runtime_options: dict[str, Any] | None = None,
        max_turns: int = 200,
        log_dir: Path | None = None,
        verbose: bool = False,
        resume_session_id: str | None = None,
        prompt: str | None = None,
        prompt_source: str | None = None,
        task_name: str | None = None,
        task_description: str | None = None,
        gateway_url: str | None = None,
        gateway_api_key: str | None = None,
    ) -> AgentHandle:
        """Start an OpenCode agent in the given worktree."""
        agent_id_file = worktree_path / ".coral_agent_id"
        agent_id = agent_id_file.read_text().strip() if agent_id_file.exists() else "unknown"

        if log_dir is None:
            log_dir = worktree_path / ".opencode" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        log_idx = len(list(log_dir.glob(f"{agent_id}*.log")))
        log_path = log_dir / f"{agent_id}.{log_idx}.log"

        if prompt is None:
            if resume_session_id:
                prompt = "Session resumed. Continue where you left off."
                logger.info(f"Resuming agent {agent_id} session {resume_session_id}")
            else:
                prompt = "Begin."

        # Build command: opencode run [flags] <prompt>
        # Keep the full provider/model format (e.g. "minimax/MiniMax-M2.5")
        # so OpenCode knows which provider to use. When the gateway is active,
        # the provider's baseURL is patched in opencode.json to route through
        # the LiteLLM proxy.
        # --dir is required: without it, opencode (under non-tty stdin) walks
        # up the directory tree past the agent worktree to the enclosing git
        # repo root, where the per-agent `vllm` provider in
        # `<worktree>/.opencode/opencode.json` is not registered — every call
        # then fails with `Model not found: vllm/<model>`.
        cmd = [
            "opencode", "run",
            "--dir", str(worktree_path),
            "--model", model,
            "--format", "json",
        ]

        if resume_session_id:
            cmd.extend(["--continue", "--session", resume_session_id])

        # Prompt goes last as positional arg
        cmd.append(prompt)

        logger.info(f"Starting OpenCode agent {agent_id} in {worktree_path}")
        logger.info(f"Command: {' '.join(cmd)}")

        agent_env = _clean_env()
        worktree_venv = str(worktree_path / ".venv")
        agent_env["UV_PROJECT_ENVIRONMENT"] = worktree_venv
        # Set VIRTUAL_ENV so login shells (which reset PATH) can restore it
        # via /etc/profile.d/coral-venv.sh in Docker containers.
        agent_env["VIRTUAL_ENV"] = worktree_venv
        # Prepend .venv/bin to PATH for non-login shells
        venv_bin = str(worktree_path / ".venv" / "bin")
        agent_env["PATH"] = venv_bin + ":" + agent_env.get("PATH", "")

        # Per-agent OpenCode data dir. OpenCode keeps its session store
        # (SQLite: $XDG_DATA_HOME/opencode/opencode.db) under XDG_DATA_HOME,
        # defaulting to ~/.local/share. Without an override every concurrent
        # agent on the host shares ONE opencode.db and serializes on its single
        # SQLite write lock; past ~60 concurrent agents most of them hang/fail
        # at session creation ("SQLiteError: database is locked" ->
        # LockTimeoutError) before their first turn, so they never call the
        # model. Isolating the data dir per worktree removes that contention.
        # Keep capture state outside the agent worktree. Agents routinely run
        # `coral eval`, which commits or deletes unignored worktree files; a
        # database inside the worktree can therefore disappear before the
        # recording builder reads nested subagent tool calls.
        opencode_data = log_dir.parent.parent / "private/opencode-data" / agent_id
        opencode_data.mkdir(parents=True, exist_ok=True)
        agent_env["XDG_DATA_HOME"] = str(opencode_data)

        # Disable OpenCode's filesystem watcher. Each agent's watcher opens an
        # inotify instance, and the kernel caps these at
        # fs.inotify.max_user_instances (default 128 per user). Past ~60
        # concurrent agents that budget is exhausted, so the rest block forever
        # at watcher init (last log line: "watcher backend ... backend=inotify")
        # and never reach their first model call. The watcher only drives
        # interactive/TUI live-reload, which headless `opencode run` agents don't
        # use — so disabling it lifts the concurrency ceiling without a host
        # sysctl change.
        agent_env["OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER"] = "true"

        # Route through gateway if configured
        if gateway_url:
            agent_env["OPENAI_BASE_URL"] = gateway_url
            logger.info(f"OpenCode agent {agent_id}: routing via gateway at {gateway_url}")
        if gateway_api_key:
            agent_env["OPENAI_API_KEY"] = gateway_api_key

        log_file = open(log_path, "w", buffering=1)

        write_coral_log_entry(
            log_file,
            prompt=prompt,
            source=prompt_source or ("restart" if resume_session_id else "start"),
            agent_id=agent_id,
            session_id=resume_session_id,
            task_name=task_name,
            task_description=task_description,
        )

        process = subprocess.Popen(
            cmd,
            cwd=str(worktree_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=agent_env,
        )

        threading.Thread(
            target=_tee_and_limit,
            args=(process, log_file, agent_id, log_path, max_turns, verbose),
            daemon=True,
        ).start()
        log_file_ref = None

        logger.info(f"OpenCode agent {agent_id} started with PID {process.pid}")

        return AgentHandle(
            agent_id=agent_id,
            process=process,
            worktree_path=worktree_path,
            log_path=log_path,
            session_id=resume_session_id,
            recording_data_path=opencode_data,
            _log_file=log_file_ref,
        )
