"""Unit tests for PaymentAgent – Payment & Financial Resolution Specialist.

Kiểm thử toàn diện:
  - valid_split_payment  : nhiều phương thức hợp lệ → refund = 0
  - duplicate_charge     : phát hiện từ payment list hoặc timeline
  - payment_mismatch     : overcharge → hoàn chênh lệch
  - refund_failed        : yêu cầu gửi lại lệnh hoàn tiền
  - refund_pending       : yêu cầu expedite lệnh đang treo
  - order_canceled       : đơn hủy chưa hoàn → hoàn toàn bộ
  - no-action case       : giao dịch hoàn toàn bình thường
  - DoD invariant        : sum(refund_lines) == recommended_refund_brl mọi trường hợp
  - Floating-point safe  : 0.10 + 0.20 == 0.30
"""
from __future__ import annotations

import pytest

from student_agent.agents.payment_agent import PaymentAgent, PaymentInvestigationResult


@pytest.fixture
def agent() -> PaymentAgent:
    return PaymentAgent()


def _r(agent: PaymentAgent, **kwargs: object) -> PaymentInvestigationResult:
    """Shortcut để gọi _reconcile với defaults."""
    defaults: dict = dict(
        case_id="L3A_CASE_TEST",
        order_id="ord_test",
        payments=[],
        payment_timeline=[],
        refund_timeline=[],
        evidence_refs=[],
        expected_order_total=None,
        order_status=None,
    )
    defaults.update(kwargs)
    return agent._reconcile(**defaults)   # type: ignore[arg-type]


def _assert_dod(result: PaymentInvestigationResult) -> None:
    """Kiểm tra DoD: sum(refund_lines.amount_brl) == recommended_refund_brl."""
    fin = result.financial_resolution
    total = round(sum(line["amount_brl"] for line in fin["refund_lines"]), 2)
    assert total == round(fin["recommended_refund_brl"], 2), (
        f"DoD violated: sum={total} != recommended={fin['recommended_refund_brl']}"
    )


# ---------------------------------------------------------------------------
# 1. Giao dịch hợp lệ, không cần hoàn tiền
# ---------------------------------------------------------------------------

class TestNoAction:
    def test_single_payment_delivered(self, agent: PaymentAgent) -> None:
        res = _r(agent,
            payments=[{"payment_reference": "pay_001", "payment_type": "credit_card", "payment_value": 150.0}],
            expected_order_total=150.0,
            order_status="delivered",
        )
        assert res.is_valid_transaction is True
        assert res.detected_issue is None
        assert res.financial_resolution["recommended_refund_brl"] == 0.0
        assert res.financial_resolution["refund_lines"] == []
        assert res.financial_resolution["currency"] == "BRL"
        _assert_dod(res)

    def test_payment_references_collected(self, agent: PaymentAgent) -> None:
        payments = [
            {"payment_reference": "pay_A", "payment_type": "credit_card", "payment_value": 100.0},
        ]
        res = _r(agent, payments=payments)
        assert "pay_A" in res.payment_references


# ---------------------------------------------------------------------------
# 2. valid_split_payment
# ---------------------------------------------------------------------------

class TestValidSplitPayment:
    def test_voucher_plus_credit_card(self, agent: PaymentAgent) -> None:
        payments = [
            {"payment_reference": "pay_vouch", "payment_type": "voucher",     "payment_value": 40.0},
            {"payment_reference": "pay_cc",    "payment_type": "credit_card", "payment_value": 60.0},
        ]
        res = _r(agent, payments=payments, expected_order_total=100.0, order_status="delivered")
        assert res.detected_issue == "valid_split_payment"
        assert res.is_valid_transaction is True
        assert res.financial_resolution["recommended_refund_brl"] == 0.0
        assert res.financial_resolution["refund_lines"] == []
        assert set(res.payment_references) == {"pay_vouch", "pay_cc"}
        _assert_dod(res)

    def test_boleto_plus_voucher(self, agent: PaymentAgent) -> None:
        payments = [
            {"payment_reference": "pay_boleto", "payment_type": "boleto",  "payment_value": 200.0},
            {"payment_reference": "pay_v",      "payment_type": "voucher", "payment_value": 50.0},
        ]
        res = _r(agent, payments=payments, expected_order_total=250.0, order_status="delivered")
        assert res.detected_issue == "valid_split_payment"
        _assert_dod(res)


# ---------------------------------------------------------------------------
# 3. payment_mismatch
# ---------------------------------------------------------------------------

