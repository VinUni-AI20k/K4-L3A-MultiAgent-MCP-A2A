# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

```text
Input → Coordinator → Specialists (order/payment/shipment) → Policy agent → Verifier → Output
                         │                                        │             │
                         └──────────────── MCP Evidence Gateway ──┴─── Trace ────┘
```

`solve_case()` (`src/student_agent/workflow.py`) implements the coordinator. It fetches
evidence through specialist calls, derives the `primary_issue` from that evidence with a
deterministic classifier (`_classify`), asks `get_policy` for the authoritative
`case_status` / `recommended_action` for that issue, then assembles and hands off the
output for verification before returning it.

The customer's own claimed topic (`customer_request.claims[].topic`) is never used as an
input to the classifier — it is only compared against the derived `primary_issue`
afterwards to score each `claim_assessments[].verdict`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | `case`, `claimed_order_id` | Dispatches specialists in order, aggregates evidence, builds the final output object | Hands off to verifier |
| Order-agent | `case_id`, `order_id` | `get_order`, `get_order_items`; dedupes repeated `order_item_id` records | Order status/timestamps, item price/freight/seller |
| Payment-agent | `case_id`, `order_id` | `get_payment_timeline`, `get_refund_timeline` (best-effort) | Captured/mismatch events, refund lifecycle |
| Shipment-agent | `case_id`, `order_id` | `get_shipment_summary` | Delivery timestamps, `delivered_late` events |
| Policy-agent | `case_id`, `policy_version`, derived `primary_issue` | `get_policy`; looks up the authoritative `case_status` / `recommended_action` / `party_type` for that issue | Resolution template for the verifier |
| Verifier | Assembled output | Confirms evidence refs came from successful calls only, emits `verification_completed` | Final output returned to `cli.py` |

No agent calls `get_sellers`, `get_product_context`, or `get_customer_history` — seller id
is already present on each item row, and the other two domains are not required by any of
the 10 `primary_issue` categories, so pulling them would only add unused (and
potentially forbidden-domain-penalized) evidence.

## 3. A2A protocol

Every message is implicit function-call handoff within one `asyncio` task, correlated by
`case_id` (passed to every MCP call and every trace event). There is no cross-case or
cross-agent shared state — each `solve_case()` call is independent, so there is no
possibility of a coordination loop. Handoff points are traced explicitly:

- `task_assigned` (coordinator → order/payment/shipment/policy-agent) before each
  specialist's calls.
- `tool_result_consumed` immediately after each successful MCP call, citing the
  `evidence_ref` that call returned.
- `policy_decided` once the classifier has produced a `primary_issue`.
- `handoff` (coordinator → verifier) once the output object is assembled.
- `verification_completed` after the verifier's checks.

Timeouts are delegated to `httpx2`'s client timeout (300s) configured in
`mcp_gateway.connect_gateway`; there is no agent-level retry loop, so no retry-induced
cycle is possible.

## 4. Evidence lifecycle

`EvidenceGateway.call()` validates every MCP response against
`mcp-evidence-response-v1.schema.json` before returning it, so a malformed envelope never
reaches the workflow. `workflow._fetch()` wraps each call, catches only `RuntimeError`
(a tool-level failure such as "not found"), and returns `None` instead of inventing data —
`get_refund_timeline` routinely returns "not found" for orders with no refund history, and
that is treated as "no refund lifecycle exists" rather than an error.

Every evidence ref actually consumed is collected into `output.evidence_refs`; the same
list is reused for every `claim_assessments[].evidence_refs`, since all fetched domains
were used to reach the one classification decision for the case. No evidence_ref is ever
constructed by hand — every ref in the output was returned by a real `gateway.call()`.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / tool error | No (single attempt) | Treated as "evidence absent"; `get_order`/`get_order_items` failing aborts the case as `insufficient_evidence`, other domains are optional and degrade the classifier's inputs | `verification_completed` with `decision_code=insufficient_evidence` |
| Not found (e.g. no refund history) | No | Treated as "lifecycle event never occurred" (`refund_timeline=None`), not as missing/invalid data | n/a (no event emitted for a domain never fetched) |
| Source conflict (order-level facts vs. a shipment/payment event) | No | The order-level, purchase-timestamp-anchored fact wins; the disagreeing event is recorded, never silently dropped | `data_conflicts[]` entry with `resolution_code` |
| Invalid specialist result (schema violation) | No | `Contracts.validate_evidence` raises `ContractError` before the workflow sees the data — this is a real bug, not a case outcome, so it is not caught | n/a — propagates and fails the run |

