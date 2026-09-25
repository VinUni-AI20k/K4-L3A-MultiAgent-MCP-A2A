from __future__ import annotations

from typing import Any


class Verifier:
    """Enforces cross-field consistency, invariants, and confidence bounds."""

    @classmethod
    def verify_and_clean(cls, output: dict[str, Any]) -> dict[str, Any]:
        """Validate and sanitize output against Day09 L3A consistency invariants."""
        # 1. Clean and deduplicate resolution_actions
        actions = output.get("resolution_actions", [])
        seen_actions: set[str] = set()
        clean_actions: list[str] = []
        for a in actions:
            if a and a not in seen_actions:
                clean_actions.append(str(a))
                seen_actions.add(str(a))
        output["resolution_actions"] = clean_actions[:8]

        # 2. Clean and deduplicate evidence_refs
        refs = output.get("evidence_refs", [])
        seen_refs: set[str] = set()
        clean_refs: list[str] = []
        for r in refs:
            if r and r not in seen_refs:
                clean_refs.append(str(r))
                seen_refs.add(str(r))
        output["evidence_refs"] = clean_refs[:30]

        # 3. Clean and deduplicate affected_entities
        entities = output.get("affected_entities", {})
        for key in ("order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids"):
            vals = entities.get(key, [])
            seen_vals: set[str] = set()
            clean_vals: list[str] = []
            for v in vals:
                if v and str(v) not in seen_vals:
                    clean_vals.append(str(v))
                    seen_vals.add(str(v))
            entities[key] = clean_vals[:20]
        output["affected_entities"] = entities

        # 4. Enforce consistency between status, refund, and actions
        assessment = output.get("assessment", {})
        fin = output.get("financial_resolution", {})
        case_status = assessment.get("case_status")
        refund_amount = fin.get("recommended_refund_brl", 0.0)

        if case_status == "no_action":
            fin["recommended_refund_brl"] = 0.0
            fin["refund_lines"] = []
        else:
            lines = fin.get("refund_lines", [])
            if refund_amount > 0 and not lines:
                order_id = (
                    entities["order_ids"][0] if entities.get("order_ids") else output["case_id"]
                )
                fin["refund_lines"] = [
                    {
                        "reason_code": "ORDER_RESOLUTION_REFUND",
                        "amount_brl": round(refund_amount, 2),
                        "entity_id": order_id,
                    }
                ]
            elif refund_amount == 0.0:
                fin["refund_lines"] = []

        # 5. Ensure seller party_id is not null if party_type is seller
        root_cause = output.get("root_cause_analysis", {})
        parties = root_cause.get("responsible_parties", [])
        for party in parties:
            if party.get("party_type") == "seller" and not party.get("party_id"):
                seller_ids = entities.get("seller_ids", [])
                party["party_id"] = seller_ids[0] if seller_ids else "seller-default"

        # 6. Clamp confidence
        conf = assessment.get("confidence", 0.95)
        assessment["confidence"] = round(max(0.0, min(1.0, float(conf))), 2)

        # 7. Ensure claim assessments evidence_refs are subset of valid refs
        claims = output.get("claim_assessments", [])
        for c in claims:
            c_refs = c.get("evidence_refs", [])
            c["evidence_refs"] = [r for r in c_refs if r in seen_refs][:10]

        return output
