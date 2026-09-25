# L3A Architecture Record — Phase 2

## 1. System overview

The implementation is a pure-Python asynchronous state machine. Business facts and
money/date calculations are deterministic Python operations. The local model is an
advisory verifier only: it cannot call tools, create identifiers, alter evidence
references, or add fields to the public output.

```text
inputs/<case_id>.json
        │
        ▼
 Coordinator / Router ──task_assigned──┬── OrderAgent ─── get_order, get_order_items
                                      ├── PaymentAgent ─ get_payment_timeline,
                                      │                  get_refund_timeline
                                      ├── ShipmentAgent  get_shipment_summary
                                      └── PolicyAgent ── get_policy
                                              │
                    validated MCP envelopes + immutable evidence refs
                                              │
                                              ▼
                                      Aggregated CaseState
                                              │
                                   deterministic classification
                                   policy and financial resolution
                                              │
                                              ▼
                         VerifierAgent (invariants + optional Ollama review)
                                              │
                                    schema validation + handoff
                                              ▼
                         outputs/<case_id>.json + traces/trace.jsonl
```

`day09` emits `case_received` before `solve_case` and `case_finalized` after the
returned output passes schema validation. `workflow_started` is intentionally not
emitted because it is not an allowed `trace-event-v1` value. The state machine emits
only `task_assigned`, `tool_result_consumed`, `handoff`, `policy_decided`, and
`verification_completed` between those lifecycle events.

## 2. Agent ownership and tool permissions

| Actor | Input | Allowed MCP tools | Responsibility | Handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Public case envelope | None | Validate routing keys, assign bounded tasks, aggregate state | CaseState to Verifier |
| OrderAgent | `case_id`, claimed `order_id` | `get_order`, `get_order_items` | Establish authoritative status, timestamps, items and sellers | Order/item evidence |
| PaymentAgent | `case_id`, authoritative order scope | `get_payment_timeline`, `get_refund_timeline` | Identify captured payments, split/duplicate/mismatch and refund lifecycle | Payment/refund evidence |
| ShipmentAgent | `case_id`, authoritative order scope | `get_shipment_summary` | Compare delivery/carrier/estimate and identify responsible actor | Shipment evidence |
| PolicyAgent | `case_id`, `policy_version` | `get_policy` | Select the exact policy rule; compute dates and resolution deterministically | Policy decision |
| VerifierAgent | Aggregated state and draft | None | Check invariants, optionally request local-model review, validate schema | Approved public output |

Agents cannot dynamically expand their permissions. Tool discovery is performed by
the runner, while this workflow uses only the published, discovered L3A tools.

## 3. A2A protocol and state machine

All messages are in-process typed Python values. Their logical envelope is:

```text
case_id, sender, recipient, task_code, state_version, evidence_refs
```

`case_id` is copied from the input and supplied to every MCP call. A specialist may
only append an MCP response to the current `CaseState`; it cannot read another
case's state. Each specialist has one bounded execution per case. Optional transient
retries are bounded, use the same arguments, and do not create a routing loop.

State transitions are:

```text
RECEIVED → ASSIGNED → EVIDENCE_COLLECTED → POLICY_DECIDED
         → DRAFTED → VERIFIED → RETURNED
```

A required-tool failure produces `insufficient_evidence` or aborts verification; it
never causes a customer claim to be accepted as fact. The verifier does not expose
chain-of-thought. Trace attributes contain only compact status/decision metadata.

## 4. Evidence integrity and provenance

1. `EvidenceGateway.call` authenticates with the team API key and always injects the
   current `case_id`.
2. The gateway validates the complete MCP evidence envelope against
   `mcp-evidence-response-v1.schema.json` before returning it.
3. Specialists copy `evidence_ref` directly from that envelope into Python memory.
   The model never receives authority to generate or transform a ref.
4. Immediately after a successful call, the specialist emits
   `tool_result_consumed` with the same tool and exact ref.
5. Output refs are selected from the current case's in-memory evidence registry.
   They are deduplicated without changing their bytes and capped at schema limits.
6. Claim refs and top-level refs contain only evidence actually used for the
   conclusion. No ref is cached or reused across cases/runs.
7. Team/run/case ownership remains server-audited. Local checks enforce same-case
   trace linkage; they do not pretend to replace the audit service.

## 5. Error handling and retry

| Failure | Retry | Safe fallback | Observable behavior |
| --- | --- | --- | --- |
| Timeout/temporary transport error | Up to 2 attempts, short exponential backoff | Required source missing → insufficient evidence; optional refund absence → continue without refund claim | No invented evidence; only successful calls are consumed |
| HTTP/MCP rate limit | Up to 2 attempts with bounded backoff | Same as timeout | Same arguments and `case_id` on retry |
| Missing optional resource | No repeated probing after definitive MCP error | Empty optional domain; it cannot support a conclusion | No fake ref or empty tool-result event |
| Missing required order/policy | Bounded retry, then stop/insufficient evidence | No customer-derived replacement | Verifier rejects unsupported draft |
| Invalid MCP envelope | No retry unless transport was incomplete | Reject response | Gateway schema error propagates |
| Source conflict | No model tie-break | Prefer authoritative lifecycle fields and record a schema-valid conflict when material | Deterministic resolution code |
| Ollama unavailable/invalid JSON | One short request, no retry storm | Deterministic Python verifier | Verification still completes safely |

Retries are idempotent reads. A failed call never yields an evidence reference and is
therefore never included in output or trace evidence linkage.

## 6. Classification and consistency invariants

Classification is driven by authoritative signals in priority order: terminal order
status with captured payment; refund failed/pending; reconciliation mismatch;
delivery lateness and actor; split/duplicate payment; otherwise unsupported claim.
Events are bounded to the order lifecycle to ignore unrelated synthetic rows.

Before return, VerifierAgent checks:

- output `case_id` equals input `case_id`;
- every submitted ref exists in the current CaseState and is linked to its case;
- required evidence domains for the selected issue are present;
- entity IDs come only from scoped MCP data;
- `recommended_refund_brl` equals the sum of refund lines;
- zero-refund resolutions contain no positive refund line;
- status, action, responsibility and policy rule agree;
- confidence is within schema bounds (the model recommendation is clamped to
  `0.70..0.95`);
- the final dictionary validates against `l3a-output-v2.schema.json`.

## 7. Local model boundary

VerifierAgent calls the OpenAI-compatible Ollama endpoint
`http://localhost:11434/v1/chat/completions` with model
`qwen2.5:7b-instruct`. Environment variables `OLLAMA_BASE_URL` and
`OLLAMA_MODEL` may override these defaults, but deployments must keep the model under
10B parameters.

The prompt contains a compact redacted summary, not secrets or mutable provenance.
Expected model JSON is `{decision, rationale, confidence}`. `decision` and
`rationale` remain internal because the public output schema has no such fields.
The model cannot override failed deterministic invariants; unavailable Ollama falls
back to a rule-based approval and calibrated confidence.

## 8. Reproducibility

- Python 3.11+; dependency bounds are in `pyproject.toml`.
- Deterministic business rules; no random sampling and Ollama temperature is zero.
- Cases are processed sequentially by the supplied CLI, preventing shared-state
  contamination.
- MCP calls are bounded per case; no customer-history or product-context calls are
  required for the public L3A issue set.
- Commands: `day09 validate-inputs`, `day09 mcp-tools`, `day09 run`,
  `day09 validate`, and `day09 package --output dist/submission.zip`.
- API keys are read from `.env`, never written to output, trace, prompt, or package.