No retry loop exists because every MCP call is a single, idempotent read; a second
identical call would return the same evidence (or the same "not found"), so retrying
cannot change the outcome and is not attempted. Missing evidence always results in a lower
`confidence` and/or `primary_issue = insufficient_evidence`, never a fabricated value.

## 6. Verification invariants

`_verify()` runs on the assembled output before `solve_case()` returns (and
`contracts.validate_output` re-checks schema in `cli.py`). It returns a list of problem
codes; a non-empty list downgrades the whole output to `primary_issue =
insufficient_evidence`, `case_status = needs_investigation`, `confidence = 0.3`
(evidence_refs/root_cause/financial_resolution/resolution_actions/claim_assessments are
rebuilt to match), and `verification_completed` is traced with
`decision_code = "failed"` and `attributes.problems = <count>` (`"passed"` / `0` when
clean). Checks:

- **Schema**: `contracts.validate_output()` validates the full object against
  `l3a-output-v2.schema.json` before it is written to `outputs/<case_id>.json`.
- **Entity scope**: `affected_entities` is built only from the fetched order/items for
  this `case_id`/`order_id` — never from another case's evidence.
- **Evidence ownership**: `_verify` checks every `evidence_refs` entry (and every
  `claim_assessments[].evidence_refs` entry) is a subset of the refs actually returned
  by a `gateway.call()` made for this case_id in this run; none are copied from a prior
  case or invented. `evidence_refs` itself is further narrowed to only the tools listed
  for the case's `primary_issue` in `ISSUE_TOOLS` (`workflow.py`), instead of citing
  every fetched domain, for evidence precision.
- **Claim linkage**: every `claim_assessments[].verdict` is derived by comparing the
  claim's topic against the independently-derived `primary_issue`, not by trusting the
  claim.
- **Money/action consistency**: `_verify` checks `case_status == "no_action"` implies
  `recommended_refund_brl == 0` and empty `refund_lines`, and `case_status ==
  "action_required"` implies `recommended_refund_brl > 0`; `case_status` and
  `recommended_action` both come from the same `get_policy` rule lookup, so they cannot
  disagree.
- **Responsible party**: `_verify` checks `party_type == "seller"` always carries a
  non-null `party_id`; `party_id` itself is set from the classifier's `seller_id` only
  when `get_policy`'s rule for this issue names `party_type == "seller"` (not from a
  per-branch heuristic), so it cannot disagree with the policy-authoritative party type.
- **Confidence bounds**: every branch of `_classify()` returns a fixed confidence in
  `[0.5, 0.95]` depending on signal strength (explicit event-driven signals like
  `reconciliation_mismatch`/`order_status`/refund-lifecycle status/exact duplicate rows
  score 0.95; late delivery scores 0.85 with a matching shipment event, 0.65 when
  actor-inferred; split/unsupported score 0.8; `insufficient_evidence` is fixed at 0.5,
  or 0.3 when `_verify` itself triggers the downgrade).

## 7. Reproducibility

- Model/config: this workflow is a deterministic rule engine — no LLM call is made
  inside `solve_case()`, so there is no model/temperature to pin.
- Dependencies: pinned via `pyproject.toml` (`httpx2`, `jsonschema[format]`, `mcp`,
  `python-dotenv`); exact versions are whatever `pip install -e ".[dev]"` resolved at
  install time, recorded in the environment's lock/freeze if one is taken.
- Concurrency: `cli.py _run()` processes cases sequentially (`for case_id in
  case_set.case_ids`), one MCP session shared across the whole run — no concurrency
  limit is needed because there is no parallelism.
- Random seed: none used — every decision is a pure function of the fetched evidence.
- Run command: `python -m student_agent.cli run`, followed by
  `python -m student_agent.cli validate` and
  `python -m student_agent.cli package --output dist/submission.zip`.
- Resource limits: bounded by the MCP Evidence Gateway's own per-call timeout (300s,
  configured in `mcp_gateway.connect_gateway`); no local resource limits are configured.
