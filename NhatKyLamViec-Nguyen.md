# 📓 Nhật Ký Làm Việc — Nguyên (Policy Agent)

> **Mục đích file này**: Giúp các thành viên trong nhóm (và AI tools như Antigravity, Codex) hiểu Nguyên đã làm gì, sửa gì, và cách phối hợp code.

---

## 👤 Thông tin

- **Người**: Nguyên
- **Vai trò**: Policy Agent + Validation Tests
- **Nhánh Git**: `feature/policy-agent`
- **Ngày bắt đầu**: 2026-09-25

---

## 📁 Danh sách file đã tạo/sửa

| File | Trạng thái | Mô tả |
|---|---|---|
| `src/student_agent/agents/__init__.py` | ✅ Tạo mới | Package init, export `PolicyAgent` |
| `src/student_agent/agents/policy_agent.py` | ✅ Tạo mới | Logic chính — class `PolicyAgent` + `PolicyResult` + `ClaimAssessment` |
| `tests/test_policy_agent.py` | ✅ Tạo mới | 10 unit tests (mock, không cần MCP thật) |
| `tests/test_invariants.py` | ✅ Tạo mới | 10 validation tests — kiểm tra output trước khi nộp |
| `NhatKyLamViec-Nguyen.md` | ✅ Tạo mới | File này |

---

## 🏗️ Kiến trúc Policy Agent

### Vai trò trong hệ thống
Policy Agent là **"quan tòa"** — nhận evidence từ các agent khác (Order, Payment, Shipment), lấy policy từ MCP, rồi ra verdict cho mỗi claim.

### Luồng xử lý
```
Coordinator gọi PolicyAgent.evaluate()
  → Bước 1: Gọi MCP get_policy → lấy policy data + evidence_ref
  → Bước 2: Đánh giá từng claim (đối chiếu evidence với policy)
  → Bước 3: Xác định primary_issue (1 trong 11 loại)
  → Bước 4: Tính financial_resolution (refund)
  → Bước 5: Đề xuất resolution_actions
  → Bước 6: Emit trace "policy_decided"
  → Trả về PolicyResult
```

### Class diagram
```
PolicyAgent
├── evaluate(case_id, policy_version, claims, findings) → PolicyResult
├── _fetch_policy(case_id, policy_version) → (data, ref)
├── _evaluate_single_claim(claim, findings, policy_data, ...) → ClaimAssessment
├── _check_canceled_order_paid(order, payment) → (verdict, confidence)
├── _check_unavailable_order_paid(order, payment) → (verdict, confidence)
├── _check_late_delivery_seller(order, shipment) → (verdict, confidence)
├── _check_late_delivery_logistics(order, shipment) → (verdict, confidence)
├── _check_valid_split_payment(order, payment) → (verdict, confidence)
├── _check_payment_mismatch(order, payment) → (verdict, confidence)
├── _check_duplicate_charge(payment) → (verdict, confidence)
├── _check_refund_status(topic, payment) → (verdict, confidence)
├── _check_requested_full_refund(order, payment, policy) → (verdict, confidence)
├── _determine_primary_issue(claims, assessments, findings) → str
├── _determine_case_status(assessments) → str
├── _compute_overall_confidence(assessments) → float
├── _compute_financial_resolution(...) → dict
└── _determine_resolution_actions(...) → list[str]

PolicyResult (dataclass)
├── claim_assessments: list[ClaimAssessment]
├── primary_issue: str
├── case_status: str
├── confidence: float
├── resolution_actions: list[str]
├── financial_resolution: dict
└── evidence_refs: list[str]

ClaimAssessment (dataclass)
├── claim_id: str
├── verdict: str
├── confidence: float
└── evidence_refs: list[str]
```

---

## 🔌 Interface với Coordinator (Tuấn)

### Cách Coordinator gọi Policy Agent

