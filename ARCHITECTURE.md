# L3A Architecture Record

## 1. System overview

The implementation is a deterministic Python `async` state machine. `Coordinator`
owns a case from receipt to finalization. It discovers the MCP tool inventory once
per case, assigns bounded specialist tasks, then hands evidence to Policy and
Verifier. The output and every trace event are checked against the public JSON
schemas by the CLI before writing artifacts.

```text
case input -> Coordinator -> Order/item ─┐
                         -> Payment    ─┼-> Policy -> Verifier -> output
                         -> Shipment   ─┘                  |
                                  MCP Evidence Gateway     +-> trace.jsonl
```

`case_id` is the correlation identifier on every MCP call and A2A handoff. Evidence
is scoped to one case and is never cached or reused by another case.

## 2. Agent ownership and permissions

| Actor | Responsibility | MCP permission | Handoff |
| --- | --- | --- | --- |
| Coordinator | validate case ID, discover tools, schedule bounded work | none | specialists, then Policy |
| Order/item agent | retrieve the claimed order and its items | `get_order`, `get_order_items` only when discovered | Policy |
| Payment agent | retrieve payment and refund histories for that order | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` only when discovered | Policy |
| Shipment agent | retrieve the order shipment summary | `get_shipment_summary` only when discovered | Policy |
| Policy agent | retrieve the requested policy version and apply evidence-backed rules | `get_policy` only when discovered | Verifier |
| Verifier agent | enforce output/evidence consistency before finalization | no MCP tools | Coordinator |

The gateway validates every MCP envelope using
`mcp-evidence-response-v1.schema.json` before a specialist may consume it. A
specialist cannot call a tool absent from discovery, and it cannot infer an ID from
free text: only explicit, named case fields are eligible.

## 3. A2A protocol and observability

An in-memory handoff consists of `{case_id, actor, target, decision_code}` plus
validated evidence envelopes held only in the per-case state. It is intentionally
not serialized as a private reasoning trace. Observable lifecycle events are:

```text
case_received -> task_assigned -> tool_result_consumed? -> handoff
              -> policy_decided -> verification_completed -> case_finalized
```

The CLI emits `case_received` and `case_finalized`; `solve_case` emits the middle
events. `tool_result_consumed` carries only the MCP-issued `evidence_ref` and tool
name. Event correlation and format are enforced by `trace-event-v1.schema.json`.
Each specialist has one handoff path to Policy, so there is no agent-to-agent loop.

## 4. Evidence lifecycle

1. Coordinator performs MCP discovery.
2. A specialist constructs a request only from an explicitly named identifier and a
   discovered, permitted tool.
3. `EvidenceGateway.call` validates the returned envelope and preserves its original
   `evidence_ref`; it never generates a reference locally.
4. The specialist stores the envelope in the current case state and emits
   `tool_result_consumed`.
5. Policy may create a claim only when its supporting evidence is relevant and the
   same references are copied to the output. Verifier rejects mismatched scope,
   unsupported claims, fabricated references, and schema deviations.

Until domain extraction and claim-linkage rules are implemented, the workflow emits
a schema-valid `insufficient_evidence` abstention with no evidence references. This
is intentional: it avoids presenting collected but unanalysed evidence as support.

## 5. Failure policy

| Failure | Retry | Fallback | Trace decision |
| --- | --- | --- | --- |
| MCP timeout/network/tool error | two total attempts, 200 ms linear backoff | count failed request; do not invent evidence | `INSUFFICIENT_EVIDENCE` |
| Tool not discovered | no retry | skip request | `SPECIALIST_EVIDENCE_READY` |
| MCP envelope/schema failure | no retry beyond request limit | discard response | `INSUFFICIENT_EVIDENCE` |
| Invalid case ID | no MCP call | fail the run | exception |
| Invalid specialist conclusion | no retry | verifier returns safe abstention | `SCHEMA_SAFE_ABSTENTION` |

All MCP operations are read-only and idempotent for a fixed `case_id`; retries are
therefore safe. Missing evidence is never converted into an assumed order, payment,
shipment, refund, or responsible party.

## 6. Verification invariants

Before writing an output, the system requires: the L3A output schema version and
case ID match; all entities are case-scoped; every submitted `evidence_ref` came
from MCP for this case; each claim has relevant linked evidence; refund totals and
lines are non-negative BRL values; actions do not contradict status/responsibility;
and confidence remains in `[0, 1]`. The CLI performs the public-schema check again
on every output and trace line.

## 7. Reproducibility

There is no model invocation, random decision, or background concurrency in this
phase. The workflow runs specialists in a fixed order and uses at most two attempts
per eligible MCP request. Dependencies are bounded in `pyproject.toml`; run with
`python -m pip install -e ".[dev]"`, then `day09 run`, `day09 validate`, and
`day09 package`. Secrets remain only in `.env` and are excluded from artifacts.
