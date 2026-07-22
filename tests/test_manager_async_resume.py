from __future__ import annotations

import signal
import threading
from pathlib import Path
from types import SimpleNamespace

from coral.agent.manager import AgentManager


class _FakeHandle:
    def __init__(self, agent_id: str, *, alive: bool = True) -> None:
        self.agent_id = agent_id
        self.alive = alive
        self.process = SimpleNamespace(returncode=0)
        self.log_path = Path(f"/{agent_id}.log")
        self.requests: list[bool] = []
        self.signals: list[int] = []

    def request_interrupt(self, *, at_turn_boundary: bool = False) -> str:
        self.requests.append(at_turn_boundary)
        return "turn-boundary" if at_turn_boundary else "sigint"

    def signal_process_group(self, sig: int) -> None:
        self.signals.append(sig)


def _manager(handles: list[_FakeHandle], *, max_total_turns: int = 10) -> AgentManager:
    manager = object.__new__(AgentManager)
    manager.config = SimpleNamespace(
        agents=SimpleNamespace(
            count=len(handles),
            max_total_turns=max_total_turns,
            restart_exited=True,
            timeout=0,
        ),
        grader=SimpleNamespace(direction="maximize"),
    )
    manager.handles = handles
    manager._restart_counts = {}
    manager._pending_resumes = {}
    manager._agent_eval_counts = {}
    manager._agent_best_scores = {}
    manager._agent_evals_since_improvement = {}
    manager._running = True
    manager._stopping = False
    manager._stop_event = threading.Event()
    manager.verbose = False
    return manager


def test_pending_resume_is_non_blocking_and_coalesces_feedback() -> None:
    handle = _FakeHandle("agent-1")
    manager = _manager([handle], max_total_turns=3)

    assert manager._schedule_interrupt_and_resume(0, "first", "heartbeat:reflect")
    assert handle.requests == [True]
    assert manager._turn_count() == 1
    assert manager._turn_budget_has_capacity()

    assert manager._schedule_interrupt_and_resume(0, "latest", "heartbeat:pivot")
    assert handle.requests == [True]
    pending = manager._pending_resumes["agent-1"]
    assert pending.prompt == "latest"
    assert pending.prompt_source == "heartbeat:pivot"


def test_pending_resume_reserves_shared_turn_budget() -> None:
    first = _FakeHandle("agent-1")
    second = _FakeHandle("agent-2")
    manager = _manager([first, second], max_total_turns=3)

    assert manager._schedule_interrupt_and_resume(0, "feedback")
    assert not manager._turn_budget_has_capacity()
    assert not manager._schedule_interrupt_and_resume(1, "other feedback")
    assert second.requests == []


def test_pending_resume_restarts_after_process_exits() -> None:
    handle = _FakeHandle("agent-1")
    manager = _manager([handle])
    manager._schedule_interrupt_and_resume(0, "feedback", "heartbeat:reflect")
    handle.alive = False
    replacement = _FakeHandle("agent-1")
    restarted: list[tuple[int, str | None, str | None]] = []

    def restart(idx: int, prompt: str | None, prompt_source: str | None):
        restarted.append((idx, prompt, prompt_source))
        return replacement

    manager._restart_agent = restart
    manager._write_agent_pids = lambda: None
    manager._advance_pending_resumes()

    assert restarted == [(0, "feedback", "heartbeat:reflect")]
    assert manager.handles == [replacement]
    assert manager._pending_resumes == {}


def test_pending_resume_escalates_without_waiting() -> None:
    handle = _FakeHandle("agent-1")
    manager = _manager([handle])
    manager._schedule_interrupt_and_resume(0, "feedback")
    pending = manager._pending_resumes["agent-1"]

    pending.deadline = 0
    manager._advance_pending_resumes()
    assert handle.signals == [signal.SIGTERM]
    assert pending.phase == "terminating"

    pending.deadline = 0
    manager._advance_pending_resumes()
    assert handle.signals == [signal.SIGTERM, signal.SIGKILL]
    assert pending.phase == "killing"


def test_monitor_restarts_other_dead_agent_while_heartbeat_is_pending(monkeypatch) -> None:
    heartbeat_agent = _FakeHandle("agent-1")
    dead_agent = _FakeHandle("agent-2", alive=False)
    manager = _manager([heartbeat_agent, dead_agent])
    manager._schedule_interrupt_and_resume(0, "feedback", "heartbeat:reflect")
    replacement = _FakeHandle("agent-2")
    restarted: list[str] = []

    def restart(idx: int, prompt=None, prompt_source=None):
        restarted.append(manager.handles[idx].agent_id)
        manager._restart_counts[manager.handles[idx].agent_id] = 1
        return replacement

    manager._restart_agent = restart
    manager._write_agent_pids = lambda: None
    manager._get_seen_attempts = lambda: set()
    manager._filter_scored = lambda _attempts: set()
    manager._read_latest_attempt = lambda _attempts: None
    manager._get_eval_count = lambda: 0
    manager._stop_event.set()
    monkeypatch.setattr(signal, "signal", lambda *_args: None)

    manager.monitor_loop(check_interval=0)

    assert heartbeat_agent.requests == [True]
    assert restarted == ["agent-2"]
    assert manager.handles[1] is replacement
