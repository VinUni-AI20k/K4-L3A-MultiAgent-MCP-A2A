# L3A Architecture Record — Team LANGXIMI

Tài liệu thiết kế kiến trúc hệ thống Multi-Agent điều tra khiếu nại thương mại điện tử (K4-L3A Multi-Agent MCP + A2A).

## 1. System overview

Quy trình xử lý tuần tự từ case input đến các specialist agent, MCP Gateway, Verifier và tạo ra Output + Trace:

```text
[inputs/<case_id>.json]
         │
         ▼
 ┌───────────────┐  task_assigned   ┌───────────────────────┐
 │  Coordinator  ├─────────────────►│   order_specialist    │──┐
 └───────┬───────┘                  └───────────┬───────────┘  │
         │                                      │ call/consume │
         │                          ┌───────────▼───────────┐  │
         │                          │  MCP Evidence Gateway │  │
         │                          └───────────┬───────────┘  │
         │                                      │              │
         │                          ┌───────────▼───────────┐  │
         │  handoff                 │   payment_specialist  │◄─┘
         │                          └───────────┬───────────┘
         │                                      │ handoff
         │                          ┌───────────▼───────────┐
         │                          │  shipment_specialist  │
         │                          └───────────┬───────────┘
         │                                      │ handoff
         │                          ┌───────────▼───────────┐
         │                          │   policy_specialist   │
         │                          └───────────┬───────────┘
         │                                      │ policy_decided + handoff
         │                                      ▼
         │                          ┌───────────────────────┐
         │◄─────────────────────────┤       verifier        │
         ▼   verification_completed └───────────────────────┘
[outputs/<case_id>.json] & [traces/trace.jsonl]
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | MCP Tools được gọi | Output/handoff |
| :--- | :--- | :--- | :--- | :--- |
| **Coordinator** | `case` object (`case_id`, `customer_request`) | Tiếp nhận case, dispatch tác vụ điều tra cho các specialist | Không gọi MCP trực tiếp | Giao tác vụ qua `task_assigned` cho `order_specialist` |
| **Order Specialist** | `claimed_order_id`, `case_id` | Xác minh sự tồn tại của đơn hàng, trạng thái đơn, danh sách mặt hàng, người bán | `get_order`, `get_order_items`, `get_sellers` | Dữ liệu đơn hàng, seller_id, items; `handoff` sang `payment_specialist` |
| **Payment Specialist** | `claimed_order_id`, `case_id` | Đối soát các khoản thanh toán, phương thức, lịch sử thanh toán và trạng thái hoàn tiền | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | Dữ liệu thanh toán, tổng tiền đã trả, trạng thái refund; `handoff` sang `shipment_specialist` |
| **Shipment Specialist** | `claimed_order_id`, `case_id` | Xác minh timeline vận chuyển: hạn giao hàng seller, ngày giao carrier, ngày giao khách hàng | `get_shipment_summary` | Timeline vận chuyển, phát hiện trễ do seller hay đơn vị vận chuyển; `handoff` sang `policy_specialist` |
| **Policy Specialist** | Dữ liệu tổng hợp từ các specialist + `policy_version` | Đối chiếu quy định chính sách với khiếu nại khách hàng, xác định `primary_issue`, lỗi thuộc bên nào, số tiền hoàn | `get_policy` | Đưa ra `policy_decided`, bảng đánh giá claim, `financial_resolution`; `handoff` sang `verifier` |
| **Verifier** | Preliminary output, evidence refs, entities | Kiểm tra các bất biến (invariants), schema, tính nhất quán tài chính và audit evidence | Không gọi MCP | Phát sự kiện `verification_completed`, chuyển kết quả hoàn chỉnh cho Coordinator finalize |

## 3. A2A protocol

- **Message Envelope & Correlation**: Toàn bộ trao đổi giữa các Agent đều gắn chặt với `case_id`. Mọi sự kiện được ghi nhận tuần tự qua `TraceWriter.emit` vào `traces/trace.jsonl` theo chuẩn `day09-trace-event-v1`.
- **Handoff Chain**: Quy trình chuyển giao đơn hướng xác định (Directed Acyclic Flow):
  `Coordinator` → `order_specialist` → `payment_specialist` → `shipment_specialist` → `policy_specialist` → `verifier` → `Coordinator`.
- **Chống lặp (Loop Prevention)**: Mỗi chuyên viên chỉ thực hiện lượt phân tích một lần duy nhất cho mỗi case, không có cơ chế gọi vòng ngược.
- **Trace Observability**: Chỉ ghi các mã quyết định (`decision_code`), tên công cụ (`tool_name`), `evidence_refs`, và các actor tương tác; tuyệt đối không đưa prompt nội bộ hoặc chuỗi suy luận (chain-of-thought) vào trace.

## 4. Evidence lifecycle

1. **Discovery & Validation**: Trước khi gọi, Agent kiểm tra sự tồn tại của công cụ trên Gateway. Phản hồi từ MCP Gateway được bọc trong envelope `day09-mcp-evidence-v1` và validate tự động qua JSON Schema.
2. **Provenance & Audit Tracking**:
   - Mỗi phản hồi chứa `evidence_ref` hợp lệ dạng `ev_[A-Za-z0-9_-]{20,96}`.
   - Ngay khi nhận được evidence, Agent phát sinh sự kiện `tool_result_consumed` với chính `evidence_ref` đó.
3. **Evidence Mapping**:
   - `evidence_ref` được lưu vào tập hợp `collected_evidence_refs` riêng biệt của từng case.
   - Được gắn vào trường `evidence_refs` cấp cao của output và từng mục trong `claim_assessments`.
   - Tuyệt đối không tái sử dụng `evidence_ref` giữa các case khác nhau (chống cross-case contamination).

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| :--- | :--- | :--- | :--- |
| **MCP timeout** | Retry tối đa 2 lần với backoff 500ms | Bỏ qua tool, đánh dấu trường dữ liệu là `unknown` | `tool_call_failed` / `TIMEOUT` |
| **Entity Not Found** | Không retry (idempotent 404) | Đặt dữ liệu rỗng `{}`, chuyển sang nhận định `unsupported_claim` | `tool_result_consumed` với ref rỗng |
| **Source Conflict** | Không retry | Ưu tiên dữ liệu từ MCP authoritative gateway so với lời khai của khách | Ghi nhận vào `data_conflicts` nếu có mâu thuẫn |
| **Invalid Specialist Result** | Không retry | Verifier tự động áp dụng chính sách an toàn: `no_action`, refund = 0.0 | `verification_completed` / `FALLBACK_SAFE` |

## 6. Verification invariants

Trước khi xuất file `outputs/<case_id>.json`, Verifier kiểm tra 7 điều kiện bất biến:
1. **Schema Compliance**: Đạt 100% JSON schema `day09-l3a-output-v2.schema.json`.
2. **Entity Scope**: Các entity IDs (`order_ids`, `item_ids`, `seller_ids`, `payment_references`, `shipment_ids`) được trích xuất trực tiếp từ evidence thật của case hiện tại.
3. **Evidence Ownership**: Toàn bộ `evidence_refs` trong output đều bắt nguồn từ các lượt gọi thành công của chính case đó.
4. **Claim Linkage**: Mọi `claim_id` từ `customer_request.claims` đều có bản ghi đánh giá tương ứng trong `claim_assessments`.
5. **Money Totals**: `recommended_refund_brl` luôn bằng chính xác tổng `amount_brl` của tất cả các dòng trong `refund_lines`.
6. **Status & Action Consistency**:
   - Nếu `case_status == "no_action"`, thì `recommended_refund_brl == 0.0` và `refund_lines` rỗng.
   - Nếu `recommended_refund_brl > 0.0`, thì `case_status` phải là `action_required`.
   - `resolution_actions` không chứa phần tử trùng lặp.
   - Nếu bên chịu trách nhiệm là `seller`, `party_id` phải khớp với `seller_id` của đơn hàng.
7. **Confidence Bounds**: `confidence` thuộc đoạn số thực `[0.0, 1.0]`.

## 7. Reproducibility

- **Runtime**: Python 3.11+, MCP SDK 2.x, HTTPX2 2.x, JSONSchema Draft 2020-12.
- **Cấu hình**: Thông tin kết nối MCP qua `.env` (`COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`).
- **Deterministic**: Quá trình phân tích tuân thủ luật suy luận nghiệp vụ tất định (deterministic business policy engine), đảm bảo kết quả nhất quán 100% giữa các lần chạy.
- **Quy trình thực thi**:
  1. `day09 validate-inputs`
  2. `day09 mcp-tools`
  3. `day09 run`
  4. `day09 validate`
  5. `day09 package --output dist/submission.zip`
