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


# ---- track(): runs recorded automatically ---------------------------------------------------------

from vsl_core.ledger import ACTION_KEY  # noqa: E402

from vsl_agt import UNMEDIATED_PREFIX  # noqa: E402


def actions(ledger):
    return [e for e in ledger.store.all_entries() if ACTION_KEY in e.payload]


def test_a_tracked_tool_records_its_run_after_an_allowed_intercept():
    ledger = VerbaLedger()
    vsl = native(ledger)
    issue_refund = vsl.track(lambda amount: f"refunded {amount}", tool_name="issue_refund")
    chain = CompositeInterceptor([PolicyInterceptor(GovernancePolicy(allowed_tools=["issue_refund"])), vsl])
    req = request(120.0)
    assert chain.intercept(req).allowed
    assert issue_refund(amount=120.0) == "refunded 120.0"
    assert ledger.enforcement(req.call_id) == Enforcement.BEFORE_ACTION
    assert [a.payload["agt"]["mediated"] for a in actions(ledger)] == [True]


def test_a_crewai_shaped_call_with_no_call_id_is_paired_too():
    ledger = VerbaLedger()
    vsl = native(ledger)

    def issue_refund(amount):
        return f"refunded {amount}"

    tracked = vsl.track(issue_refund)  # tool name from the function
    req = ToolCallRequest(tool_name="issue_refund", arguments={"amount": 120.0}, agent_id="crew-agent")
    assert vsl.intercept(req).allowed  # what AGT's CrewAI adapter does, then the original _run
    tracked(120.0)
    assert ledger.enforcement(req.metadata["vsl_decision_id"]) == Enforcement.BEFORE_ACTION


def test_a_tracked_tool_run_without_an_intercept_shows_as_after_action():
    ledger = VerbaLedger()
    tracked = native(ledger).track(lambda amount: "refunded", tool_name="issue_refund")
    tracked(120.0)  # nothing asked the gates first
    (action,) = actions(ledger)
    assert action.decision_id.startswith(UNMEDIATED_PREFIX) and action.payload["agt"]["mediated"] is False
    assert ledger.enforcement(action.decision_id) == Enforcement.AFTER_ACTION


def test_a_denied_intercept_leaves_nothing_pending_so_a_run_anyway_is_caught():
    ledger = VerbaLedger()
    vsl = native(ledger)
    tracked = vsl.track(lambda amount: "refunded", tool_name="issue_refund")
    assert not vsl.intercept(request(950.0)).allowed
    tracked(950.0)  # the caller ignored the deny
    assert ledger.enforcement(actions(ledger)[0].decision_id) == Enforcement.AFTER_ACTION


def test_each_intercept_pairs_with_one_run():
    ledger = VerbaLedger()
    vsl = native(ledger)
    tracked = vsl.track(lambda amount: "refunded", tool_name="issue_refund")
    first = request(120.0)
    assert vsl.intercept(first).allowed
    tracked(120.0)
    tracked(120.0)  # a second run with no new intercept
    assert ledger.enforcement(first.call_id) == Enforcement.BEFORE_ACTION
    assert [a.decision_id.startswith(UNMEDIATED_PREFIX) for a in actions(ledger)] == [False, True]


def test_a_tool_that_raises_is_recorded_as_an_error_and_the_error_propagates():
    ledger = VerbaLedger()
    vsl = native(ledger)

    def issue_refund(amount):
        raise RuntimeError("payment service down")

    tracked = vsl.track(issue_refund)
    req = request(120.0)
    assert vsl.intercept(req).allowed
    with pytest.raises(RuntimeError):
        tracked(120.0)
    (action,) = actions(ledger)
    assert action.payload["is_error"] is True and ledger.enforcement(req.call_id) == Enforcement.BEFORE_ACTION


async def test_an_async_tool_is_tracked_in_its_task():
    ledger = VerbaLedger()
    vsl = native(ledger)

    async def issue_refund(amount):
        return f"refunded {amount}"

    tracked = vsl.track(issue_refund)
    req = request(120.0)
    assert vsl.intercept(req).allowed
    assert await tracked(120.0) == "refunded 120.0"
    assert ledger.enforcement(req.call_id) == Enforcement.BEFORE_ACTION


def test_record_action_by_hand_clears_the_pending_decision():
    ledger = VerbaLedger()
    vsl = native(ledger)
    tracked = vsl.track(lambda amount: "refunded", tool_name="issue_refund")
    req = request(120.0)
    assert vsl.intercept(req).allowed
    vsl.record_action(req)
    tracked(120.0)  # not paired with req: it was already recorded
    assert actions(ledger)[1].decision_id.startswith(UNMEDIATED_PREFIX)


def test_tracking_an_ungated_tool_is_refused():
    with pytest.raises(ValueError):
        native(VerbaLedger()).track(lambda: None, tool_name="lookup_order")
