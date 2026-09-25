# Team Guide — K4 L3A Multi-Agent MCP + A2A

## Mục tiêu chung

Xây dựng hệ thống điều tra khiếu nại thương mại điện tử cho 100 case L3A. Điểm
quan trọng nhất là kết luận đúng và mọi kết luận đều phải được chống đỡ bằng
evidence thật lấy qua MCP Evidence Gateway.

Customer message chỉ là lời khai, **không phải ground truth**. Không tự tạo,
sửa, hoặc dùng chéo `evidence_ref` giữa các case.

## Phân công

### Quân — Coordinator và tích hợp

**Chịu trách nhiệm chính:** điều phối toàn bộ workflow, tích hợp code và tạo
submission cuối.

**Phần việc**

- Chạy `day09 mcp-tools`, ghi lại tên tool và tham số thật để nhóm sử dụng.
- Thiết kế interface chung giữa các specialist.
- Cài đặt `solve_case()` trong `src/student_agent/workflow.py`.
- Emit các `task_assigned`, `handoff` đúng actor/target, gọi verifier, và trả
  output cuối.
- Áp dụng dừng sớm: nếu order không tìm thấy hoặc evidence chưa đủ, không gọi
  tool payment/shipment vô ích; handoff cho policy và verifier.
- Tích hợp các phần của thành viên khác, xử lý Git conflict và đóng gói bài.

**Không làm**

- Không tự tạo evidence, entity ID, số tiền hoàn, hay kết luận không có trong
  handoff của specialist.

**Bàn giao cuối**

- `workflow.py` chạy được 100 case.
- `dist/submission.zip` đã pass validate.

### Tuân — Order, item, seller specialist

**Chịu trách nhiệm chính:** xác minh order và các entity liên quan.

**Phần việc**

- Dùng tool đã discovery để lấy order, item và seller theo `claimed_order_id`.
- Luôn truyền `case_id=case["case_id"]` trong MCP call.
- Chuẩn hóa order status, timeline order, item IDs, seller IDs, tiền hàng/phí
  ship nếu evidence có.
- Nhận diện order canceled, unavailable, không tồn tại, hoặc đủ điều kiện để
  tiếp tục tra payment/shipment.
- Emit `tool_result_consumed` cho từng MCP evidence được dùng.

**Bàn giao cho Quân/Hãn**

```python
{
    "agent": "order-item-agent",
    "evidence_refs": ["ev_..."],
    "entities": {
        "order_ids": [], "item_ids": [], "seller_ids": [],
        "payment_references": [], "shipment_ids": [],
    },
    "facts": {"order_status": "...", "timeline": {}},
    "can_continue": True,
    "confidence": 0.0,
    "status": "complete",
}
```

### Vũ — Payment và refund specialist

**Chịu trách nhiệm chính:** đối soát thanh toán và tính toán số tiền hoàn.

**Phần việc**

- Tra payment/refund chỉ với order ID đã được order specialist xác minh.
- Thu thập payment references, tổng captured, tổng refunded và trạng thái
  refund.
- Phân loại bằng evidence: `valid_split_payment`, `payment_mismatch`,
  `duplicate_charge`, `refund_pending`, `refund_failed`, hoặc
  `insufficient_evidence`.
- Đề xuất `financial_resolution` theo payment evidence và policy decision;
  không coi yêu cầu full refund của khách là căn cứ hoàn tiền.
- Đảm bảo tổng `amount_brl` của `refund_lines` bằng
  `recommended_refund_brl`.
- Emit trace khi tiêu thụ evidence.

**Bàn giao cho Quân/Hãn**

```python
{
    "agent": "payment-agent",
    "evidence_refs": ["ev_..."],
    "entities": {"order_ids": [], "item_ids": [], "seller_ids": [],
                 "payment_references": [], "shipment_ids": []},
    "facts": {"captured_total_brl": 0, "refunded_total_brl": 0},
    "payment_issue": "...",
    "financial_resolution": {"currency": "BRL", "recommended_refund_brl": 0,
                             "refund_lines": []},
    "confidence": 0.0,
    "status": "complete",
}
```

### Điệp — Shipment và policy specialist

**Chịu trách nhiệm chính:** xác định nguyên nhân giao hàng và quyền lợi theo
policy.

**Phần việc**

