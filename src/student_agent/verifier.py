"""Local invariants; the server remains authoritative for audit ownership/scoring."""

from __future__ import annotations

from typing import Any

from .analysis import ZERO, money
from .evidence import CaseEvidence

ENTITY_FIELDS = {
    "order_ids": {"order_id"},
    "item_ids": {"order_item_id", "item_id"},
    "seller_ids": {"seller_id"},
    "payment_references": {"payment_reference", "payment_sequential"},
    "shipment_ids": {"shipment_id"},
}


def evidence_entities(value: Any) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {name: set() for name in ENTITY_FIELDS}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for field, keys in ENTITY_FIELDS.items():
                for key in keys:
                    if node.get(key) is not None:
                        found[field].add(str(node[key]))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def verify_output(output: dict[str, Any], ledger: CaseEvidence) -> None:
    ledger.trace.contracts.validate_output(output, "verifier output")
    if output["case_id"] != ledger.case_id:
        raise ValueError("Verifier: case ID mismatch")
    used = set(output["evidence_refs"])
    owned = {record["evidence_ref"] for record in ledger.records.values()}
    if not used or not used <= owned:
        raise ValueError("Verifier: missing or unowned evidence reference")
    found = {key: set() for key in ENTITY_FIELDS}
    for tool, evidence in ledger.records.items():
        if tool == "get_policy" or evidence["evidence_ref"] not in used:
            continue
        for field, identifiers in evidence_entities(evidence["data"]).items():
            found[field].update(identifiers)
    entities = output["affected_entities"]
    for field, identifiers in entities.items():
        if not set(identifiers) <= found[field]:
            raise ValueError(f"Verifier: unsupported entity in {field}")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]) <= used:
            raise ValueError("Verifier: claim cites unused evidence")
        if claim["verdict"] != "insufficient_evidence" and not claim["evidence_refs"]:
            raise ValueError("Verifier: claim verdict lacks evidence")
    financial = output["financial_resolution"]
    amount = money(financial["recommended_refund_brl"])
    lines = [money(line["amount_brl"]) for line in financial["refund_lines"]]
    if amount is None or any(line is None for line in lines) or sum(lines, ZERO) != amount:
        raise ValueError("Verifier: refund total is not the sum of refund lines")
    allowed = set().union(*(set(values) for values in entities.values()))
    for line in financial["refund_lines"]:
        if line["entity_id"] is not None and line["entity_id"] not in allowed:
            raise ValueError("Verifier: refund line is outside affected entities")
    status = output["assessment"]["case_status"]
    actions = output["resolution_actions"]
    if status == "no_action" and amount != ZERO:
        raise ValueError("Verifier: no_action cannot refund money")
    if status == "action_required" and (not actions or actions == ["document_no_action"]):
        raise ValueError("Verifier: actionable case lacks an action")
    if output["assessment"]["primary_issue"] == "insufficient_evidence" and (
        status != "needs_investigation" or amount != ZERO
    ):
        raise ValueError("Verifier: insufficient evidence cannot authorize a refund")
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if (
            party["party_type"] == "seller"
            and party["party_id"] is not None
            and party["party_id"] not in entities["seller_ids"]
        ):
            raise ValueError("Verifier: seller responsibility outside entity scope")
    ranks = [cause["rank"] for cause in output["root_cause_analysis"]["ranked_causes"]]
    if ranks != list(range(1, len(ranks) + 1)):
        raise ValueError("Verifier: causes must have consecutive ranks")
    for conflict in output["data_conflicts"]:
        if (
            conflict["selected_source"] is not None
            and conflict["selected_source"] not in conflict["sources"]
        ):
            raise ValueError("Verifier: conflict selects an unknown source")
