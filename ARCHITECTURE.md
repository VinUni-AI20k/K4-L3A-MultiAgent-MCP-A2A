# L3A Architecture Record

Documenting verified architectural decisions, agent boundaries, communication protocols, and invariant verification rules for the L3A Multi-Agent E-Commerce Investigation System.

## 1. System overview

The system processes dispute cases through an Agent-to-Agent (A2A) orchestration topology:

```text
[Input Case]
     │
     ▼
[Coordinator Agent] (case_received)
     │
     ├─► [task_assigned] ──► [Order/Item Agent] ───► MCP: get_order, get_order_items, get_sellers, get_product_context, get_customer_history
     ├─► [task_assigned] ──► [Payment Agent]    ───► MCP: get_order_payments, get_payment_timeline, get_refund_timeline
     └─► [task_assigned] ──► [Shipment Agent]   ───► MCP: get_shipment_summary
     │
     ◄── [handoff] ────────── (Domain Evidence Collections)
     │
     ▼
[Coordinator Agent] (Preliminary synthesis & issue detection)
     │
     ├─► [task_assigned] ──► [Policy Agent]    ───► MCP: get_policy
     │                                               │
     │   ◄── [policy_decided & handoff] ─────────────┘
     │
     ▼
[Coordinator Agent] (Drafts dispute output resolution)
     │
     ├─► [task_assigned] ──► [Verifier Agent]  ───► Audit 6 verification invariants
     │                                               │
     │   ◄── [verification_completed & handoff] ─────┘
     │
     ▼
[Coordinator Agent] (case_finalized) ──► Validated JSON Output
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff | Permitted MCP Tools |
| --- | --- | --- | --- | --- |
| `coordinator` | `case_id`, customer inquiry, claimed order ID | Điều phối luồng A2A, phân bổ nhiệm vụ, tổng hợp dữ liệu, finalization | Task assignments, draft resolution, final output | None (pure orchestrator) |
| `order-item-agent` | `order_id` | Truy vấn đơn hàng, mặt hàng, người bán, bối cảnh sản phẩm và lịch sử khách hàng | Trích xuất `order_ids`, `item_ids`, `seller_ids`, `customer_history` | `get_order`, `get_order_items`, `get_sellers`, `get_product_context`, `get_customer_history` |
| `payment-agent` | `order_id` | Khảo sát các giao dịch thanh toán, timeline thanh toán và timeline hoàn tiền | Trích xuất `payment_references`, timeline sự kiện hoàn tiền | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` |
| `shipment-agent` | `order_id` | Khảo sát tiến độ giao hàng, hạn chót vận chuyển, sự kiện giao trễ | Trích xuất `shipment_ids`, đối chiếu mốc thời gian giao nhận | `get_shipment_summary` |
| `policy-agent` | `policy_version`, preliminary issue | Đối chiếu quy chế sàn, xác định mức hoàn tiền và phân định trách nhiệm | Quyết định quy chế (`policy_decided`), `refund_brl`, trách nhiệm | `get_policy` |
| `verifier-agent` | Draft output resolution | Kiểm định 6 bất biến (invariants), chuẩn hóa số liệu và hiệu chuẩn confidence | Báo cáo kiểm định (`verification_completed`), verified output | None (read-only audit engine) |

## 3. A2A protocol

- **Message Envelope**: Mọi thông điệp và sự kiện được chuẩn hóa theo schema `day09-trace-event-v1` với các trường bắt buộc: `schema_version`, `event_id` (`evt_...`), `case_id`, `event_type`, `occurred_at`, `actor`.
- **Correlation**: Tất cả tương tác giữa các agent đều được liên kết bởi `case_id` thống nhất từ input đến finalization.
- **Handoff Conditions**:
  - Domain specialists hoàn tất truy vấn dữ liệu hoặc ghi nhận lỗi -> emit `handoff` về `coordinator` với thuộc tính `evidence_count`.
  - Policy Agent xác định xong quy chế áp dụng -> emit `policy_decided` và `handoff` về `coordinator`.
  - Verifier Agent hoàn tất thẩm tra 6 bất biến -> emit `verification_completed` và `handoff` về `coordinator`.
- **Loop Prevention & Timeout**: Luồng A2A hoàn toàn là đồ thị có hướng không chu trình (DAG). Mỗi agent chỉ thực thi đúng 1 lượt phân công theo pha xác định; không phát sinh hồi tiếp lặp.

## 4. Evidence lifecycle