- Tra evidence shipment, seller shipment và policy theo `policy_version`.
- So sánh thời điểm seller bàn giao, ngày giao dự kiến và thời điểm giao thực tế.
- Chỉ kết luận `late_delivery_seller` khi evidence chứng minh seller trễ; chỉ
  kết luận `late_delivery_logistics` khi seller bàn giao đúng hạn nhưng logistics
  trễ.
- Nếu timeline thiếu/mâu thuẫn, trả về thiếu evidence hoặc data conflict, không
  quy trách nhiệm tùy đoán.
- Diễn giải policy thành điều kiện refund và `resolution_actions` thực thi được.
- Emit trace cho evidence shipment và policy đã dùng.

**Bàn giao cho Quân/Hãn**

```python
{
    "agent": "shipment-policy-agent",
    "evidence_refs": ["ev_..."],
    "entities": {"order_ids": [], "item_ids": [], "seller_ids": [],
                 "payment_references": [], "shipment_ids": []},
    "facts": {"shipment_timeline": {}, "policy_rule": "..."},
    "shipment_issue": "...",
    "responsible_parties": [],
    "policy_decision": {"refund_allowed": False, "actions": []},
    "confidence": 0.0,
    "status": "complete",
}
```

### Hãn — Verifier, contracts và QA

**Chịu trách nhiệm chính:** ngăn output sai schema, sai evidence scope hoặc mâu
thuẫn nghiệp vụ.

**Phần việc**

- Đọc `contracts/schemas/l3a-output-v2.schema.json`,
  `contracts/schemas/trace-event-v1.schema.json` và scoring policy.
- Cài verifier cho các kiểm tra:
  - `case_id` và evidence refs đúng case;
  - entity ID phải được evidence hỗ trợ;
  - primary issue khớp payment/order/shipment facts;
  - refund total khớp refund lines;
  - `action_required` có action hợp lệ;
  - seller bị quy trách nhiệm phải có evidence và ID nếu có;
  - confidence phản ánh evidence thiếu hoặc conflict.
- Emit `verification_completed`, kèm evidence refs đã kiểm tra.
- Hoàn thiện `ARCHITECTURE.md` theo kiến trúc triển khai thật.
- Chạy QA trước và sau khi Quân tích hợp.

**Bàn giao cuối**

- Verifier dùng được trong workflow.
- `ARCHITECTURE.md` hoàn chỉnh.
- Danh sách lỗi/case bất thường cho Quân sửa trước khi nộp.

## Quy ước MCP và trace

1. Tool discovery trước, không đoán tên tool.
2. Mỗi MCP call luôn có `case_id` hiện tại.
3. `evidence_ref` giữ nguyên từ MCP response.
4. Mỗi evidence được sử dụng phải có trace `tool_result_consumed`.
5. Không ghi prompt, chain-of-thought, API key hoặc log nhạy cảm vào trace.
6. Không dùng evidence trả về cho case A trong output của case B.

Ví dụ dùng evidence đúng:

```python
evidence = await gateway.call(
    "tool_name_from_discovery",
    case_id=case_id,
    order_id=order_id,
)

trace.emit(
    case_id=case_id,
    event_type="tool_result_consumed",
    actor="order-item-agent",
    tool_name="tool_name_from_discovery",
    evidence_refs=[evidence["evidence_ref"]],
)
```

## Luồng tích hợp cho mỗi case

```text
Input
  -> Quân: task_assigned(order-item-agent)
  -> Tuân: order/item/seller evidence
  -> Quân: handoff(payment-agent, shipment-policy-agent)
  -> Vũ + Điệp: payment/refund, shipment/policy evidence
  -> Quân: handoff(verifier)
  -> Hãn: verification_completed
  -> Quân: output JSON
  -> CLI: case_finalized
```

Nếu order không có hoặc evidence không đủ, Quân handoff lý do cho Điệp và Hãn;
không thay dữ liệu thiếu bằng giả định.

## Checklist trước khi nộp

```bash
source .venv/bin/activate
pytest -q
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Trước upload, kiểm tra ZIP chỉ có:

```text
manifest.json
trace.jsonl
outputs/<case_id>.json
```

Không có `.env`, Team API key, source code, inputs, debug logs, hay evidence tự
tạo trong submission.

## Quy trình Git đề xuất

- Mỗi người tạo branch theo dạng `codex/<ten>-<feature>`.
- Chỉ sửa phần việc của mình; báo Quân trước khi đổi interface chung.
- Commit nhỏ, mô tả rõ chức năng và test đã chạy.
- Quân review/integrate lần lượt: Tuân -> Vũ/Điệp -> Hãn -> workflow cuối.
