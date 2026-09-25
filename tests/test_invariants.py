"""Validation / invariant tests — kiểm tra output JSON trước khi nộp.

Người sở hữu: Nguyên
Mục đích: Chạy trên output thật (outputs/*.json) để phát hiện lỗi TRƯỚC khi nộp.
Chạy: pytest tests/test_invariants.py -v

Các test này kiểm tra các invariant mà nếu vi phạm sẽ dẫn đến 0 điểm (hard gate)
hoặc mất điểm consistency/schema.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from student_agent.contracts import Contracts

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUTS_DIR = REPO_ROOT / "outputs"
INPUTS_DIR = REPO_ROOT / "inputs"
CONTRACTS_DIR = REPO_ROOT / "contracts" / "schemas"

EVIDENCE_REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")

VALID_PRIMARY_ISSUES = frozenset([
    "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
    "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
    "duplicate_charge", "refund_pending", "refund_failed",
    "unsupported_claim", "insufficient_evidence",
])

VALID_VERDICTS = frozenset([
    "supported", "unsupported", "partially_supported", "insufficient_evidence",
])

VALID_CASE_STATUSES = frozenset([
    "action_required", "no_action", "needs_investigation",
])


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _load_outputs() -> list[tuple[str, dict]]:
    """Load tất cả output JSON files. Trả về list (filename, data)."""
    outputs = []
    if not OUTPUTS_DIR.exists():
        return outputs
    for path in sorted(OUTPUTS_DIR.glob("*.json")):
        if path.name == ".gitkeep":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        outputs.append((path.name, data))
    return outputs


def _load_input(case_id: str) -> dict | None:
    """Load input JSON cho một case_id."""
    path = INPUTS_DIR / f"{case_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _get_outputs_or_skip() -> list[tuple[str, dict]]:
    """Load outputs, skip nếu chưa có output nào."""
    outputs = _load_outputs()
    if not outputs:
        pytest.skip("Chưa có output files trong outputs/ — chạy 'day09 run' trước")
    return outputs


# --------------------------------------------------------------------------- #
# Test 1: Output matches JSON Schema                                           #
# --------------------------------------------------------------------------- #

def test_output_matches_schema():
    """Validate mỗi output file theo l3a-output-v2.schema.json.

    Hard gate: unscorable_schema → 0 điểm.
    """
    outputs = _get_outputs_or_skip()
    contracts = Contracts(CONTRACTS_DIR)

    errors = []
    for filename, data in outputs:
        try:
            contracts.validate_output(data, filename)
        except Exception as exc:
            errors.append(f"{filename}: {exc}")

    assert not errors, "Output không đúng schema:\n" + "\n".join(errors)


# --------------------------------------------------------------------------- #
# Test 2: case_id khớp giữa output và input                                   #
# --------------------------------------------------------------------------- #

def test_case_id_matches():
    """output.case_id phải == input.case_id (và filename).

    Hard gate: case_id_mismatch → 0 điểm.
    """
    outputs = _get_outputs_or_skip()

    errors = []
    for filename, data in outputs:
        expected_case_id = filename.replace(".json", "")
        actual_case_id = data.get("case_id", "")
        if actual_case_id != expected_case_id:
            errors.append(
                f"{filename}: expected case_id='{expected_case_id}', got '{actual_case_id}'"
            )

    assert not errors, "case_id mismatch:\n" + "\n".join(errors)


# --------------------------------------------------------------------------- #
# Test 3: evidence_ref format hợp lệ                                          #
# --------------------------------------------------------------------------- #

def test_evidence_ref_format():
    """Tất cả evidence_refs phải match ^ev_[A-Za-z0-9_-]{20,96}$.

    Hard gate: invalid_evidence_refs → 0 điểm.
    """
    outputs = _get_outputs_or_skip()

    errors = []
    for filename, data in outputs:
        all_refs = data.get("evidence_refs", [])
        # Cũng kiểm tra claim_assessments
        for ca in data.get("claim_assessments", []):
            all_refs.extend(ca.get("evidence_refs", []))

        for ref in all_refs:
            if not EVIDENCE_REF_PATTERN.match(ref):
                errors.append(f"{filename}: invalid evidence_ref '{ref}'")

    assert not errors, "evidence_ref format sai:\n" + "\n".join(errors)


# --------------------------------------------------------------------------- #
# Test 4: evidence_refs không trống                                            #
# --------------------------------------------------------------------------- #

def test_evidence_refs_not_empty():
    """Output phải có ≥1 evidence_ref.

    Hard gate: missing_required_evidence → 0 điểm.
    """
    outputs = _get_outputs_or_skip()

    errors = []
    for filename, data in outputs:
        refs = data.get("evidence_refs", [])
        if not refs:
            errors.append(f"{filename}: evidence_refs trống")

    assert not errors, "Thiếu evidence_refs:\n" + "\n".join(errors)


# --------------------------------------------------------------------------- #
# Test 5: primary_issue hợp lệ                                                #
# --------------------------------------------------------------------------- #

def test_primary_issue_valid():
    """assessment.primary_issue phải thuộc 11 giá trị enum cho phép."""
    outputs = _get_outputs_or_skip()

    errors = []
    for filename, data in outputs:
        assessment = data.get("assessment", {})
        issue = assessment.get("primary_issue", "")
        if issue not in VALID_PRIMARY_ISSUES:
            errors.append(f"{filename}: invalid primary_issue '{issue}'")

    assert not errors, "primary_issue không hợp lệ:\n" + "\n".join(errors)


# --------------------------------------------------------------------------- #
# Test 6: confidence trong [0, 1]                                              #
# --------------------------------------------------------------------------- #

def test_confidence_in_range():
    """confidence phải nằm trong [0, 1] — cả assessment lẫn claim."""
    outputs = _get_outputs_or_skip()

    errors = []
    for filename, data in outputs:
        # Assessment confidence
        assessment = data.get("assessment", {})
        conf = assessment.get("confidence")
        if conf is not None and not (0 <= conf <= 1):
            errors.append(f"{filename}: assessment.confidence={conf} ngoài [0,1]")

        # Claim confidence
        for ca in data.get("claim_assessments", []):
            claim_conf = ca.get("confidence")
            if claim_conf is not None and not (0 <= claim_conf <= 1):
                errors.append(
                    f"{filename}: claim {ca.get('claim_id')}.confidence={claim_conf} ngoài [0,1]"
                )

    assert not errors, "confidence ngoài phạm vi:\n" + "\n".join(errors)


# --------------------------------------------------------------------------- #
# Test 7: Tổng refund_lines == recommended_refund_brl                          #
# --------------------------------------------------------------------------- #

def test_refund_total_matches_lines():
    """Σ refund_lines.amount_brl phải == recommended_refund_brl.

    Liên quan: consistency score (10%).
    """
    outputs = _get_outputs_or_skip()

    errors = []
    for filename, data in outputs:
        fin = data.get("financial_resolution", {})
        recommended = fin.get("recommended_refund_brl", 0)
        lines = fin.get("refund_lines", [])
        total_lines = sum(line.get("amount_brl", 0) for line in lines)

        if abs(total_lines - recommended) > 0.01:
            errors.append(
                f"{filename}: recommended_refund={recommended}, "
                f"sum(refund_lines)={total_lines}, diff={abs(total_lines - recommended):.2f}"
            )

    assert not errors, "Refund total không khớp:\n" + "\n".join(errors)


# --------------------------------------------------------------------------- #
# Test 8: no_action → refund = 0                                              #
# --------------------------------------------------------------------------- #

def test_no_action_means_zero_refund():
    """Nếu case_status="no_action" thì recommended_refund_brl phải = 0.

    Liên quan: consistency score (10%).
    """
    outputs = _get_outputs_or_skip()

    errors = []
    for filename, data in outputs:
        assessment = data.get("assessment", {})
        status = assessment.get("case_status", "")
        if status == "no_action":
            fin = data.get("financial_resolution", {})
            refund = fin.get("recommended_refund_brl", 0)
            if refund != 0:
                errors.append(
                    f"{filename}: case_status=no_action nhưng refund={refund}"
                )

    assert not errors, "no_action nhưng có refund:\n" + "\n".join(errors)


# --------------------------------------------------------------------------- #
# Test 9: action_required → resolution_actions không trống                     #
# --------------------------------------------------------------------------- #

def test_resolution_actions_not_empty_when_action_required():
    """Nếu case_status="action_required" thì resolution_actions phải có ≥1 item.

    Liên quan: consistency score (10%).
    """
    outputs = _get_outputs_or_skip()

    errors = []
    for filename, data in outputs:
        assessment = data.get("assessment", {})
        status = assessment.get("case_status", "")
        if status == "action_required":
            actions = data.get("resolution_actions", [])
            if not actions:
                errors.append(
                    f"{filename}: case_status=action_required nhưng resolution_actions trống"
                )

    assert not errors, "action_required nhưng không có actions:\n" + "\n".join(errors)


# --------------------------------------------------------------------------- #
# Test 10: Claim evidence_refs ⊆ global evidence_refs                         #
# --------------------------------------------------------------------------- #

def test_claim_evidence_subset_of_global():
    """Mỗi claim's evidence_refs phải là tập con của output's evidence_refs.

    Liên quan: consistency score (10%).
    """
    outputs = _get_outputs_or_skip()

    errors = []
    for filename, data in outputs:
        global_refs = set(data.get("evidence_refs", []))
        for ca in data.get("claim_assessments", []):
            claim_refs = set(ca.get("evidence_refs", []))
            extra = claim_refs - global_refs
            if extra:
                errors.append(
                    f"{filename}: claim {ca.get('claim_id')} has refs not in global: {extra}"
                )

    assert not errors, "Claim evidence_refs không phải subset:\n" + "\n".join(errors)