- **Validation**: Mọi phản hồi từ MCP Evidence Gateway được kiểm tra hợp quy qua hợp đồng `mcp-evidence-response-v1.schema.json`.
- **Evidence Storage & Provenance**: Mỗi dữ liệu thu thập được cấp một `evidence_ref` duy nhất theo định dạng `^ev_[A-Za-z0-9_-]{20,96}$`. Mỗi lần trích xuất thành công đều kích hoạt phát sự kiện `tool_result_consumed` với `actor` là specialist tương ứng.
- **Evidence Isolation**: `evidence_ref` gắn liền với phiên của từng `case_id`, tuyệt đối không tái sử dụng qua các case khác nhau.
- **Mapping**: Mọi thẩm định khiếu nại (`claim_assessments`) đều được liên kết bằng các `evidence_refs` cụ thể thuộc phạm vi nghiệp vụ liên quan.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / Transport glitch | Retry 1 lần (backoff 300ms) | Trả về `None`, đánh dấu nguồn dữ liệu không khả dụng | `tool_result_consumed` với `status="failed"` |
| MCP Not found / ToolExecutionError | Không retry (fail-fast 0ms) | Ghi nhận bản ghi rỗng, chuyển sang thẩm tra các nguồn còn lại | `tool_result_consumed` với `status="not_found"` |
| Source conflict | Không retry | Phát hiện xung đột, đưa vào `data_conflicts` và chọn nguồn có thẩm quyền cao hơn | Ghi nhận vào mục `data_conflicts` của output |
| Invalid specialist result | Không retry | Verifier Agent tự động kích hoạt luật bảo toàn giá trị và hiệu chỉnh | `verification_completed` kèm chi tiết hiệu chỉnh |

## 6. Verification invariants

Trước khi hoàn tất hồ sơ (`case_finalized`), Verifier Agent bắt buộc thẩm định và thực thi 6 bất biến sau:

1. **Entity Scope & ID Ownership**: Toàn bộ `order_ids`, `item_ids`, `seller_ids`, `payment_references`, `shipment_ids` phải được khử trùng lặp, không chứa chuỗi rỗng và giới hạn tối đa 20 phần tử mỗi tập hợp.
2. **Evidence Ownership & Linkage**: Tập `evidence_refs` ở cấp độ case không được rỗng (tối đa 30). Tất cả `evidence_refs` trong từng `claim_assessment` phải là tập con hợp lệ của danh sách bằng chứng case.
3. **Claim Verdict Alignment**: Đánh giá khiếu nại phải nhất quán với `primary_issue`. Khiếu nại không có căn cứ (`unsupported_claim`) buộc phải có phán quyết `unsupported`. Khiếu nại yêu cầu hoàn tiền toàn phần (`requested_full_refund`) phải được phân loại thành `supported`, `partially_supported`, hoặc `unsupported` tùy theo bản chất sự cố.
4. **Financial Resolution & Conservation of Value**: Khi `case_status == "no_action"`, số tiền hoàn `recommended_refund_brl` bắt buộc là `0.0` và `refund_lines` phải rỗng `[]`. Khi có hoàn tiền, tổng số tiền các dòng `sum(refund_lines.amount_brl)` phải khớp chính xác với `recommended_refund_brl`.
5. **Responsibility & Root Cause Consistency**: Phân tích nguyên nhân gốc rễ phải chứa các mã nguyên nhân chuẩn hóa và xếp hạng thứ tự tăng dần (`rank: 1, 2, ...`). Các bên chịu trách nhiệm phải mang `party_type` hợp quy; riêng `party_type == "seller"` bắt buộc mang `party_id` thực tế của người bán trong case.
6. **Confidence Bounds & Operational Actions**: Chỉ số tin cậy `confidence` phải nằm nghiêm ngặt trong khoảng `[0.10, 1.00]`, được hiệu chuẩn phản ánh đúng độ chắc chắn của bằng chứng. Danh sách `resolution_actions` gồm các chuỗi hành động duy nhất, tối đa 8 mục, chiều dài mỗi mục không quá 80 ký tự.

## 7. Reproducibility

- **Runtime Environment**: Python 3.12, Windows x64.
- **Dependencies**: `httpx2`, `mcp`, `jsonschema`, `pydantic`.
- **Execution Architecture**: AsyncIO Event Loop với cấu hình xử lý theo lô (`batch_size=20`, `concurrency=3`).
- **Deterministic Run**: Quy tắc phân loại ngữ nghĩa, chính sách sàn và logic kiểm định bất biến hoàn toàn mang tính tất định (deterministic), không phụ thuộc vào nhiệt độ ngẫu nhiên của mô hình hay các điều kiện ngoại lai.
- **Packaging Command**: `day09 package --output dist/submission.zip`.
