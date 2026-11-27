"""VSLToolCallInterceptor inside AGT's own interceptor chain
(agent_os.integrations.base from agent-governance-toolkit-core)."""

from __future__ import annotations

import asyncio
import uuid
import warnings

import pytest

with warnings.catch_warnings():
    # agent_os warns on import in AGT 5.0 ("agent-os-kernel is deprecated"), even from -core.
    warnings.simplefilter("ignore", DeprecationWarning)
    base = pytest.importorskip("agent_os.integrations.base")

from vsl_core.constructs import Invariant, PreNode
from vsl_core.ledger import Enforcement, VerbaLedger
from vsl_core.metrics import GammaEstimate

from vsl_agt import TIMEOUT_REASON, VSLInterceptor, VSLToolCallInterceptor

ToolCallRequest, CompositeInterceptor = base.ToolCallRequest, base.CompositeInterceptor
PolicyInterceptor, GovernancePolicy = base.PolicyInterceptor, base.GovernancePolicy


async def within_cap(args) -> bool:
    return 0 < float(args.get("amount", 0)) <= 1000.0


async def budget(args) -> GammaEstimate:
    amount = float(args.get("amount", 0))
    return GammaEstimate(gamma_hat=1000.0 / amount if amount > 0 else 0.0)


def native(ledger: VerbaLedger, **kwargs) -> VSLToolCallInterceptor:
    gates = [
        Invariant(name="refund-cap", description="d", rule=within_cap),
        PreNode(name="refund-budget", description="d", monitor=budget, gamma_threshold=1.1),
    ]
    return VSLToolCallInterceptor(VSLInterceptor({"issue_refund": gates}, ledger=ledger, identity_key="refund-agent"), **kwargs)


def request(amount: float, *, call_id: str | None = None) -> ToolCallRequest:
    return ToolCallRequest(tool_name="issue_refund", arguments={"amount": amount}, call_id=call_id or str(uuid.uuid4()), agent_id="refund-agent")


def test_an_allowed_call_in_a_chain_with_agts_policy_interceptor():
    ledger = VerbaLedger()
    vsl = native(ledger)
    chain = CompositeInterceptor([PolicyInterceptor(GovernancePolicy(allowed_tools=["issue_refund"])), vsl])
    req = request(120.0)
    result = chain.intercept(req)
    assert result.allowed
    vsl.record_action(req)  # after the tool runs
    assert ledger.enforcement(req.call_id) == Enforcement.BEFORE_ACTION


def test_a_vsl_deny_blocks_the_chain_and_carries_the_ledger_evidence():
    ledger = VerbaLedger()
    chain = CompositeInterceptor([PolicyInterceptor(GovernancePolicy(allowed_tools=["issue_refund"])), native(ledger)])
    req = request(950.0)
    result = chain.intercept(req)
    assert not result.allowed and result.reason == "vsl:insufficient_gamma:refund-budget"
    assert result.audit_entry["vsl_decision_id"] == req.call_id
    assert set(result.audit_entry["vsl_evidence"]["verification_pointers"]) == {"refund-cap", "refund-budget"}
    assert ledger.enforcement(req.call_id) == Enforcement.BEFORE_ACTION
    assert ledger.audit().all_passed


def test_agts_own_policy_deny_comes_first_and_vsl_records_nothing():
    ledger = VerbaLedger()
    chain = CompositeInterceptor([PolicyInterceptor(GovernancePolicy(allowed_tools=["lookup_order"])), native(ledger)])
    assert not chain.intercept(request(120.0)).allowed
    assert list(ledger.store.all_entries()) == []


def test_an_allowed_call_without_record_action_is_not_observed():
    ledger = VerbaLedger()
    req = request(120.0)
    assert native(ledger).intercept(req).allowed
    assert ledger.enforcement(req.call_id) == Enforcement.NOT_OBSERVED


def test_a_request_without_a_call_id_keeps_one_decision_id():
    ledger = VerbaLedger()
    vsl = native(ledger)
    req = ToolCallRequest(tool_name="issue_refund", arguments={"amount": 120.0}, agent_id="crew-agent")
    assert vsl.intercept(req).allowed
    vsl.record_action(req)
    assert ledger.enforcement(req.metadata["vsl_decision_id"]) == Enforcement.BEFORE_ACTION


def test_a_gate_that_does_not_decide_in_time_is_a_deny():
    async def slow(_args) -> bool:
        await asyncio.sleep(2)
        return True

    vsl = VSLToolCallInterceptor(
        VSLInterceptor({"t": [Invariant(name="slow", description="d", rule=slow)]}, ledger=VerbaLedger(), identity_key="a"),
        timeout_sec=0.1,
    )
    result = vsl.intercept(ToolCallRequest(tool_name="t", arguments={}, call_id="c1"))
    assert not result.allowed and result.reason == TIMEOUT_REASON


def test_ungated_tools_pass():
    ledger = VerbaLedger()
    assert native(ledger).intercept(ToolCallRequest(tool_name="lookup_order", arguments={})).allowed
    assert list(ledger.store.all_entries()) == []
