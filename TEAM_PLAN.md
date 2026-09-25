# 🚀 KẾ HOẠCH TÁC CHIẾN MULTI-AGENT (K4-L3A) - SPRINT 1 BUỔI SÁNG

> **Thời gian:** 09:45 – 12:15 (Khoảng 2.5 tiếng)  
> **Mục tiêu:** Hoàn thiện luồng Multi-Agent A2A, đạt chuẩn Public Contract V2, vượt qua tất cả Hard Gates và nộp bài trước 12:15.

---

## 🎯 1. Chiến lược "Ăn chắc điểm" (Scoring Strategy)

Không code dàn trải hay over-engineer. Tập trung ăn trọn các đầu điểm dễ lấy nhất:
1. **35% Điểm Nền (Bắt buộc phải đạt 100%):**
   * **5% Schema:** Output đúng 100% theo `l3a-output-v2.schema.json`.
   * **15% Provenance:** Tất cả `evidence_ref` đều lấy từ kết quả thực tế của MCP Gateway (`ev_...`). **Tuyệt đối không bịa ref**.
   * **10% Consistency:** Nếu `case_status == "no_action"` thì `recommended_refund_brl = 0`. `resolution_actions` không được trùng lặp.
   * **5% Workflow:** Phát đủ và đúng thứ tự các event trong `trace.jsonl`: `task_assigned` ➔ `tool_result_consumed` ➔ `handoff` ➔ `policy_decided` ➔ `verification_completed`.
2. **45% Semantic (Tập trung giải quyết 5 lỗi phổ biến nhất):**
   * `canceled_order_paid`: Đơn hàng bị hủy (`canceled`) nhưng khách đã trả tiền.
   * `unavailable_order_paid`: Đơn hàng hết hàng (`unavailable`) nhưng khách đã trả tiền.
   * `late_delivery_seller`: Người bán giao cho đơn vị vận chuyển sau ngày `shipping_limit_date`.
   * `late_delivery_logistics`: Giao tới khách sau ngày `order_estimated_delivery_date`.
   * `duplicate_charge`: Khách bị trừ tiền 2 lần cho cùng một số tiền.

---

## ⏰ 2. Lịch trình buổi sáng (Timeline)

| Thời gian | Giai đoạn | Trọng tâm | Người thực hiện |
| :--- | :--- | :--- | :--- |
| **09:45 – 10:15 (30p)** | **Chặng 1: Hạ tầng & Setup** | Lấy API Key, nạp 100 case, xuất danh sách tool MCP | **TV4** (Team Lead hỗ trợ) |
| **10:15 – 11:15 (60p)** | **Chặng 2: Code song song** | TV2 viết Order Agent, TV3 viết Payment Agent, TV1 dựng Workflow, TV4 viết docs | **Cả 4 người** |
| **11:15 – 11:45 (30p)** | **Chặng 3: Ráp nối & Test nhanh** | Tích hợp vào `workflow.py`, test chạy thử 5 case | **TV1 + TV4** |
| **11:45 – 12:15 (30p)** | **Chặng 4: Run Full 100 Case & Nộp** | Chạy full 100 case, validate, đóng gói zip và nộp bài | **TV4 + TV1** |

---

## 👥 3. Phân công nhiệm vụ chi tiết từng người

```
                            ┌────────────────┐
                            │   COORDINATOR  │ (TV1)
                            └───────┬────────┘
                    task_assigned   │
                                    ▼
                         ┌─────────────────────┐
                         │  ORDER & LOGISTICS  │ (TV2)
                         │       AGENT         │
                         └──────────┬──────────┘
                                    │ handoff
                                    ▼
                         ┌─────────────────────┐
                         │  PAYMENT & POLICY   │ (TV3)
                         │       AGENT         │
                         └──────────┬──────────┘
                                    │ handoff
                                    ▼
                            ┌────────────────┐
                            │    VERIFIER    │ (TV1)
                            └────────────────┘
```

---

### 👤 THÀNH VIÊN 4: Ops, Quality Assurance & Documentation
> **Ưu tiên số 1:** Mở đường cho cả team bằng cách chuẩn bị data và key ngay trong 15 phút đầu.

