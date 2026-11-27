"""AGTAdapter: vsl-core's VSLAdapter contract for the Agent Governance Toolkit.

The compiled gates use the same evaluation as VSLInterceptor (evaluate), so
run_conformance_suite(AGTAdapter()) checks the logic the interceptor runs.
A compiled gate can also be called inline, anywhere code is about to act.
Like every VSL adapter's compiled gates, they write nothing to a ledger:
VSLInterceptor does the recording.
"""

from __future__ import annotations

from typing import Any

from vsl_core.conformance.protocol import CompiledGate
from vsl_core.constructs import Invariant, PreNode
from vsl_core.exceptions import AutomationDeniedException, InvariantViolation

from .interceptor import evaluate


class AGTAdapter:
    """Compiles PreNodes and Invariants into gates that raise before the action."""

    def __init__(self, *, identity_key: str = "vsl-agt") -> None:
        if not identity_key:
            raise ValueError("identity_key must be a non-empty string.")
        self.identity_key = identity_key

    def compile_pre_node(self, pre_node: PreNode) -> CompiledGate:
        identity_key = self.identity_key

        async def gate(candidate_input: Any) -> None:
            allowed, _ = await evaluate(pre_node, candidate_input)
            if not allowed:
                raise AutomationDeniedException(reason=f"Pre-node '{pre_node.name}' insufficient Gamma", identity_key=identity_key)

        return gate

    def compile_invariant(self, invariant: Invariant) -> CompiledGate:
        identity_key = self.identity_key

        async def gate(candidate_input: Any) -> None:
            holds, _ = await evaluate(invariant, candidate_input)
            if not holds:
                terminal = invariant.on_violation.name if invariant.on_violation is not None else None
                reason = f"Invariant '{invariant.name}' violated"
                if terminal is not None:
                    reason += f" -- entering terminal state '{terminal}'"
                raise InvariantViolation(
                    reason=reason, identity_key=identity_key, invariant_name=invariant.name, terminal_state_name=terminal
                )

        return gate
