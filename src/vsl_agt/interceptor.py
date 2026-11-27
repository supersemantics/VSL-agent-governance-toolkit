"""VSLInterceptor: VSL gates as an Agent Hooks interceptor.

Microsoft's Agent Governance Toolkit (AGT) mediates an agent through the
Agent Hooks contract (agent-hooks/0.1): its framework adapters build an
AgentContext at each interception point and pass it to every registered
interceptor, whose Verdict the host enforces. Register a VSLInterceptor on
the host's InterceptionEmitter and VSL gates run there, next to AGT's own
policy, in whichever framework AGT is adapting.

What it does at the two tool-call points:

- pre_tool_call: runs the PreNodes and Invariants declared for the tool, in
  order, records each decision with VerbaLedger.write_gate_decision, and
  returns allow or deny. A denied call is closed in the ledger (PRE_NODE
  and VERIFICATION) so audit() passes; an Invariant naming a TerminalState
  enters it (TERMINAL), and every gated call is denied until release().
- post_tool_call: records that the tool ran with VerbaLedger.write_action.
  VerbaLedger.enforcement(decision_id) then shows, from record order,
  whether every gate decided before the tool ran.

Each gate decision and action record carries an `agent_hooks` link: the
session id and sequence that identify the host's own InterceptionRecord,
and the context identity that record carries as `input_identity`. So the
VSL ledger and the host's audit trail can be matched entry for entry. A
deny also carries the ledger entries' hashes in `evidence`, which the host
keeps on the record of the interceptor that decided.

Agent Hooks does not promise complete mediation: a code path the
framework never routes through an interception point is not seen here, so
the ledger can only speak for mediated calls.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agent_hooks import AgentContext, Decision, Evidence, InterceptionPoint, Verdict, context_identity
from vsl_core.constructs import Invariant, PreNode, TerminalState
from vsl_core.governance import GovernanceAuthority, HumanAuthorisedTransition
from vsl_core.identity import Evidence as TransitionEvidence
from vsl_core.ledger import LedgerEntry, LedgerEntryType, VerbaLedger, VerificationResult
from vsl_core.metrics import GammaEstimate

Gate = PreNode | Invariant

#: The points a VSLInterceptor needs; pass as `at=` when registering.
INTERCEPTION_POINTS: tuple[InterceptionPoint, ...] = (
    InterceptionPoint.PRE_TOOL_CALL,
    InterceptionPoint.POST_TOOL_CALL,
)

#: Every reason this interceptor returns starts with this prefix.
REASON_PREFIX = "vsl:"

#: The Agent Hooks evidence artefact name on every verdict.
EVIDENCE_ARTEFACT = "vsl-core VerbaLedger"


def decision_id_of(ctx: AgentContext) -> str:
    """The VSL decision id for a tool call: session id and tool-call id.

    Agent Hooks pairs pre_tool_call and post_tool_call by tool_call.id, so
    the gate decisions and the action record share this id.
    """
    return f"{ctx['session']['id']}/{ctx['tool_call']['id']}"


async def evaluate(gate: Gate, candidate: Any) -> tuple[bool, GammaEstimate | None]:
    """Run one gate on a candidate: (allowed, the PreNode's estimate or None).

    The same check vsl-core's reference adapter makes: a PreNode passes when
    its estimate is sufficient for its threshold; an Invariant when its rule
    returns True. A rule returning anything but a bool is an error.
    """
    if isinstance(gate, PreNode):
        estimate = await gate.monitor(candidate)
        if not isinstance(estimate, GammaEstimate):
            raise TypeError(f"PreNode '{gate.name}' monitor returned {type(estimate).__name__}, not a GammaEstimate")
        if not math.isfinite(estimate.gamma_hat):
            return False, estimate
        return bool(estimate.sufficient(gate.gamma_threshold)), estimate
    holds = await gate.rule(candidate)
    if not isinstance(holds, bool):
        raise TypeError(f"Invariant '{gate.name}' rule returned {type(holds).__name__}, not a bool")
    return holds, None


def hook_link(ctx: AgentContext) -> dict[str, Any]:
    """The fields that tie a ledger entry to the host's InterceptionRecord
    for the same emission: session id, sequence, point, context identity."""
    link: dict[str, Any] = {
        "session_id": ctx["session"]["id"],
        "sequence": ctx["sequence"],
        "interception_point": ctx["interception_point"],
    }
    try:
        link["context_identity"] = context_identity(ctx)
    except Exception:  # a context the core can't canonicalize; the host fails it closed anyway
        pass
    return link


def _pointer(entry: LedgerEntry) -> str:
    return f"urn:vsl:ledger-entry:{entry.entry_hash}"


class VSLInterceptor:
    """An Agent Hooks interceptor that runs VSL gates before tool calls and
    records them, and the calls, in a VerbaLedger.

    `gates` maps a tool name to the gates that must pass before it runs, in
    order; tools not named pass through without records. `candidate_input`
    turns the context into what the gates evaluate (default: the call's
    effective arguments, the context's `target`).
    """

    def __init__(
        self,
        gates: Mapping[str, Sequence[Gate]],
        *,
        ledger: VerbaLedger,
        identity_key: str,
        candidate_input: Callable[[AgentContext], Any] | None = None,
    ) -> None:
        if not identity_key:
            raise ValueError("identity_key must be a non-empty string: it is recorded on every ledger entry.")
        checked: dict[str, tuple[Gate, ...]] = {}
        for tool, tool_gates in gates.items():
            tool_gates = tuple(tool_gates)
            if not tool_gates:
                raise ValueError(f"Tool '{tool}' is listed with no gates; leave it out to let it pass through.")
            for gate in tool_gates:
                if not isinstance(gate, (PreNode, Invariant)):
                    raise TypeError(f"Gates for '{tool}' must be PreNode or Invariant, got {type(gate).__name__}.")
            checked[tool] = tool_gates
        self._gates = checked
        self.ledger = ledger
        self.identity_key = identity_key
        self._candidate_input = candidate_input or (lambda ctx: ctx.get("target"))
        self._terminal: TerminalState | None = None
        self._terminal_entry: LedgerEntry | None = None

    @property
    def points(self) -> tuple[InterceptionPoint, ...]:
        """The interception points to register this interceptor `at`."""
        return INTERCEPTION_POINTS

    @property
    def terminal_state(self) -> TerminalState | None:
        """The Terminal State the agent is in, or None."""
        return self._terminal

    async def intercept(self, context: AgentContext, /) -> Verdict:
        point = context.get("interception_point")
        if point == InterceptionPoint.PRE_TOOL_CALL.value:
            return await self._before_tool(context)
        if point == InterceptionPoint.POST_TOOL_CALL.value:
            return self._after_tool(context)
        return Verdict.allow()

    # ---- pre_tool_call -------------------------------------------------------

    async def _before_tool(self, ctx: AgentContext) -> Verdict:
        tool = ctx["tool_call"]["name"]
        if not self.gates(tool):
            return Verdict.allow()
        return await self.check(
            tool, self._candidate_input(ctx), decision_id=decision_id_of(ctx), extra={"agent_hooks": hook_link(ctx)}
        )

    def _after_tool(self, ctx: AgentContext) -> Verdict:
        tool = ctx["tool_call"]["name"]
        is_error = bool((ctx.get("tool_result") or {}).get("is_error", False))
        entry = self.record_action(tool, decision_id_of(ctx), is_error=is_error, extra={"agent_hooks": hook_link(ctx)})
        if entry is None:
            return Verdict.allow()
        return Verdict(decision=Decision.ALLOW, evidence=self._evidence([entry]))

    # ---- the gate check, shared by every surface -------------------------------

    def gates(self, tool: str) -> tuple[Gate, ...]:
        """The gates declared for `tool`, in order; empty if it is ungated."""
        return self._gates.get(tool, ())

    async def check(
        self, tool: str, candidate: Any, *, decision_id: str, extra: Mapping[str, Any] | None = None
    ) -> Verdict:
        """Run `tool`'s gates on `candidate`, record every decision under
        `decision_id`, and return the Verdict. `extra` is added to each
        gate-decision record (the host's correlation fields)."""
        tool_gates = self.gates(tool)
        if not tool_gates:
            return Verdict.allow()
        payload = {"tool": tool, **(extra or {})}

        if self._terminal is not None:
            held = self.ledger.write_monitor(
                identity_key=self.identity_key, drift_detected=True, decision_id=decision_id,
                extra_payload={**payload, "terminal_state": self._terminal.name},
            )
            self._close_denied(held, tool, decision_id)
            return self._deny(
                f"terminal_state:{self._terminal.name}",
                f"'{tool}' refused: the agent is in Terminal State '{self._terminal.name}' until a named human releases it.",
                [held],
            )

        entries: list[LedgerEntry] = []
        for gate in tool_gates:
            try:
                allowed, estimate = await evaluate(gate, candidate)
            except Exception as error:  # fail closed, and say so in the ledger
                entry = self.ledger.write_gate_decision(
                    identity_key=self.identity_key, primitive_name=gate.name, allowed=False,
                    decision_id=decision_id, extra_payload={**payload, "error": type(error).__name__},
                )
                entries.append(entry)
                self._close_denied(entry, tool, decision_id)
                return self._deny(f"gate_failed:{gate.name}", f"Gate '{gate.name}' failed ({type(error).__name__}); '{tool}' not run.", entries)

            if estimate is not None and math.isfinite(estimate.gamma_hat):
                entry = self.ledger.write_gate_decision(
                    identity_key=self.identity_key, primitive_name=gate.name, estimate=estimate,
                    gamma_threshold=gate.gamma_threshold, decision_id=decision_id, extra_payload=payload,
                )
            else:
                entry = self.ledger.write_gate_decision(
                    identity_key=self.identity_key, primitive_name=gate.name, allowed=allowed,
                    decision_id=decision_id, extra_payload=payload,
                )
            entries.append(entry)
            if allowed:
                continue

            self._close_denied(entry, tool, decision_id)
            if isinstance(gate, Invariant):
                if gate.on_violation is not None:
                    self._enter_terminal(gate.on_violation, entry, decision_id)
                    return self._deny(
                        f"invariant_violation:{gate.name}",
                        f"Invariant '{gate.name}' violated; entering Terminal State '{gate.on_violation.name}'.",
                        entries,
                    )
                return self._deny(f"invariant_violation:{gate.name}", f"Invariant '{gate.name}' violated; '{tool}' not run.", entries)
            return self._deny(f"insufficient_gamma:{gate.name}", f"PreNode '{gate.name}': Gamma not above the threshold; '{tool}' not run.", entries)

        return Verdict(decision=Decision.ALLOW, evidence=self._evidence(entries))

    def record_action(
        self, tool: str, decision_id: str, *, is_error: bool = False, extra: Mapping[str, Any] | None = None
    ) -> LedgerEntry | None:
        """Record that a gated tool ran (None for an ungated tool, which is not recorded)."""
        if not self.gates(tool):
            return None
        return self.ledger.write_action(
            identity_key=self.identity_key, action_name=tool, decision_id=decision_id,
            extra_payload={"is_error": is_error, **(extra or {})},
        )

    # ---- Terminal State ------------------------------------------------------

    def release(
        self, *, authority: GovernanceAuthority, authorised_by: str, evidence: TransitionEvidence
    ) -> HumanAuthorisedTransition:
        """Leave the Terminal State: a named human, with evidence, recorded as a
        HUMAN_AUTHORISED_TRANSITION caused by the TERMINAL entry."""
        if self._terminal is None or self._terminal_entry is None:
            raise RuntimeError("The agent is not in a Terminal State; there is nothing to release.")
        transition = HumanAuthorisedTransition.authorise(authority, authorised_by=authorised_by, evidence=evidence)
        self.ledger.write(
            LedgerEntryType.HUMAN_AUTHORISED_TRANSITION,
            identity_key=self.identity_key,
            caused_by=self._terminal_entry.entry_id,
            payload={
                "terminal_state": self._terminal.name,
                "authority": transition.authority.name,
                "authorised_by": transition.authorised_by,
                "evidence_hash": transition.authorisation_evidence_hash,
            },
        )
        self._terminal = None
        self._terminal_entry = None
        return transition

    def _enter_terminal(self, state: TerminalState, cause: LedgerEntry, decision_id: str) -> None:
        self._terminal_entry = self.ledger.write(
            LedgerEntryType.TERMINAL, identity_key=self.identity_key, decision_id=decision_id,
            caused_by=cause.entry_id, payload={"terminal_state": state.name},
        )
        self._terminal = state

    # ---- helpers -------------------------------------------------------------

    def _close_denied(self, cause: LedgerEntry, tool: str, decision_id: str) -> None:
        # A denial is drift as far as audit() is concerned: record the fallback
        # (the call not run) and its verification, causally linked.
        held = self.ledger.write(
            LedgerEntryType.PRE_NODE, identity_key=self.identity_key, decision_id=decision_id,
            caused_by=cause.entry_id, payload={"fallback": f"'{tool}' denied before it ran"},
        )
        self.ledger.write_verification(
            identity_key=self.identity_key, result=VerificationResult.SUFFICIENT, decision_id=decision_id,
            caused_by=held.entry_id, extra_payload={"verified": "deny returned to the Agent Hooks host"},
        )

    def _deny(self, reason: str, message: str, entries: list[LedgerEntry]) -> Verdict:
        return Verdict(decision=Decision.DENY, reason=REASON_PREFIX + reason, message=message, evidence=self._evidence(entries))

    @staticmethod
    def _evidence(entries: list[LedgerEntry]) -> Evidence:
        pointers: dict[str, str] = {}
        for entry in entries:
            key = entry.payload.get("primitive_name") or ("action" if "action" in entry.payload else entry.entry_type.value.lower())
            pointers[str(key)] = _pointer(entry)
        return Evidence(artefact=EVIDENCE_ARTEFACT, verification_pointers=pointers)
