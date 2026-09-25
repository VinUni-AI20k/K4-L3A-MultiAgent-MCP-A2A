from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..mcp_gateway import EvidenceGateway, call_with_retry
from ..trace import TraceWriter

_ORDER_ID_KEYS = frozenset({"claimed_order_id", "order_id", "order_ids"})
_PAYMENT_REFERENCE_KEYS = frozenset(
    {
        "payment_id",
        "payment_ids",
        "payment_ref",
        "payment_reference",
        "payment_references",
        "transaction_id",
        "transaction_ids",
        "transaction_ref",
        "transaction_reference",
        "transaction_references",
    }
)
_STATUS_KEYS = frozenset({"event", "event_type", "payment_status", "state", "status"})
_PAYMENT_METHOD_KEYS = frozenset(
    {"method", "payment_method", "payment_methods", "payment_type", "payment_types"}
)
_DUPLICATE_KEYS = frozenset(
    {"duplicate", "duplicate_charge", "duplicate_of", "is_duplicate"}
)
_POLICY_VERSION_KEYS = frozenset({"policy_version", "policy_versions"})
_CLAIMS_KEYS = frozenset({"claims"})
_CLAIM_KIND_KEYS = ("claim_type", "issue", "reason_code", "topic", "type")
_PAYMENT_CLAIM_KINDS = frozenset(
    {
        "duplicate_charge",
        "payment_mismatch",
        "refund_failed",
        "refund_pending",
        "valid_split_payment",
    }
)


def _iter_keyed_values(value: Any, wanted_keys: frozenset[str]) -> Iterable[Any]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in wanted_keys:
                yield child
            yield from _iter_keyed_values(child, wanted_keys)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_keyed_values(child, wanted_keys)


def _flatten_strings(values: Iterable[Any]) -> list[str]:
    flattened: list[str] = []
    for value in values:
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                flattened.append(candidate.strip())
    return list(dict.fromkeys(flattened))


def _extract_strings(value: Any, keys: frozenset[str]) -> list[str]:
    return _flatten_strings(_iter_keyed_values(value, keys))


def _contains_explicit_duplicate(value: Any) -> bool:
    for candidate in _iter_keyed_values(value, _DUPLICATE_KEYS):
        if candidate is True:
            return True
        if isinstance(candidate, str) and candidate.strip().lower() not in {
            "",
            "false",
            "no",
            "none",
            "null",
        }:
            return True
    return any(
        "duplicate" in status.lower() for status in _extract_strings(value, _STATUS_KEYS)
    )


def _record_count(data: Any, collection_keys: tuple[str, ...]) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, Mapping):
        for key in collection_keys:
            records = data.get(key)
            if isinstance(records, list):
                return len(records)
        return int(bool(data))
    return 0


def _normalize_token(value: str) -> str:
    return "_".join(value.strip().lower().replace("-", " ").split())


def _extract_claims(context: dict[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for value in _iter_keyed_values(context, _CLAIMS_KEYS):
        if not isinstance(value, list):
            continue
        for claim in value:
            if not isinstance(claim, Mapping):
                continue
            claim_id = claim.get("claim_id")
            if not isinstance(claim_id, str) or not claim_id or claim_id in seen_ids:
                continue
            claims.append(dict(claim))
            seen_ids.add(claim_id)
    return claims


def _claim_kind(claim: Mapping[str, Any]) -> str | None:
    for key in _CLAIM_KIND_KEYS:
        value = claim.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_token(value)
    return None


def _payment_claim_assessments(
    claims: list[dict[str, Any]],
    issue_signals: dict[str, bool],
    payment_evidence_refs: list[str],
    refund_evidence_refs: list[str],
) -> list[dict[str, Any]]:
    assessments: list[dict[str, Any]] = []
    for claim in claims:
        kind = _claim_kind(claim)
        if kind not in _PAYMENT_CLAIM_KINDS:
            continue
        supported = issue_signals.get(kind, False)
        relevant_refs = (
            refund_evidence_refs
            if kind in {"refund_failed", "refund_pending"}
            else payment_evidence_refs
        )
        assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": "supported" if supported else "insufficient_evidence",
                "confidence": 0.9 if supported else 0.7,
                "evidence_refs": relevant_refs,
            }
        )
    return assessments


