# L3A Architecture Record

## 1. System overview

```text
inputs/<case_id>.json
  -> coordinator
  -> order-item-agent -> MCP order/item/seller evidence
  -> payment-agent    -> MCP payment/refund evidence
  -> shipment-agent   -> MCP shipment evidence
  -> policy-agent     -> MCP policy evidence and resolution decision
  -> verifier         -> contract-shaped output and observable trace
```

The implementation lives in `src/student_agent/workflow.py`. It never treats the
customer message as ground truth. It uses the claimed order id only as a lookup key,
collects MCP evidence, derives entities and monetary totals from returned data, and
returns only MCP-provided `evidence_ref` values.

## 2. Agent ownership

| Actor | Input | Responsibility | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case JSON, discovered tools | Assign bounded specialist tasks and preserve case scope | `task_assigned` events |
| Order/item | `case_id`, `order_id` | Fetch order, item and seller evidence; extract item/seller ids | Handoff to payment |
| Payment | `case_id`, `order_id` | Fetch payment/refund evidence; reconcile paid/refunded totals | Handoff to shipment |
| Shipment | `case_id`, `order_id` | Fetch shipment evidence; expose delivery timeline signals | Handoff to policy |
| Policy | `policy_version`, specialist evidence | Fetch policy and select issue, responsibility and actions | `policy_decided` event |
| Verifier | Draft output | Check schema-facing invariants before finalize | `verification_completed` event |

Tool names are selected from MCP discovery. The workflow prefers public starter
names such as `get_order`, then falls back to equivalent discovered names with
domain-specific prefixes.

## 3. A2A protocol

Messages are implicit, typed handoffs recorded in trace events. Each handoff is
correlated by `case_id`, includes the target actor, and carries only observable
decision codes plus evidence refs. The workflow is acyclic:

```text
coordinator -> order-item -> payment -> shipment -> policy -> verifier
```

Each specialist calls a small set of domain tools and returns once; failed candidate
tool signatures are tried with bounded alternatives. There is no unbounded retry loop.

## 4. Evidence lifecycle

MCP responses are validated by `EvidenceGateway` against
`mcp-evidence-response-v1.schema.json`. The workflow stores the `evidence_ref`,
domain and data in an in-memory per-case evidence list. Every successful MCP call
emits `tool_result_consumed` with the real `evidence_ref`. Output evidence refs are
deduplicated, capped to the public contract limit, and never reused across cases.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | CLI/server failure handles the case run | No fabricated output | None for missing evidence |
| Tool signature mismatch | Yes, bounded argument aliases | Try next alias/candidate | Successful calls only are consumed |
| Tool not discovered | No | Continue with available domains and lower confidence | Specialist handoff still occurs |
| Source conflict | No automatic override | Mark conservative issue/confidence where possible | `policy_decided` |
| Invalid specialist result | No | Verifier keeps schema-safe defaults | `verification_completed` |

Missing evidence is not converted into invented evidence refs or fake facts.

## 6. Verification invariants

The verifier checks that the output shape matches the public schema expectations:
case id is unchanged, evidence refs are deduplicated, entity ids stay within contract
limits, refund total equals the single refund line when present, confidence is bounded,
actions match the selected status, and responsible party type is consistent with the
primary issue.

## 7. Reproducibility

Runtime: Python 3.11+ using the package dependencies declared in `pyproject.toml`.
The workflow is deterministic and uses no random seed, model call, hidden prompt or
external state besides MCP evidence. Recommended commands:

```bash
source .venv/bin/activate
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Do not include `.env`, API keys, input payloads or debug logs in the submission ZIP.