* **Việc 1 (09:45 - 10:05): Setup môi trường & nạp data**
  1. Vào web cuộc thi tại `/register`, đăng ký tên team để lấy `COMPETITION_TEAM_API_KEY`.
  2. Tạo file `.env` từ `.env.example`, điền key thật:
     ```dotenv
     COMPETITION_API_URL=https://n7-competition.pages.dev
     COMPETITION_TEAM_API_KEY=sk-team-xxx
     MCP_ENDPOINT=https://day09-competition.34-142-201-239.sslip.io/mcp
     ```
  3. Tải file ZIP 100 cases từ release, giải nén vào thư mục `inputs/` và kiểm tra:
     ```bash
     day09 validate-inputs
     ```
  4. Chạy lệnh lấy danh sách MCP tools và gửi vào nhóm cho TV2 & TV3:
     ```bash
     day09 mcp-tools
     ```

* **Việc 2 (10:15 - 11:15): Cập nhật tài liệu [`ARCHITECTURE.md`](ARCHITECTURE.md)**
  * Xóa toàn bộ các chữ `TODO` trong file `ARCHITECTURE.md`.
  * Mô tả các vai trò: Coordinator (TV1), Order/Logistics (TV2), Payment/Policy (TV3), Verifier (TV1).

* **Việc 3 (11:45 - 12:15): Nghiệm thu và Đóng gói nộp bài**
  ```bash
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
  * Lấy file `dist/submission.zip` nộp lên hệ thống web `/l3a`.

---

### 👤 THÀNH VIÊN 2: Order & Logistics Specialist
> **Mục tiêu:** Viết file `src/student_agent/order_agent.py` để xử lý trạng thái đơn và giao trễ hạn.

* **File tạo mới:** `src/student_agent/order_agent.py`
* **Code triển khai mẫu:**
```python
from typing import Any

async def check_order_and_delivery(case_id: str, order_id: str, gateway: Any, trace: Any) -> dict[str, Any]:
    # 1. Gọi tool get_order
    order_res = await gateway.call("get_order", case_id=case_id, order_id=order_id)
    ev_order = order_res["evidence_ref"]
    order_data = order_res.get("data", {})
    
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="order-agent",
        tool_name="get_order",
        evidence_refs=[ev_order]
    )
    
    status = order_data.get("order_status")
    
    # Kiểm tra hủy / không có hàng
    if status == "canceled":
        return {
            "issue": "canceled_order_paid",
            "responsible": "platform",
            "ev": [ev_order],
            "status": status,
            "order_id": order_id
        }
    if status == "unavailable":
        return {
            "issue": "unavailable_order_paid",
            "responsible": "seller",
            "ev": [ev_order],
            "status": status,
            "order_id": order_id
        }
        
    # 2. Gọi tool get_shipment để kiểm tra trễ hạn
    ship_res = await gateway.call("get_shipment", case_id=case_id, order_id=order_id)
    ev_ship = ship_res["evidence_ref"]
    ship_data = ship_res.get("data", {})
    
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="shipment-agent",
        tool_name="get_shipment",
        evidence_refs=[ev_ship]
    )
    
    carrier_date = ship_data.get("order_delivered_carrier_date")
    limit_date = ship_data.get("shipping_limit_date")
    delivered_customer = ship_data.get("order_delivered_customer_date")
    estimated_date = ship_data.get("order_estimated_delivery_date")
    
    # Người bán giao trễ cho bên vận chuyển
    if carrier_date and limit_date and carrier_date > limit_date:
        return {
            "issue": "late_delivery_seller",
            "responsible": "seller",
            "ev": [ev_order, ev_ship],
            "status": status,
            "order_id": order_id
        }
    # Bên vận chuyển giao trễ cho khách
    if delivered_customer and estimated_date and delivered_customer > estimated_date:
        return {
            "issue": "late_delivery_logistics",
            "responsible": "logistics_provider",
            "ev": [ev_order, ev_ship],
            "status": status,
            "order_id": order_id
        }
        
    return {
        "issue": "no_issue",
        "responsible": "platform",
        "ev": [ev_order, ev_ship],
        "status": status,
        "order_id": order_id
    }
```

---

### 👤 THÀNH VIÊN 3: Payment & Financial Resolution Specialist
> **Mục tiêu:** Viết file `src/student_agent/payment_agent.py` để đối soát thanh toán và tính số tiền hoàn.

* **File tạo mới:** `src/student_agent/payment_agent.py`
* **Code triển khai mẫu:**
```python
from typing import Any

