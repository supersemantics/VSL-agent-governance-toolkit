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
- A Microsoft project, or Microsoft's AGT repository. This is Super Semantics' VSL adapter for AGT;
  AGT itself is at [microsoft/agent-governance-toolkit](https://github.com/microsoft/agent-governance-toolkit).
- A replacement for AGT's policy engine, identity, trust scoring or sandboxing. It runs next to them.
- Complete mediation. Agent Hooks says so itself: a code path the framework never routes through an
  interception point is not seen, so the ledger speaks only for the calls that were.

## How this adapter differs from the other VSL adapters

Every other VSL adapter connects VSL to an orchestration framework: a LangGraph node, LangChain or
Agent Framework middleware, a ROS 2 publisher. vsl-agt connects VSL to another governance layer.
AGT already sits between the agent and its tools; vsl-agt puts VSL's gates in the same place, so
both decide on the same call.

| | The other VSL adapters | vsl-agt |
|---|---|---|
| Plugs into | the framework's own hook | AGT's interception points, next to AGT's own policy |
| Who decides | VSL's gate alone | AGT's verdict and VSL's together: any deny stops the call |
| Ledger writes | the gate blocks; your code records the decision and the action (vsl-ros2's `vsl_guard` node records its own) | gate decisions, the fallback for each denial, the Terminal State and the tool's run are recorded for you |
| Terminal State | the gate raises; your code records it and holds the agent | the interceptor holds the agent, the hold survives a restart, and only a recorded human release ends it |
| Audit trail | the VSL ledger | the VSL ledger, each entry linked to AGT's record of the same call |
| A tool run with no check before it | not detected by the adapter | a tracked tool's unchecked run reads `AFTER_ACTION` |

## VSL adapters

Each adapter passes vsl-core's conformance suite and builds gates with no assurance claim. vsl-core
itself: [Python](https://github.com/supersemantics/VSL-Core) (`super-semantics-vsl`),
[TypeScript](https://github.com/supersemantics/VSL-Corejs) (`@supersemantics/vsl-core`),
[.NET](https://github.com/supersemantics/VSL-Core.NET) (`SuperSemantics.Vsl`).

| Framework | Package | Where the gate runs | What a denial does |
|---|---|---|---|
| Microsoft Agent Governance Toolkit | `vsl-agt` (this repo) | AGT's `pre_tool_call` interception point, or its `ToolCallInterceptor` chain | Agent Hooks raises `InterceptionBlocked`; AGT's chain returns `allowed=False` |
| [LangGraph](https://github.com/supersemantics/VSL-Langgraph) | `vsl-langgraph` | `gated_node`: before the node body | the node is skipped; `route_on_denial` routes the graph |
| [LangGraph.js](https://github.com/supersemantics/VSL-Langgraphjs) | `@supersemantics/vsl-langgraph` | `gatedNode`: before the node body | the node is skipped; `routeOnDenial` routes the graph |
| [LangChain](https://github.com/supersemantics/VSL-LangChain) | `vsl-langchain` | `VSLToolMiddleware`, `create_agent`'s `wrap_tool_call` | the tool never runs; the model gets an error `ToolMessage` |
| [Microsoft Agent Framework (Python)](https://github.com/supersemantics/VSL-Microsoft-agent-framework) | `vsl-maf` | function middleware, before the tool | the run aborts with `VSLGateDenied` |
| [Microsoft Agent Framework (.NET)](https://github.com/supersemantics/VSL-Microsoft-agent-framework.NET) | `SuperSemantics.Vsl.AgentFramework` | `UseVslGate` / `UseVslGates`, before the tool | the run aborts with `VslGateDeniedException` |
| [ROS 2](https://github.com/supersemantics/VSL-ROS2) | `vsl-ros2` | before a command is published, a service handled or an action goal accepted | the message isn't sent, or the goal is rejected; `vsl_guard` proxies Nav2 |

## What VSL adds to AGT

Each row is a limit AGT documents about itself, and what vsl-agt does about it.

| AGT, in its own docs | vsl-agt |
|---|---|
| The audit trail "records what the agent attempted", not whether the action ran ([LIMITATIONS](https://github.com/microsoft/agent-governance-toolkit/blob/main/docs/LIMITATIONS.md) §2) | `post_tool_call` records that the tool ran (`write_action`); `ledger.enforcement(decision_id)` computes, from record order, whether every gate decided first |
| The decision fields are outside the audit entry hash in v1.0 ([AUDIT-COMPLIANCE-1.0](https://github.com/microsoft/agent-governance-toolkit/blob/main/docs/specs/AUDIT-COMPLIANCE-1.0.md) §4.3.1) | every VSL gate decision is a hash-chained ledger entry, decision included |
| No correlation across a sequence of allowed actions (LIMITATIONS §1) | a gate can be any Invariant, including a vsl-core `Cluster` Invariant over state the agents share; hand-offs between agents are recorded with `write_handover` in the same ledger |
| A recorded kill is not proof of termination (LIMITATIONS §13) | an Invariant can name a `TerminalState`: every gated call is denied until a named human releases it, recorded as a `HUMAN_AUTHORISED_TRANSITION` |
| Policy verdicts are allow, deny or transform | a `PreNode` gates on a measured ratio (Gamma) against a threshold, recorded with its value |

## Install

```bash
pip install vsl-agt            # with Agent Hooks
pip install "vsl-agt[agt]"     # also AGT core, for VSLToolCallInterceptor
```

vsl-agt runs on Python 3.10 or later. AGT core 5.0, and so `VSLToolCallInterceptor`, needs 3.11 or
later.

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
  (`vsl:terminal_state:<name>`) until `vsl.release(authority=..., authorised_by=..., evidence=...)`
  records a `HUMAN_AUTHORISED_TRANSITION`. The state is read back from the ledger when a
  `VSLInterceptor` is created, so with a `JsonlLedgerStore` it survives a restart: the agent comes
  back still held, and only a recorded release frees it. Another agent's Terminal State (a different
  `identity_key`) does not hold this one;
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
issue_refund = vsl_native.track(issue_refund)   # records each run after it returns or raises
chain = CompositeInterceptor([PolicyInterceptor(GovernancePolicy(allowed_tools=["issue_refund"])), vsl_native])

request = ToolCallRequest(tool_name="issue_refund", arguments={"amount": 120.0}, call_id=call_id, agent_id="refund-agent")
if chain.intercept(request).allowed:
    issue_refund(amount=120.0)

ledger.enforcement(call_id)                    # Enforcement.BEFORE_ACTION
```

AGT's protocol is synchronous and runs before the tool only:

- the VSL gates run on a private event loop thread; a gate that doesn't decide within `timeout_sec`
  (default 5 s) is a deny, `vsl:gate_timeout`;
- there is no after-tool hook, so `track(fn)` wraps each gated tool (sync or async). An allowed
  intercept leaves its decision id pending; the tracked tool's next run records under it, with
  `is_error` if it raised. A run with no allowed intercept before it (the gates were never asked, or
  they denied and the caller ran it anyway) is recorded under a new `unmediated/...` decision id,
  which `enforcement()` reads as `AFTER_ACTION`. Give the tracked function to the framework in
  place of the original: for AGT's CrewAI adapter, track the tool's function before the adapter
  wraps it;
- the pending id belongs to the thread or asyncio task: intercept and run the tool in the same one,
  as `CompositeInterceptor` callers and the CrewAI adapter do;
- without `track`, call `record_action(request)` after the tool runs; without either, `enforcement()`
  reads `NOT_OBSERVED` for an allowed call. Use one or the other for a tool, not both;
- the result's `audit_entry` carries `vsl_decision_id` and the ledger `vsl_evidence`.

`agent_os` is imported only when a call is intercepted. In AGT 5.0 importing it warns
(`agent-os-kernel is deprecated`), even from `agent-governance-toolkit-core`.

## Other orchestration frameworks

There are three routes, depending on what the framework offers.

**1. A VSL adapter exists for it.** Use it (table above). If the agent also runs under AGT, put
both side by side. In Microsoft Agent Framework, for example, AGT's `GovernancePolicyMiddleware`
and vsl-maf's `VSLFunctionMiddleware` go in the same middleware list.

**2. Through AGT.** Which vsl-agt class you use depends on how AGT reaches the framework:

| How AGT reaches the framework | Use |
|---|---|
| A host that emits Agent Hooks (`agent-hooks-sdk`, or your own loop) | `VSLInterceptor`, registered at `vsl.points` |
| An AGT adapter that calls `ToolCallInterceptor`s, or a `CompositeInterceptor` in your own loop | `VSLToolCallInterceptor`, with each gated tool wrapped by `track()` |
| An AGT adapter with no third-party seam | route 1, or put the framework's tool calls through a `CompositeInterceptor` yourself |

In `agent-governance-toolkit-core` 5.0.0, the CrewAI adapter is the one that calls
`ToolCallInterceptor`s. The other Python adapters run AGT's policy through their own pre- and
post-execute checks, which take no third-party interceptor. That is AGT's code today, not a VSL
limit; as more of AGT's adapters move onto Agent Hooks, `VSLInterceptor` reaches them unchanged.

**3. No adapter at all.** Compile a gate with `AGTAdapter()` (or vsl-core's
`PlainPythonReferenceAdapter`) and call it immediately before the action. It raises on a denial.
Record the decision and the action yourself:

```python
gate = AGTAdapter().compile_invariant(refund_cap)
await gate(args)                                   # raises InvariantViolation on a denial
ledger.write_gate_decision(identity_key="refund-agent", primitive_name="refund-cap", allowed=True, decision_id=d)
issue_refund(**args)
ledger.write_action(identity_key="refund-agent", action_name="issue_refund", decision_id=d)
```

## What a gate reports

Gates take no assurance claim: a `PreNode` or `Invariant` is built from its name, description and
monitor or rule. Whether a gate held before its action is computed from the ledger (see
[vsl-core's "What a gate reports"](https://github.com/supersemantics/VSL-Core#what-a-gate-reports)).

## Status

Alpha. Tested against the real packages, not mocks:

- `agent-hooks-sdk` 0.1.0b3: allowed and denied calls through `InterceptionEmitter`, the Terminal
  State, its release, and both surviving a restart on a `JsonlLedgerStore`, a failing gate, ledger entries matched to the host's records, AGT-style
  interceptors before VSL in the chain, and a host that runs a denied call anyway (the ledger reads
  `AFTER_ACTION`).
- `agent-governance-toolkit-core` 5.0.0: `VSLToolCallInterceptor` in a `CompositeInterceptor` with
  AGT's `PolicyInterceptor`, requests without a call id (CrewAI's shape), the timeout, and tracked
  tools: sync and async, errors, one run per intercept, and runs with no intercept or after a deny
  (`AFTER_ACTION`).
- `run_conformance_suite(AGTAdapter())` is empty.

Known gaps: a tracked tool run in a different thread or task from its intercept is recorded as
unmediated; Agent Hooks is a 0.1 beta and AGT is in preview, so both APIs may change.

## License

MIT. Copyright Super Semantics.
