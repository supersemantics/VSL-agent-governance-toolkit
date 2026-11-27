"""VSLInterceptor through the real Agent Hooks host emitter (agent-hooks-sdk),
the way an AGT framework adapter drives it: build a context, emit
pre_tool_call, run the tool only if the emission proceeds, emit
post_tool_call."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agent_hooks import AgentContextBuilder, Decision, InterceptionBlocked, InterceptionEmitter, Verdict
from vsl_core.constructs import Invariant, PreNode, TerminalState
from vsl_core.governance import GovernanceAuthority
from vsl_core.identity import Evidence
from vsl_core.ledger import ACTION_KEY, Enforcement, LedgerEntryType, VerbaLedger
from vsl_core.metrics import GammaEstimate

from vsl_agt import REASON_PREFIX, VSLInterceptor, decision_id_of

HARD_CAP = 1000.0
BUDGET_LEFT = 1000.0
REFUND_ESCALATED = TerminalState(name="refund-escalated", description="Refunds stop until a supervisor reviews.")


async def within_cap(args: dict[str, Any]) -> bool:
    return 0 < float(args.get("amount", 0)) <= HARD_CAP


async def budget_headroom(args: dict[str, Any]) -> GammaEstimate:
    amount = float(args.get("amount", 0))
    return GammaEstimate(gamma_hat=BUDGET_LEFT / amount if amount > 0 else 0.0)


def refund_gates(on_violation: TerminalState | None = None) -> list:
    return [
        Invariant(name="refund-cap", description="Refunds of $1000 or less.", rule=within_cap, on_violation=on_violation),
        PreNode(name="refund-budget", description="Budget left / amount above 1.1.", monitor=budget_headroom, gamma_threshold=1.1),
    ]


class Host:
    """A minimal Agent Hooks host around one agent session."""

    def __init__(self, interceptor: VSLInterceptor, *extra_first: Any) -> None:
        self.ctx = AgentContextBuilder(agent_id="refund-agent", framework="test", session_id=str(uuid.uuid4()))
        self.emitter = InterceptionEmitter()
        for other in extra_first:
            self.emitter.register(other, name=type(other).__name__, at=interceptor.points)
        self.emitter.register(interceptor, name="vsl", at=interceptor.points)
        self.ran: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, args: dict[str, Any], *, ignore_denials: bool = False, fail: bool = False):
        call_id = str(uuid.uuid4())
        pre = self.ctx.pre_tool_call(call_id=call_id, name=name, args=args)
        if ignore_denials:
            record = await self.emitter.emit_unchecked(pre)
        else:
            record = (await self.emitter.emit(pre)).record
        self.ran.append((name, args))  # the tool's side effect
        post = self.ctx.post_tool_call(call_id=call_id, name=name, args=args, value="err" if fail else "ok", is_error=fail)
        await self.emitter.emit(post)
        return record, decision_id_of(pre)


@pytest.fixture
def ledger() -> VerbaLedger:
    return VerbaLedger()


def interceptor(ledger: VerbaLedger, **kwargs: Any) -> VSLInterceptor:
    return VSLInterceptor({"issue_refund": refund_gates(**kwargs)}, ledger=ledger, identity_key="refund-agent")


async def test_an_allowed_call_runs_and_the_ledger_shows_the_gates_decided_first(ledger):
    host = Host(interceptor(ledger))
    record, decision_id = await host.call("issue_refund", {"amount": 120.0})
    assert host.ran == [("issue_refund", {"amount": 120.0})]
    assert record.verdict.decision is Decision.ALLOW
    assert ledger.enforcement(decision_id) == Enforcement.BEFORE_ACTION
    gates = [e for e in ledger.store.all_entries() if "primitive_name" in e.payload]
    assert [e.payload["primitive_name"] for e in gates] == ["refund-cap", "refund-budget"]
    assert gates[1].payload["gamma_hat"] == pytest.approx(BUDGET_LEFT / 120.0)


async def test_every_ledger_entry_names_the_host_record_it_belongs_to(ledger):
    host = Host(interceptor(ledger))
    await host.call("issue_refund", {"amount": 120.0})
    pre, post = host.emitter.results
    entries = [e for e in ledger.store.all_entries() if "agent_hooks" in e.payload]
    assert len(entries) == 3  # two gate decisions, one action
    for entry in entries:
        record = pre if "primitive_name" in entry.payload else post
        link = entry.payload["agent_hooks"]
        assert (link["session_id"], link["sequence"], link["interception_point"]) == (
            record.session_id, record.sequence, record.interception_point.value)
        assert link["context_identity"] == record.input_identity


async def test_a_deny_record_carries_the_ledger_hashes_as_evidence(ledger):
    host = Host(interceptor(ledger))
    with pytest.raises(InterceptionBlocked) as blocked:
        await host.call("issue_refund", {"amount": 950.0})
    by_name = {e.payload["primitive_name"]: e for e in ledger.store.all_entries() if "primitive_name" in e.payload}
    pointers = blocked.value.result.verdict.evidence.verification_pointers
    assert pointers == {name: f"urn:vsl:ledger-entry:{entry.entry_hash}" for name, entry in by_name.items()}


async def test_a_budget_denial_blocks_the_call_before_it_runs(ledger):
    host = Host(interceptor(ledger))
    with pytest.raises(InterceptionBlocked) as blocked:
        await host.call("issue_refund", {"amount": 950.0})  # 1000 / 950 = 1.05, not above 1.1
    assert host.ran == []
    verdict = blocked.value.result.verdict
    assert verdict.decision is Decision.DENY and verdict.reason == REASON_PREFIX + "insufficient_gamma:refund-budget"
    decision_id = next(e.decision_id for e in ledger.store.all_entries())
    assert ledger.enforcement(decision_id) == Enforcement.BEFORE_ACTION
    assert ledger.audit().all_passed


async def test_an_invariant_violation_enters_the_terminal_state_until_a_named_human_releases_it(ledger):
    vsl = interceptor(ledger, on_violation=REFUND_ESCALATED)
    host = Host(vsl)
    with pytest.raises(InterceptionBlocked) as first:
        await host.call("issue_refund", {"amount": 9000.0})
    assert first.value.result.verdict.reason == REASON_PREFIX + "invariant_violation:refund-cap"
    assert vsl.terminal_state is REFUND_ESCALATED

    with pytest.raises(InterceptionBlocked) as paused:
        await host.call("issue_refund", {"amount": 50.0})  # within every rule, still refused
    assert paused.value.result.verdict.reason == REASON_PREFIX + "terminal_state:refund-escalated"
    assert host.ran == []

    vsl.release(
        authority=GovernanceAuthority(name="Refunds Supervisor", escalation_contact="supervisor@example.com"),
        authorised_by="j.smith",
        evidence=Evidence(root_cause_analysis="a $9000 refund was requested", specification_update_proposal="cap at intake"),
    )
    _, decision_id = await host.call("issue_refund", {"amount": 50.0})
    assert host.ran == [("issue_refund", {"amount": 50.0})]
    assert ledger.enforcement(decision_id) == Enforcement.BEFORE_ACTION

    kinds = [e.entry_type for e in ledger.store.all_entries()]
    assert kinds.index(LedgerEntryType.TERMINAL) < kinds.index(LedgerEntryType.HUMAN_AUTHORISED_TRANSITION)
    assert ledger.audit().all_passed and ledger.verify_integrity()


async def test_release_without_a_terminal_state_is_refused(ledger):
    with pytest.raises(RuntimeError):
        interceptor(ledger).release(
            authority=GovernanceAuthority(name="a", escalation_contact="a@example.com"),
            authorised_by="j.smith", evidence=Evidence(root_cause_analysis="r", specification_update_proposal="p"),
        )


async def test_a_failing_gate_fails_closed_and_says_so_in_the_ledger(ledger):
    async def broken(_args):
        raise ConnectionError("judge service down")

    vsl = VSLInterceptor({"issue_refund": [Invariant(name="judge", description="d", rule=broken)]}, ledger=ledger, identity_key="a")
    host = Host(vsl)
    with pytest.raises(InterceptionBlocked) as blocked:
        await host.call("issue_refund", {"amount": 10.0})
    assert blocked.value.result.verdict.reason == REASON_PREFIX + "gate_failed:judge"
    assert host.ran == []
    gate = next(e for e in ledger.store.all_entries() if "primitive_name" in e.payload)
    assert gate.payload["allowed"] is False and gate.payload["error"] == "ConnectionError"


async def test_a_rule_that_returns_a_non_bool_fails_closed(ledger):
    async def sloppy(_args):
        return "yes"

    host = Host(VSLInterceptor({"t": [Invariant(name="sloppy", description="d", rule=sloppy)]}, ledger=ledger, identity_key="a"))
    with pytest.raises(InterceptionBlocked):
        await host.call("t", {})
    assert host.ran == []


async def test_ungated_tools_pass_through_with_no_records(ledger):
    host = Host(interceptor(ledger))
    await host.call("lookup_order", {"order_id": "A100"})
    assert host.ran == [("lookup_order", {"order_id": "A100"})]
    assert list(ledger.store.all_entries()) == []


async def test_a_host_that_runs_a_denied_call_anyway_shows_up_as_after_action(ledger):
    host = Host(interceptor(ledger))
    record, decision_id = await host.call("issue_refund", {"amount": 9000.0}, ignore_denials=True)
    assert not record.proceeds and host.ran  # the host ignored the deny
    assert ledger.enforcement(decision_id) == Enforcement.AFTER_ACTION


async def test_a_tool_error_is_recorded_on_the_action(ledger):
    host = Host(interceptor(ledger))
    _, decision_id = await host.call("issue_refund", {"amount": 120.0}, fail=True)
    action = next(e for e in ledger.store.all_entries() if ACTION_KEY in e.payload)
    assert action.payload["is_error"] is True
    assert ledger.enforcement(decision_id) == Enforcement.BEFORE_ACTION


async def test_an_earlier_policy_deny_stops_the_call_before_vsl_runs(ledger):
    class AgtPolicyDeny:
        def intercept(self, context):
            return Verdict.deny(reason="policy:blocked_tool")

    host = Host(interceptor(ledger), AgtPolicyDeny())
    with pytest.raises(InterceptionBlocked) as blocked:
        await host.call("issue_refund", {"amount": 120.0})
    assert blocked.value.result.verdict.reason == "policy:blocked_tool"
    assert host.ran == [] and list(ledger.store.all_entries()) == []


async def test_both_allow_and_the_vsl_record_still_holds(ledger):
    class AgtPolicyAllow:
        def intercept(self, context):
            return Verdict.allow()

    host = Host(interceptor(ledger), AgtPolicyAllow())
    _, decision_id = await host.call("issue_refund", {"amount": 120.0})
    assert ledger.enforcement(decision_id) == Enforcement.BEFORE_ACTION


def test_configuration_errors_are_caught_early(ledger):
    with pytest.raises(ValueError):
        VSLInterceptor({"t": []}, ledger=ledger, identity_key="a")
    with pytest.raises(TypeError):
        VSLInterceptor({"t": ["not a gate"]}, ledger=ledger, identity_key="a")
    with pytest.raises(ValueError):
        VSLInterceptor({}, ledger=ledger, identity_key="")


def test_gates_take_no_assurance_basis_and_warn_about_nothing(ledger):
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        VSLInterceptor({"issue_refund": refund_gates()}, ledger=ledger, identity_key="a")


# ---- the Terminal State survives a restart ------------------------------------------------------

from vsl_core.ledger import JsonlLedgerStore  # noqa: E402

SUPERVISOR = GovernanceAuthority(name="Refunds Supervisor", escalation_contact="supervisor@example.com")
RELEASE_EVIDENCE = Evidence(root_cause_analysis="a $9000 refund was requested", specification_update_proposal="cap at intake")


async def test_the_terminal_state_survives_a_restart_and_a_release_does_too(tmp_path):
    path = tmp_path / "decisions.jsonl"
    before = interceptor(VerbaLedger(JsonlLedgerStore(path)), on_violation=REFUND_ESCALATED)
    with pytest.raises(InterceptionBlocked):
        await Host(before).call("issue_refund", {"amount": 9000.0})

    after_restart = interceptor(VerbaLedger(JsonlLedgerStore(path)), on_violation=REFUND_ESCALATED)
    assert after_restart.terminal_state is REFUND_ESCALATED  # the declared state, not a copy
    host = Host(after_restart)
    with pytest.raises(InterceptionBlocked) as paused:
        await host.call("issue_refund", {"amount": 50.0})
    assert paused.value.result.verdict.reason == REASON_PREFIX + "terminal_state:refund-escalated"

    after_restart.release(authority=SUPERVISOR, authorised_by="j.smith", evidence=RELEASE_EVIDENCE)
    released = interceptor(VerbaLedger(JsonlLedgerStore(path)), on_violation=REFUND_ESCALATED)
    assert released.terminal_state is None
    _, decision_id = await Host(released).call("issue_refund", {"amount": 50.0})
    assert released.ledger.enforcement(decision_id) == Enforcement.BEFORE_ACTION
    assert released.ledger.audit().all_passed and released.ledger.verify_integrity()


async def test_a_terminal_state_no_gate_declares_any_more_still_holds(tmp_path):
    path = tmp_path / "decisions.jsonl"
    with pytest.raises(InterceptionBlocked):
        await Host(interceptor(VerbaLedger(JsonlLedgerStore(path)), on_violation=REFUND_ESCALATED)).call(
            "issue_refund", {"amount": 9000.0})
    reconfigured = interceptor(VerbaLedger(JsonlLedgerStore(path)))  # no on_violation now
    assert reconfigured.terminal_state is not None and reconfigured.terminal_state.name == "refund-escalated"
    with pytest.raises(InterceptionBlocked):
        await Host(reconfigured).call("issue_refund", {"amount": 50.0})


async def test_another_agents_terminal_state_does_not_hold_this_one(tmp_path):
    path = tmp_path / "decisions.jsonl"
    with pytest.raises(InterceptionBlocked):
        await Host(interceptor(VerbaLedger(JsonlLedgerStore(path)), on_violation=REFUND_ESCALATED)).call(
            "issue_refund", {"amount": 9000.0})
    other = VSLInterceptor({"issue_refund": refund_gates(REFUND_ESCALATED)}, ledger=VerbaLedger(JsonlLedgerStore(path)), identity_key="other-agent")
    assert other.terminal_state is None
