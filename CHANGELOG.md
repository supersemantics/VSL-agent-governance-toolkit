# Changelog

What changed in each release, and why. Newest first.

## 0.1.0 (unreleased): first version

**Before:** VSL gates could run in Microsoft's Agent Governance Toolkit only by hand, outside its
interception chain, and nothing tied AGT's audit trail to the VSL ledger.

**After:**
- `VSLInterceptor`: an Agent Hooks (`agent-hooks/0.1`) interceptor. At `pre_tool_call` it runs the
  tool's PreNodes and Invariants, records each decision, and allows or denies; at `post_tool_call`
  it records that the tool ran. Denials are closed in the ledger, an Invariant can enter a Terminal
  State released only by a named human, and every entry names the host record it belongs to.
- `VSLToolCallInterceptor`: AGT's own `ToolCallInterceptor` protocol, for `CompositeInterceptor`
  chains and the adapters that call interceptors.
- `AGTAdapter`: vsl-core's `VSLAdapter`, conformance-suite clean.

Requires `super-semantics-vsl>=0.4,<0.5` and `agent-hooks-sdk==0.1.0b3`; the `agt` extra adds
`agent-governance-toolkit-core>=5,<6`.

**Why:** AGT decides whether a call may run; VSL adds gates on measured thresholds, hard rules with a
human-released Terminal State, and a hash-chained record of whether each gate decided before its
action ran.

**Tests:** 24 pass against agent-hooks-sdk 0.1.0b3 and agent-governance-toolkit-core 5.0.0 (17,
and the AGT-native tests skipped, without AGT installed).
