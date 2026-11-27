"""VSLToolCallInterceptor: VSL gates on AGT's own ToolCallInterceptor protocol.

AGT's Python integrations (agent_os.integrations) accept any object with
`intercept(request: ToolCallRequest) -> ToolCallResult`: CompositeInterceptor
chains them (every one must allow), and adapters such as the CrewAI one call
them before each tool runs. This class is that protocol, backed by a
VSLInterceptor, so VSL gates sit in the same chain as AGT's PolicyInterceptor.

The protocol is synchronous and runs before the tool only:

- VSL monitors and rules are async, so they run on a private event loop
  thread with a timeout. A gate that does not decide in time is a deny.
- There is no after-tool hook. Wrap each gated tool with track() and its
  runs are recorded automatically: an allowed intercept leaves its decision
  id pending, and the tracked tool records its run under it (or under a new
  "unmediated/..." id if no allowed intercept came first, which enforcement()
  reads as AFTER_ACTION). Or call record_action(request) yourself after the
  tool runs. Without either, enforcement() reads NOT_OBSERVED for an allowed
  call. The pending id is per thread / asyncio task: intercept and run the
  tool in the same one, as CompositeInterceptor callers and AGT's CrewAI
  adapter do.

agent_os is imported only when a call is intercepted: this package does not
depend on AGT itself.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import inspect
import threading
import uuid
from collections.abc import Callable
from typing import Any, TypeVar

from vsl_core.ledger import LedgerEntry

from .interceptor import VSLInterceptor

#: The reason returned when the gates don't decide within the timeout.
TIMEOUT_REASON = "vsl:gate_timeout"

DEFAULT_TIMEOUT_SEC = 5.0

#: Prefix of the decision id a tracked tool records under when it ran with no
#: allowed intercept before it.
UNMEDIATED_PREFIX = "unmediated/"

F = TypeVar("F", bound=Callable[..., Any])


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
        # tool name -> the decision id of its latest allowed intercept, not yet run.
        self._pending: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
            f"vsl_agt_pending_{id(self)}", default={}
        )

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
        if verdict.decision.permits:
            self._pending.set({**self._pending.get(), request.tool_name: decision_id})
        return ToolCallResult(allowed=verdict.decision.permits, reason=verdict.reason, audit_entry=audit)

    def record_action(self, request: Any, *, is_error: bool = False) -> LedgerEntry | None:
        """Record that the tool ran; call it after the tool, with the same
        request. Not needed for a tool wrapped with track()."""
        decision_id = self.decision_id(request)
        pending = self._pending.get()
        if pending.get(request.tool_name) == decision_id:
            self._pending.set({k: v for k, v in pending.items() if k != request.tool_name})
        return self.vsl.record_action(
            request.tool_name, decision_id, is_error=is_error,
            extra={"agt": {"agent_id": request.agent_id, "call_id": request.call_id, "mediated": True}},
        )

    def track(self, fn: F, *, tool_name: str | None = None) -> F:
        """Wrap a gated tool so each run is recorded after it returns or raises.

        Give the wrapped function to the framework in place of the original.
        A run with no allowed intercept for this tool before it, in the same
        thread or task, is recorded under a new "unmediated/..." decision id.
        """
        name = tool_name or getattr(fn, "__name__", "")
        if not self.vsl.gates(name):
            raise ValueError(f"'{name}' has no gates, so its runs would not be recorded; pass tool_name= or gate it.")

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def tracked_async(*args: Any, **kwargs: Any) -> Any:
                decision_id = self._take(name)
                try:
                    result = await fn(*args, **kwargs)
                except BaseException:
                    self._record_run(name, decision_id, is_error=True)
                    raise
                self._record_run(name, decision_id, is_error=False)
                return result

            return tracked_async  # type: ignore[return-value]

        @functools.wraps(fn)
        def tracked(*args: Any, **kwargs: Any) -> Any:
            decision_id = self._take(name)
            try:
                result = fn(*args, **kwargs)
            except BaseException:
                self._record_run(name, decision_id, is_error=True)
                raise
            self._record_run(name, decision_id, is_error=False)
            return result

        return tracked  # type: ignore[return-value]

    def _take(self, tool: str) -> str | None:
        pending = self._pending.get()
        decision_id = pending.get(tool)
        if decision_id is not None:
            self._pending.set({k: v for k, v in pending.items() if k != tool})
        return decision_id

    def _record_run(self, tool: str, decision_id: str | None, *, is_error: bool) -> LedgerEntry | None:
        mediated = decision_id is not None
        return self.vsl.record_action(
            tool, decision_id if mediated else f"{UNMEDIATED_PREFIX}{uuid.uuid4()}", is_error=is_error,
            extra={"agt": {"mediated": mediated}},
        )