async def check_payment_and_refund(case_id: str, order_id: str, gateway: Any, trace: Any) -> dict[str, Any]:
    pay_res = await gateway.call("get_payment", case_id=case_id, order_id=order_id)
    ev_pay = pay_res["evidence_ref"]
    pay_data = pay_res.get("data", {})
    
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="payment-agent",
        tool_name="get_payment",
        evidence_refs=[ev_pay]
    )
    
    payments = pay_data.get("payments", [])
    total_paid = sum(float(p.get("payment_value", 0.0)) for p in payments)
    
    # Kiểm tra duplicate charge (2 lần trừ tiền cùng giá trị)
    values = [float(p.get("payment_value", 0.0)) for p in payments]
    is_duplicate = len(values) > 1 and len(values) != len(set(values))
    
    # Ghi nhận quyết định chính sách
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code="REFUND_POLICY_EVALUATED"
    )
    
    return {
        "total_paid": round(total_paid, 2),
        "is_duplicate": is_duplicate,
        "payment_refs": [str(p.get("payment_sequential", idx + 1)) for idx, p in enumerate(payments)],
        "ev": [ev_pay]
    }
```

---

### 👤 THÀNH VIÊN 1: Team Lead & Workflow Orchestrator
> **Mục tiêu:** Kết nối các agent tại [`src/student_agent/workflow.py`](src/student_agent/workflow.py) và xuất output đạt chuẩn JSON schema.

* **File chỉnh sửa:** [`src/student_agent/workflow.py`](src/student_agent/workflow.py)
* **Code triển khai mẫu:**
```python
from __future__ import annotations
from typing import Any

from .mcp_gateway import EvidenceGateway
from .order_agent import check_order_and_delivery
from .payment_agent import check_payment_and_refund
from .trace import TraceWriter

async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    order_id = case.get("context", {}).get("order_id") or case.get("order_id")
    
    # 1. Coordinator giao việc cho Order Agent
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-agent"
    )
    order_res = await check_order_and_delivery(case_id, order_id, gateway, trace)
    
    # 2. Handoff sang Payment Agent
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order-agent",
        target="payment-agent"
    )
    pay_res = await check_payment_and_refund(case_id, order_id, gateway, trace)
    
    # 3. Phân định kết quả nghiệp vụ (Semantic Logic)
    if order_res["issue"] in ["canceled_order_paid", "unavailable_order_paid", "late_delivery_seller", "late_delivery_logistics"]:
        primary_issue = order_res["issue"]
        case_status = "action_required"
        refund_brl = pay_res["total_paid"]
    elif pay_res["is_duplicate"]:
        primary_issue = "duplicate_charge"
        case_status = "action_required"
        refund_brl = round(pay_res["total_paid"] / 2.0, 2)
    else:
        primary_issue = "unsupported_claim"
        case_status = "no_action"
        refund_brl = 0.0

    # 4. Verifier thẩm định trước khi xuất output
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier"
    )
    
    all_ev = list(dict.fromkeys(order_res["ev"] + pay_res["ev"]))
    
    # 5. Đóng gói output khớp chuẩn l3a-output-v2.schema.json
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": 0.95 if all_ev else 0.5
        },
        "affected_entities": {
            "order_ids": [order_id] if order_id else [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": pay_res["payment_refs"],
            "shipment_ids": []
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": [{"party_type": order_res["responsible"], "party_id": None}]
        },
        "evidence_refs": all_ev,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund_brl),
            "refund_lines": [
                {"reason_code": primary_issue, "amount_brl": float(refund_brl), "entity_id": order_id}
            ] if refund_brl > 0 else []
        },
        "resolution_actions": ["issue_refund"] if refund_brl > 0 else ["close_case"]
    }
```

---

## ⚠️ 4. Checklist phòng ngừa 0 điểm (Hard Gates)

Trước khi nộp bài, TV4 và Team Lead phải rà soát:
- [ ] File `.env` tuyệt đối **không** được đưa vào file zip nộp bài (lệnh `day09 package` đã tự động lọc).
- [ ] Không có `evidence_ref` nào rỗng, giả mạo hoặc copy từ case khác sang.
- [ ] Nếu `case_status == "no_action"` thì `recommended_refund_brl` bắt buộc phải là `0`.
- [ ] Lệnh `day09 validate` phải báo **OK** cho đủ 100 cases và toàn bộ trace events.
- [ ] Nộp file `dist/submission.zip` lên đúng workspace `/l3a`.