```python
from student_agent.agents import PolicyAgent

# Trong workflow.py hoặc coordinator.py:
policy_agent = PolicyAgent(gateway, trace)

# Emit trace task_assigned TRƯỚC khi gọi
trace.emit(case_id=case_id, event_type="task_assigned",
           actor="coordinator", target="policy-agent")

# Gọi evaluate
policy_result = await policy_agent.evaluate(
    case_id=case["case_id"],
    policy_version=case.get("policy_version", "EC_POLICY_V1"),
    claims=case["customer_request"]["claims"],
    findings={
        "order":    {"data": order_data,    "evidence_ref": order_ref},
        "payment":  {"data": payment_data,  "evidence_ref": payment_ref},
        "shipment": {"data": shipment_data, "evidence_ref": shipment_ref},
        "items":    {"data": items_data,    "evidence_ref": items_ref},      # optional
        "seller":   {"data": seller_data,   "evidence_ref": seller_ref},     # optional
    },
)

# Emit trace handoff SAU khi nhận kết quả
trace.emit(case_id=case_id, event_type="handoff",
           actor="policy-agent", target="coordinator")

# Sử dụng kết quả
result_dict = policy_result.to_dict()
```

### Format `findings` (input)

```python
findings = {
    "order": {
        "data": {                          # dữ liệu thật từ MCP get_order
            "order_id": "abc123...",
            "order_status": "canceled",    # canceled | delivered | shipped | ...
            "price": 100.0,
            "freight_value": 20.0,
            "order_estimated_delivery_date": "2018-01-10",
            "order_delivered_customer_date": "2018-01-20",
            "order_delivered_carrier_date": "2018-01-07",
        },
        "evidence_ref": "ev_order_abc123..."  # ref thật từ MCP
    },
    "payment": {
        "data": {                          # có thể là dict hoặc list[dict]
            "payment_type": "credit_card",
            "payment_value": 120.0,
        },
        "evidence_ref": "ev_payment_xyz..."
    },
    "shipment": {
        "data": {
            "shipping_limit_date": "2018-01-08",
            "carrier": "carrier_name",
        },
        "evidence_ref": "ev_shipment_def..."
    },
}
```

### Format `PolicyResult` (output)

```python
PolicyResult(
    claim_assessments=[
        ClaimAssessment(
            claim_id="claim-001-a",
            verdict="supported",              # supported | unsupported | partially_supported | insufficient_evidence
            confidence=0.92,                   # 0.0 → 1.0
            evidence_refs=["ev_order_...", "ev_payment_..."]
        )
    ],
    primary_issue="canceled_order_paid",       # 1 trong 11 giá trị
    case_status="action_required",             # action_required | no_action | needs_investigation
    confidence=0.92,
    resolution_actions=["issue_refund", "notify_customer"],
    financial_resolution={
        "currency": "BRL",
        "recommended_refund_brl": 120.50,
        "refund_lines": [
            {"reason_code": "canceled_order_paid", "amount_brl": 120.50, "entity_id": "order_id"}
        ]
    },
    evidence_refs=["ev_order_...", "ev_payment_...", "ev_policy_..."]
)
```

---

## 🧪 Chạy tests

```bash
# Unit tests cho Policy Agent (mock, chạy độc lập)
pytest tests/test_policy_agent.py -v

# Validation tests (cần có output trong outputs/ — chạy sau 'day09 run')
pytest tests/test_invariants.py -v

# Tất cả tests
pytest -v
```

---

## ⚠️ Lưu ý quan trọng cho nhóm

1. **Actor name**: Policy Agent dùng `"policy-agent"` trong trace. Coordinator cần dùng đúng tên này.

2. **Trace events Policy Agent tự emit**:
   - `tool_result_consumed` (khi lấy policy từ MCP)
   - `policy_decided` (khi ra quyết định)

3. **Trace events Coordinator cần emit** (TRƯỚC/SAU khi gọi Policy Agent):
   - `task_assigned` (actor="coordinator", target="policy-agent") — trước
   - `handoff` (actor="policy-agent", target="coordinator") — sau

4. **Dependency**: Policy Agent chỉ import từ `student_agent.mcp_gateway` và `student_agent.trace` — không phụ thuộc code của agent khác.

5. **pytest-asyncio**: Tests dùng `@pytest.mark.asyncio`, cần cài `pytest-asyncio`.

---

## 📝 Changelog

| Ngày | Thay đổi |
|---|---|
| 2026-09-25 | Tạo `policy_agent.py` — implement 10 loại claim topic, financial_resolution, resolution_actions |
| 2026-09-25 | Tạo `test_policy_agent.py` — 10 unit tests với mock |
| 2026-09-25 | Tạo `test_invariants.py` — 10 validation checks cho output |
| 2026-09-25 | Tạo `NhatKyLamViec-Nguyen.md` |