class PaymentPolicyAgent:
    """Collect payment, refund and policy evidence for one scoped case."""

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def run(self, case_id: str, context: dict[str, Any]) -> dict[str, Any]:
        order_ids = _extract_strings(context, _ORDER_ID_KEYS)
        order_id = order_ids[0] if order_ids else None
        policy_versions = _extract_strings(context, _POLICY_VERSION_KEYS)
        if len(policy_versions) > 1:
            raise ValueError(f"case contains multiple policy versions: {policy_versions}")

        requests: list[tuple[str, dict[str, str], str]] = []
        if order_id is not None:
            requests.extend(
                (
                    ("get_order_payments", {"order_id": order_id}, "payment"),
                    ("get_payment_timeline", {"order_id": order_id}, "payment"),
                    ("get_refund_timeline", {"order_id": order_id}, "refund"),
                )
            )
        if policy_versions:
            requests.append(
                ("get_policy", {"policy_version": policy_versions[0]}, "policy")
            )

        evidence: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for tool_name, arguments, expected_domain in requests:
            try:
                result = await call_with_retry(
                    self.gateway,
                    tool_name,
                    case_id=case_id,
                    **arguments,
                )
                if result["domain"] != expected_domain:
                    raise ValueError(
                        f"{tool_name} returned domain {result['domain']!r}; "
                        f"expected {expected_domain!r}"
                    )
            except Exception as error:
                errors.append(f"{tool_name}: {type(error).__name__}")
                continue

            evidence[tool_name] = result
            actor = "policy_agent" if tool_name == "get_policy" else "payment_agent"
            attributes = {"domain": result["domain"]}
            if order_id is not None and tool_name != "get_policy":
                attributes["order_id"] = order_id
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[result["evidence_ref"]],
                attributes=attributes,
            )

        payment_payloads = [
            evidence[name]["data"]
            for name in ("get_order_payments", "get_payment_timeline")
            if name in evidence
        ]
        refund_payload = (
            evidence["get_refund_timeline"]["data"]
            if "get_refund_timeline" in evidence
            else []
        )
        refund_statuses = _extract_strings(refund_payload, _STATUS_KEYS)
        normalized_refund_statuses = {status.lower() for status in refund_statuses}
        issue_signals = {
            "duplicate_charge": _contains_explicit_duplicate(payment_payloads),
            "refund_pending": bool(
                normalized_refund_statuses
                & {"initiated", "pending", "processing", "requested"}
            ),
            "refund_failed": bool(
                normalized_refund_statuses
                & {"declined", "failed", "rejected", "reversed"}
            ),
        }
        payment_evidence_refs = [
            evidence[name]["evidence_ref"]
            for name in ("get_order_payments", "get_payment_timeline")
            if name in evidence
        ]
        refund_evidence_refs = (
            [evidence["get_refund_timeline"]["evidence_ref"]]
            if "get_refund_timeline" in evidence
            else []
        )
        policy_ref = (
            evidence["get_policy"]["evidence_ref"] if "get_policy" in evidence else None
        )
        self.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy_agent",
            decision_code=(
                "policy_evidence_collected" if policy_ref else "policy_evidence_unavailable"
            ),
            evidence_refs=[policy_ref] if policy_ref else None,
            attributes={"policy_available": policy_ref is not None},
        )

        all_payment_data = [
            evidence[name]["data"]
            for name in ("get_order_payments", "get_payment_timeline")
            if name in evidence
        ]
        evidence_refs = [item["evidence_ref"] for item in evidence.values()]
        return {
            "evidence": evidence,
            "data": {name: value["data"] for name, value in evidence.items()},
            "errors": errors,
            "evidence_refs": evidence_refs,
            "payment_data": {
                "order_id": order_id,
                "payment_record_count": _record_count(
                    evidence.get("get_order_payments", {}).get("data", []),
                    ("payments", "payment_rows", "records"),
                ),
                "payment_statuses": _extract_strings(all_payment_data, _STATUS_KEYS),
                "refund_statuses": refund_statuses,
                "payment_methods": _extract_strings(
                    all_payment_data, _PAYMENT_METHOD_KEYS
                ),
            },
            "payment_references": _extract_strings(
                all_payment_data, _PAYMENT_REFERENCE_KEYS
            ),
            "issue_signals": issue_signals,
            "claim_assessments": _payment_claim_assessments(
                _extract_claims(context),
                issue_signals,
                payment_evidence_refs,
                refund_evidence_refs,
            ),
            "policy": {
                "status": "evidence_collected" if policy_ref else "missing_policy_evidence",
                "evidence_ref": policy_ref,
                "data": (
                    evidence["get_policy"]["data"] if "get_policy" in evidence else None
                ),
            },
            "status": "completed" if order_id else "missing_order_id",
        }