class TestPaymentMismatch:
    def test_overcharge(self, agent: PaymentAgent) -> None:
        payments = [{"payment_reference": "pay_mm", "payment_type": "credit_card", "payment_value": 125.75}]
        res = _r(agent, payments=payments, expected_order_total=100.0, order_status="delivered")
        assert res.detected_issue == "payment_mismatch"
        fin = res.financial_resolution
        assert fin["recommended_refund_brl"] == 25.75
        assert len(fin["refund_lines"]) == 1
        assert fin["refund_lines"][0]["reason_code"] == "overcharge_mismatch"
        assert fin["refund_lines"][0]["amount_brl"] == 25.75
        _assert_dod(res)

    def test_underpayment_no_refund(self, agent: PaymentAgent) -> None:
        # Underpayment không gây refund – để policy agent xử lý
        payments = [{"payment_reference": "pay_under", "payment_type": "boleto", "payment_value": 80.0}]
        res = _r(agent, payments=payments, expected_order_total=100.0, order_status="delivered")
        assert res.detected_issue not in {"payment_mismatch", "duplicate_charge"}
        assert res.financial_resolution["recommended_refund_brl"] == 0.0
        _assert_dod(res)


# ---------------------------------------------------------------------------
# 4. duplicate_charge – phát hiện từ payment list
# ---------------------------------------------------------------------------

class TestDuplicateCharge:
    def test_duplicate_credit_card_same_amount_as_expected(self, agent: PaymentAgent) -> None:
        payments = [
            {"payment_reference": "pay_orig", "payment_type": "credit_card", "payment_value": 150.0},
            {"payment_reference": "pay_dup",  "payment_type": "credit_card", "payment_value": 150.0},
        ]
        res = _r(agent, payments=payments, expected_order_total=150.0, order_status="delivered")
        assert res.detected_issue == "duplicate_charge"
        fin = res.financial_resolution
        assert fin["recommended_refund_brl"] == 150.0
        assert len(fin["refund_lines"]) == 1
        assert fin["refund_lines"][0]["reason_code"] == "duplicate_charge"
        assert fin["refund_lines"][0]["entity_id"] == "pay_dup"
        _assert_dod(res)

    def test_duplicate_detected_via_timeline(self, agent: PaymentAgent) -> None:
        payments = [
            {"payment_reference": "pay_x", "payment_type": "credit_card", "payment_value": 99.0},
        ]
        timeline = [
            {"event_type": "captured", "external_transaction_id": "ext_txn_001",
             "payment_reference": "pay_x",     "amount": 99.0},
            {"event_type": "captured", "external_transaction_id": "ext_txn_001",
             "payment_reference": "pay_x_dup", "amount": 99.0},
        ]
        res = _r(agent, payments=payments, payment_timeline=timeline, expected_order_total=99.0)
        assert res.detected_issue == "duplicate_charge"
        _assert_dod(res)


# ---------------------------------------------------------------------------
# 5. refund_failed
# ---------------------------------------------------------------------------

class TestRefundFailed:
    def test_failed_refund_triggers_retry_line(self, agent: PaymentAgent) -> None:
        payments = [{"payment_reference": "pay_fail", "payment_type": "credit_card", "payment_value": 80.0}]
        refunds  = [{"refund_id": "ref_fail_01", "status": "failed", "amount": 80.0}]
        res = _r(agent, payments=payments, refund_timeline=refunds,
                 expected_order_total=80.0, order_status="canceled")
        assert res.detected_issue == "refund_failed"
        fin = res.financial_resolution
        assert fin["recommended_refund_brl"] == 80.0
        assert fin["refund_lines"][0]["reason_code"] == "refund_failed_retry"
        assert fin["refund_lines"][0]["entity_id"] == "ref_fail_01"
        _assert_dod(res)

    def test_rejected_status_treated_as_failed(self, agent: PaymentAgent) -> None:
        refunds = [{"refund_id": "ref_rej", "status": "rejected", "amount": 50.0}]
        res = _r(agent, refund_timeline=refunds)
        assert res.detected_issue == "refund_failed"
        _assert_dod(res)


# ---------------------------------------------------------------------------
# 6. refund_pending
# ---------------------------------------------------------------------------

