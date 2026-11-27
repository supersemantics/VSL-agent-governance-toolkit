"""VSLToolCallInterceptor: VSL gates on AGT's own ToolCallInterceptor protocol.

AGT's Python integrations (agent_os.integrations) accept any object with
`intercept(request: ToolCallRequest) -> ToolCallResult`: CompositeInterceptor
chains them (every one must allow), and adapters such as the CrewAI one call
them before each tool runs. This class is that protocol, backed by a
VSLInterceptor, so VSL gates sit in the same chain as AGT's PolicyInterceptor.

The protocol is synchronous and runs before the tool only:

- VSL monitors and rules are async, so they run on a private event loop
  thread with a timeout. A gate that does not decide in time is a deny.
- There is no after-tool hook, so call record_action(request) after the tool
  runs; without it, enforcement() reads NOT_OBSERVED for an allowed call.

agent_os is imported only when a call is intercepted: this package does not
depend on AGT itself.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import uuid
from typing import Any

from vsl_core.ledger import LedgerEntry

from .interceptor import VSLInterceptor

#: The reason returned when the gates don't decide within the timeout.
TIMEOUT_REASON = "vsl:gate_timeout"

DEFAULT_TIMEOUT_SEC = 5.0


class _LoopThread:
    """One private asyncio loop on a daemon thread, started on first use."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def run(self, coro: Any, timeout: float) -> Any:
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, name="vsl-agt-gates", daemon=True).start()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except BaseException:
            future.cancel()
            raise


class VSLToolCallInterceptor:
    """AGT's ToolCallInterceptor, running a VSLInterceptor's gates."""

    def __init__(self, vsl: VSLInterceptor, *, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> None:
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive.")
        self.vsl = vsl
        self.timeout_sec = timeout_sec
        self._runner = _LoopThread()

    @staticmethod
    def decision_id(request: Any) -> str:
        """The request's call_id; a request without one (CrewAI's) gets a UUID,
        kept in request.metadata so record_action finds the same id."""
        if request.call_id:
            return str(request.call_id)
        return request.metadata.setdefault("vsl_decision_id", str(uuid.uuid4()))

    def intercept(self, request: Any) -> Any:
        from agent_os.integrations.base import ToolCallResult

        if not self.vsl.gates(request.tool_name):
            return ToolCallResult(allowed=True)
        decision_id = self.decision_id(request)
        extra = {"agt": {"agent_id": request.agent_id, "call_id": request.call_id}}
        try:
            verdict = self._runner.run(
                self.vsl.check(request.tool_name, request.arguments, decision_id=decision_id, extra=extra),
                self.timeout_sec,
            )
        except (TimeoutError, concurrent.futures.TimeoutError):
            return ToolCallResult(allowed=False, reason=TIMEOUT_REASON, audit_entry={"vsl_decision_id": decision_id})
        audit = {"vsl_decision_id": decision_id}
        if verdict.evidence is not None:
            audit["vsl_evidence"] = verdict.evidence.to_wire()
        return ToolCallResult(allowed=verdict.decision.permits, reason=verdict.reason, audit_entry=audit)

    def record_action(self, request: Any, *, is_error: bool = False) -> LedgerEntry | None:
        """Record that the tool ran; call it after the tool, with the same request."""
        return self.vsl.record_action(
            request.tool_name, self.decision_id(request), is_error=is_error,
            extra={"agt": {"agent_id": request.agent_id, "call_id": request.call_id}},
        )
