# L3A Architecture Record

Tài liệu này mô tả các quyết định có thể kiểm chứng của workflow. Trace chỉ ghi sự kiện,
decision code và provenance; không ghi prompt hoặc chain-of-thought.

## 1. Luồng xử lý

```text
Input case
   │
   ▼
Coordinator ── task_assigned ──► Domain agents
                                    │
                                    ├─ Order/item agent ─┐
                                    ├─ Payment agent ────┤ MCP Evidence Gateway
                                    ├─ Shipment agent ───┤
                                    └─ Policy agent ─────┘
                                             │
                                      evidence + handoff
                                             ▼
                                      Verifier agent
                                             │
                                             ▼
                                  Schema-locked output + trace
```

Coordinator đọc topic như một giả thuyết để route, không coi lời khách hàng là ground
truth. Domain evidence phải xác nhận giả thuyết; policy chỉ được áp dụng sau bước xác
nhận. Nếu không đủ bằng chứng, workflow trả `insufficient_evidence`, refund bằng 0 và
không tự suy đoán dữ liệu.

## 2. Phân định trách nhiệm agent

| Actor | Input | Quyền gọi tool | Trách nhiệm | Handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Case input | Không gọi MCP | Kiểm tra input, route theo candidate issue, tổng hợp output | Task cho domain agents; nhận kết quả verifier |
| Order/item agent | `case_id`, `order_id` | `get_order`, `get_order_items` | Xác minh trạng thái order và entity liên quan | Evidence refs cho verifier |
| Payment agent | `case_id`, `order_id` | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | Xác minh capture, mismatch, duplicate và refund lifecycle | Evidence refs cho verifier |
| Shipment agent | `case_id`, `order_id` | `get_shipment_summary`, `get_sellers` | Xác minh giao trễ và actor chịu trách nhiệm | Evidence refs cho verifier |
| Policy agent | `case_id`, `policy_version` | `get_policy` | Chọn đúng rule đã được evidence xác nhận; lấy status, action, refund và responsible party | Policy decision cho verifier |
| Verifier agent | Tất cả handoff cùng case | Không gọi MCP | Kiểm tra chéo issue–party–money–action, conflict và confidence | Kết quả đã xác minh cho coordinator |

Quyền tool được khóa bằng `TOOLS_BY_ISSUE` và `TOOL_ACTORS`. Workflow không gọi tool
ngoài route của case; đặc biệt không gọi refund timeline cho case không có refund
lifecycle.

## 3. A2A protocol

Thông điệp quan sát được dùng các field của `trace-event-v1`: `case_id` là correlation
key; `actor` là bên gửi; `target` là bên nhận; `evidence_refs` là provenance. Mỗi actor
nhận đúng một task cho case và handoff đúng một lần sau khi các tool của actor hoàn tất.
Không có vòng lặp agent. Thứ tự lifecycle là:

```text
case_received → task_assigned → tool_result_consumed → handoff
              → policy_decided → verification_completed → case_finalized
```

## 4. Evidence lifecycle

1. Gateway discovery khóa danh sách tên tool hợp lệ.
2. Mọi call luôn truyền `case_id` từ case hiện tại.
3. MCP envelope được validate bằng `mcp-evidence-response-v1.schema.json`.
4. `evidence_ref` được giữ nguyên, không sinh mới hoặc sửa đổi.
5. Ngay khi dùng response, actor emit `tool_result_consumed` với tool và ref tương ứng.
6. Chỉ refs của case hiện tại được đưa vào handoff, claim assessment và final output.
7. Số tiền không được tính bằng cách cộng toàn bộ row nhiễu; lấy đúng `refund_brl` từ
   policy rule đã được domain evidence xác nhận.

## 5. Failure policy

| Failure | Retry | Xử lý |
| --- | --- | --- |
| Không kết nối được MCP | Không retry vô hạn | Dừng run và báo lỗi hạ tầng; không tạo output giả |
| Tool không tồn tại sau discovery | Không | Dừng ngay với lỗi rõ tên tool |
| MCP trả `is_error` | Không tự fallback | Surface nội dung lỗi; không dùng response làm evidence |
| Domain evidence không xác nhận claim | Không | `insufficient_evidence`, `needs_investigation`, refund 0 |
| Hai source mâu thuẫn | Không che giấu | Ghi `data_conflicts`, chọn source theo resolution code và giảm confidence |
| Policy rule thiếu/không hợp lệ | Không suy đoán | `insufficient_evidence` |

## 6. Verification invariants

- `case_id` của call, trace và output phải giống input.
- Mọi output ref phải đến từ MCP response đã consume của chính case.
- `primary_issue` phải được domain evidence xác nhận và có policy rule.
- `responsible_parties`, `case_status`, `resolution_actions` và số refund cùng đến từ
  một policy rule.
- Tổng `refund_lines.amount_brl` bằng `recommended_refund_brl`; refund 0 không có line.
- Seller ID từ policy được đưa vào affected entities khi seller chịu trách nhiệm.
- Conflict làm giảm confidence; thiếu evidence có confidence thấp.
- Output và từng trace event phải pass public JSON Schema trước khi ghi artifact.

## 7. Tính tái lập

- Runtime: Python >= 3.11; dependencies khóa theo ranges trong `pyproject.toml`.
- Workflow deterministic, không dùng model, random seed hoặc concurrency.
- Lệnh kiểm tra: `ruff check src tests`, `pytest -q`, `day09 validate-inputs`,
  `day09 run`, `day09 validate`.
- Team API key chỉ đọc từ `.env`; không xuất hiện trong output, trace hoặc package.