class TestRefundPending:
    def test_pending_refund_expedite_line(self, agent: PaymentAgent) -> None:
        payments = [{"payment_reference": "pay_pend", "payment_type": "credit_card", "payment_value": 60.0}]
        refunds  = [{"refund_id": "ref_pend_01", "status": "pending", "amount": 60.0}]
        res = _r(agent, payments=payments, refund_timeline=refunds,
                 expected_order_total=60.0, order_status="canceled")
        assert res.detected_issue == "refund_pending"
        fin = res.financial_resolution
        assert fin["recommended_refund_brl"] == 60.0
        assert fin["refund_lines"][0]["reason_code"] == "refund_pending_expedite"
        _assert_dod(res)

    def test_processing_status_treated_as_pending(self, agent: PaymentAgent) -> None:
        refunds = [{"refund_id": "ref_proc", "status": "processing", "amount": 25.0}]
        res = _r(agent, refund_timeline=refunds)
        assert res.detected_issue == "refund_pending"
        _assert_dod(res)


# ---------------------------------------------------------------------------
# 7. Đơn bị hủy – chưa hoàn tiền
# ---------------------------------------------------------------------------

class TestCanceledOrder:
    def test_canceled_order_triggers_full_refund(self, agent: PaymentAgent) -> None:
        payments = [{"payment_reference": "pay_cancel", "payment_type": "boleto", "payment_value": 200.0}]
        res = _r(agent, payments=payments, expected_order_total=200.0, order_status="canceled")
        fin = res.financial_resolution
        assert fin["recommended_refund_brl"] == 200.0
        assert fin["refund_lines"][0]["reason_code"] == "order_canceled"
        _assert_dod(res)

    def test_canceled_order_already_refunded_no_action(self, agent: PaymentAgent) -> None:
        payments = [{"payment_reference": "pay_done", "payment_type": "credit_card", "payment_value": 100.0}]
        refunds  = [{"refund_id": "ref_done", "status": "completed", "amount": 100.0}]
        res = _r(agent, payments=payments, refund_timeline=refunds, order_status="canceled")
        # Đã hoàn đủ rồi → refund_lines rỗng
        assert res.financial_resolution["recommended_refund_brl"] == 0.0
        assert res.financial_resolution["refund_lines"] == []
        _assert_dod(res)


# ---------------------------------------------------------------------------
# 8. DoD & Floating-point safety
# ---------------------------------------------------------------------------

class TestDoD:
    def test_floating_point_sum_is_exact(self, agent: PaymentAgent) -> None:
        """0.10 + 0.20 phải bằng đúng 0.30, không phải 0.30000000000000004.

        recommended_refund_brl được tính từ Decimal → chính xác.
        refund_lines lưu float, nên dùng round() để so sánh.
        """
        refunds = [
            {"refund_id": "r1", "status": "failed", "amount": 0.10},
            {"refund_id": "r2", "status": "failed", "amount": 0.20},
        ]
        res = _r(agent, refund_timeline=refunds)
        fin = res.financial_resolution
        assert fin["recommended_refund_brl"] == 0.30
        # float sum có thể ra 0.30000000000000004 → dùng round() để kiểm tra
        assert round(sum(line["amount_brl"] for line in fin["refund_lines"]), 2) == 0.30
        _assert_dod(res)

    def test_dod_invariant_always_holds_for_multiple_lines(self, agent: PaymentAgent) -> None:
        refunds = [
            {"refund_id": "r1", "status": "failed", "amount": 10.50},
            {"refund_id": "r2", "status": "failed", "amount": 5.75},
            {"refund_id": "r3", "status": "failed", "amount": 3.33},
        ]
        res = _r(agent, refund_timeline=refunds)
        _assert_dod(res)
        assert res.financial_resolution["recommended_refund_brl"] == 19.58


# ---------------------------------------------------------------------------
# 9. payment_references maxItems
# ---------------------------------------------------------------------------

class TestPaymentReferences:
    def test_max_20_references(self, agent: PaymentAgent) -> None:
        payments = [
            {"payment_reference": f"pay_{i:03d}", "payment_type": "credit_card", "payment_value": 1.0}
            for i in range(25)
        ]
        res = _r(agent, payments=payments)
        assert len(res.payment_references) <= 20

    def test_duplicate_refs_deduplicated(self, agent: PaymentAgent) -> None:
        payments = [
            {"payment_reference": "pay_same", "payment_type": "voucher", "payment_value": 10.0},
            {"payment_reference": "pay_same", "payment_type": "voucher", "payment_value": 10.0},
        ]
        res = _r(agent, payments=payments)
        assert res.payment_references.count("pay_same") == 1
