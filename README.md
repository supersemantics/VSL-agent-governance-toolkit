# vsl-agt

[VSL](https://github.com/supersemantics/VSL-Core) gates and the VERBA ledger inside Microsoft's
[Agent Governance Toolkit](https://github.com/microsoft/agent-governance-toolkit) (AGT).

AGT decides, before a tool call runs, whether its policy allows it. vsl-agt adds VSL's gates to that
same decision, and keeps a hash-chained record that shows, call by call, whether the gates decided
before the tool ran.

**Is:**
- `VSLInterceptor`: an [Agent Hooks](https://github.com/responsibleai/agent-hooks) interceptor
  (`agent-hooks/0.1`, the interception contract AGT's policy engine is built on). Register it on a
  host's `InterceptionEmitter` at `pre_tool_call` and `post_tool_call`.
- `VSLToolCallInterceptor`: AGT's own `ToolCallInterceptor` protocol (`agent_os.integrations`), for
  `CompositeInterceptor` chains and the adapters that call interceptors (CrewAI).
- `AGTAdapter`: vsl-core's `VSLAdapter`, passing `run_conformance_suite` with no failures.

**Is not:**
- A replacement for AGT's policy engine, identity, trust scoring or sandboxing. It runs next to them.
- Complete mediation. Agent Hooks says so itself: a code path the framework never routes through an
  interception point is not seen, so the ledger speaks only for the calls that were.

## What VSL adds to AGT

Each row is a limit AGT documents about itself, and what vsl-agt does about it.

| AGT, in its own docs | vsl-agt |
|---|---|
| The audit trail "records what the agent attempted", not whether the action ran ([LIMITATIONS](https://github.com/microsoft/agent-governance-toolkit/blob/main/docs/LIMITATIONS.md) §2) | `post_tool_call` records that the tool ran (`write_action`); `ledger.enforcement(decision_id)` computes, from record order, whether every gate decided first |
| The decision fields are outside the audit entry hash in v1.0 ([AUDIT-COMPLIANCE-1.0](https://github.com/microsoft/agent-governance-toolkit/blob/main/docs/specs/AUDIT-COMPLIANCE-1.0.md) §4.3.1) | every VSL gate decision is a hash-chained ledger entry, decision included |
| No correlation across a sequence of allowed actions (LIMITATIONS §1) | vsl-core `Cluster` Invariants and handovers, written to the same ledger |
| A recorded kill is not proof of termination (LIMITATIONS §13) | an Invariant can name a `TerminalState`: every gated call is denied until a named human releases it, recorded as a `HUMAN_AUTHORISED_TRANSITION` |
| Policy verdicts are allow, deny or transform | a `PreNode` gates on a measured ratio (Gamma) against a threshold, recorded with its value |

## Install

```bash
pip install vsl-agt            # with Agent Hooks
pip install "vsl-agt[agt]"     # also AGT core, for VSLToolCallInterceptor
```

`agent-hooks-sdk` is a beta (0.1.0b3) and is pinned exactly. vsl-core comes from PyPI as
`super-semantics-vsl` (0.4.x).

## Agent Hooks: `VSLInterceptor`

```python
from agent_hooks import AgentContextBuilder, InterceptionBlocked, InterceptionEmitter
from vsl_core.constructs import Invariant, PreNode, TerminalState
from vsl_core.ledger import JsonlLedgerStore, VerbaLedger
from vsl_core.metrics import GammaEstimate
from vsl_agt import VSLInterceptor, decision_id_of

async def within_cap(args):
    return 0 < args["amount"] <= 1000

async def budget_headroom(args):
    return GammaEstimate(gamma_hat=budget_left() / args["amount"] if args["amount"] > 0 else 0.0)

ledger = VerbaLedger(JsonlLedgerStore("decisions.jsonl", fsync=True))
vsl = VSLInterceptor(
    {"issue_refund": [
        Invariant(name="refund-cap", description="Refunds of $1000 or less.", rule=within_cap,
                  on_violation=TerminalState(name="refund-escalated", description="Refunds stop for review.")),
        PreNode(name="refund-budget", description="Budget left / amount above 1.1.",
                monitor=budget_headroom, gamma_threshold=1.1),
    ]},
    ledger=ledger,
    identity_key="refund-agent",
)

emitter = InterceptionEmitter()
emitter.register(vsl, name="vsl", at=vsl.points)
```

A host (an AGT framework adapter, or your own loop) emits `pre_tool_call` before the tool and
`post_tool_call` after it. A denial raises `InterceptionBlocked` and the tool never runs:

```python
ctx = AgentContextBuilder(agent_id="refund-agent", framework="my-agent", session_id=session_id)
pre = ctx.pre_tool_call(call_id=call_id, name="issue_refund", args={"amount": 120.0})
await emitter.emit(pre)                       # raises InterceptionBlocked on a deny
value = issue_refund(amount=120.0)
await emitter.emit(ctx.post_tool_call(call_id=call_id, name="issue_refund", args={"amount": 120.0}, value=value))

ledger.enforcement(decision_id_of(pre))       # Enforcement.BEFORE_ACTION
```

What it records, under the decision id `<session id>/<tool call id>`:

- each gate's decision (`write_gate_decision`; a PreNode's with its Gamma and threshold);
- for a denial, the fallback and its verification, so `ledger.audit()` passes;
- for an Invariant naming a `TerminalState`, a `TERMINAL` entry. Every gated call is then denied
  (`vsl:terminal_state:<name>`) until `vsl.release(authority=..., authorised_by=..., evidence=...)`;
- after the tool, the action (`write_action`, with `is_error`).

**Matching the two audit trails.** Every gate decision and action entry carries an `agent_hooks`
field: the session id, sequence and point of the host's `InterceptionRecord` for the same emission,
and the context identity that record carries as `input_identity`. A deny also returns the ledger
entries' hashes in `evidence`, which the host keeps on the record. (The host keeps no evidence for a
plain allow, so use the `agent_hooks` field to match allowed calls.)

Denial reasons start with `vsl:`: `insufficient_gamma:<gate>`, `invariant_violation:<gate>`,
`terminal_state:<state>`, `gate_failed:<gate>`. A gate that raises, or a rule that returns anything
but a bool, is a deny (fail closed), and the ledger records the error.

Register an interceptor at every point your host emits: in `enforce` mode, Agent Hooks denies an
emission with no interceptor bound (`host_error:no_interceptor`). `vsl.points` is the two tool-call
points; the rest belong to AGT's own interceptors.

## AGT's ToolCallInterceptor: `VSLToolCallInterceptor`

```python
from agent_os.integrations.base import CompositeInterceptor, GovernancePolicy, PolicyInterceptor, ToolCallRequest
from vsl_agt import VSLToolCallInterceptor

vsl_native = VSLToolCallInterceptor(vsl)   # the VSLInterceptor above
chain = CompositeInterceptor([PolicyInterceptor(GovernancePolicy(allowed_tools=["issue_refund"])), vsl_native])

request = ToolCallRequest(tool_name="issue_refund", arguments={"amount": 120.0}, call_id=call_id, agent_id="refund-agent")
if chain.intercept(request).allowed:
    issue_refund(amount=120.0)
    vsl_native.record_action(request)
```

AGT's protocol is synchronous and runs before the tool only:

- the VSL gates run on a private event loop thread; a gate that doesn't decide within `timeout_sec`
  (default 5 s) is a deny, `vsl:gate_timeout`;
- there is no after-tool hook, so call `record_action(request)` after the tool runs. Without it,
  `enforcement()` reads `NOT_OBSERVED` for an allowed call;
- the result's `audit_entry` carries `vsl_decision_id` and the ledger `vsl_evidence`.

`agent_os` is imported only when a call is intercepted. In AGT 5.0 importing it warns
(`agent-os-kernel is deprecated`), even from `agent-governance-toolkit-core`.

## Microsoft Agent Framework

AGT's Agent Framework integration is middleware (`GovernancePolicyMiddleware` and others). VSL
already has Agent Framework middleware: [vsl-maf](https://github.com/supersemantics/VSL-Microsoft-agent-framework)
in Python and [VSL-Microsoft-agent-framework.NET](https://github.com/supersemantics/VSL-Microsoft-agent-framework.NET).
Put both in the agent's middleware list.

## What a gate reports

Gates take no assurance claim: a `PreNode` or `Invariant` is built from its name, description and
monitor or rule. Whether a gate held before its action is computed from the ledger (see
[vsl-core's "What a gate reports"](https://github.com/supersemantics/VSL-Core#what-a-gate-reports)).

## Status

Alpha. Tested against the real packages, not mocks:

- `agent-hooks-sdk` 0.1.0b3: allowed and denied calls through `InterceptionEmitter`, the Terminal
  State and its release, a failing gate, ledger entries matched to the host's records, AGT-style
  interceptors before VSL in the chain, and a host that runs a denied call anyway (the ledger reads
  `AFTER_ACTION`).
- `agent-governance-toolkit-core` 5.0.0: `VSLToolCallInterceptor` in a `CompositeInterceptor` with
  AGT's `PolicyInterceptor`, requests without a call id, and the timeout.
- `run_conformance_suite(AGTAdapter())` is empty.

Known gaps: the Terminal State is held in memory, so it does not survive a restart; Agent Hooks is a
0.1 beta and AGT is in preview, so both APIs may change.

## License

MIT. Copyright Super Semantics.
