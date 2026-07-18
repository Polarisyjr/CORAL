"""Best-effort runtime span recording for CORAL and its agent runtimes.

Recording is opt-in through ``CORAL_TRACE_PATH``.  Records from the manager,
agent CLI processes, the grader daemon, and OpenCode's recorder plugin all use
the same append-only JSONL file.  Instrumentation must never change an agent's
outcome, so write failures are deliberately ignored.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any

TRACE_PATH_ENV = "CORAL_TRACE_PATH"
TRACE_SCHEMA_VERSION = "coral.runtime-span/v1"


def _append(record: dict[str, Any]) -> None:
    raw_path = os.environ.get(TRACE_PATH_ENV)
    if not raw_path:
        return
    try:
        path = Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(record, sort_keys=True, default=str) + "\n").encode()
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    except Exception:
        # Tracing is observational and must never break a run.
        return


class RuntimeSpan(AbstractContextManager["RuntimeSpan"]):
    """One cross-process, append-only CORAL runtime span."""

    def __init__(
        self,
        name: str,
        *,
        actor_id: str | None = None,
        eval_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.span_id = f"span_{uuid.uuid4().hex}"
        self.actor_id = actor_id or os.environ.get("CORAL_AGENT_ID") or "coral"
        self.eval_id = eval_id
        self.parent_call_id = os.environ.get("CORAL_PARENT_CALL_ID")
        self.parent_session_id = os.environ.get("CORAL_PARENT_SESSION_ID")
        self.attributes = dict(attributes or {})
        self.result: dict[str, Any] = {}
        self.started_at_ns = time.time_ns()
        _append(self._base() | {"phase": "start"})

    def _base(self) -> dict[str, Any]:
        return {
            "schema_version": TRACE_SCHEMA_VERSION,
            "kind": "coral.span",
            "name": self.name,
            "span_id": self.span_id,
            "actor_id": self.actor_id,
            "eval_id": self.eval_id,
            "parent_call_id": self.parent_call_id,
            "parent_session_id": self.parent_session_id,
            "pid": os.getpid(),
            "started_at_ns": self.started_at_ns,
            "attributes": self.attributes,
        }

    def set_eval_id(self, eval_id: str) -> None:
        self.eval_id = eval_id

    def set_result(self, **result: Any) -> None:
        self.result.update(result)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        record = self._base() | {
            "phase": "end",
            "ended_at_ns": time.time_ns(),
            "status": "error" if exc is not None else "success",
            "result": self.result,
        }
        if exc is not None:
            record["error"] = {
                "type": exc_type.__name__ if exc_type is not None else type(exc).__name__,
                "message": str(exc),
            }
        _append(record)
        return None


def runtime_span(
    name: str,
    *,
    actor_id: str | None = None,
    eval_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> RuntimeSpan:
    return RuntimeSpan(
        name,
        actor_id=actor_id,
        eval_id=eval_id,
        attributes=attributes,
    )
