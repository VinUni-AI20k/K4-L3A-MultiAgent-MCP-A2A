# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Mỗi case được coordinator phân công cho bốn specialist. Specialist chỉ thu thập
evidence thuộc domain của mình. Evidence được temporal-scoping theo timeline của
order trước khi Qwen tổng hợp; verifier Python áp dụng invariant xác định và policy
MCP trước khi tạo output.

```text
Input → Coordinator → Specialists → Verifier → Output
                         │              │
                         └── MCP ───────┴── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Input case | Phân công, correlation và finalize | Task cho specialist; output cuối |
| Order/item | `case_id`, `order_id` | Gọi `get_order`, `get_order_items`, `get_sellers` | Order, item và seller evidence cho verifier |
| Payment | `case_id`, `order_id` | Gọi payment/refund tools | Payment và refund lifecycle evidence |
| Shipment | `case_id`, `order_id` | Gọi `get_shipment_summary` | Delivery timeline và actor evidence |
| Policy | `case_id`, `policy_version` | Gọi `get_policy`, chọn remedy sau classification | Status, action, refund và responsible party |
| Verifier | Specialist handoffs | Temporal scope, LLM synthesis, invariant checks, schema consistency | Assessment đã kiểm chứng cho coordinator |

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

Message được correlation bằng `case_id`; mọi MCP call bắt buộc nhận cùng ID. Luồng
một chiều `coordinator → specialist → verifier → coordinator`, không có handoff quay
lại nên không tạo vòng lặp. Mỗi specialist handoff sau khi đã gọi xong domain tools.
MCP dùng timeout connect 30 giây/request 300 giây; OpenRouter dùng request timeout
180 giây. Trace chỉ lưu lifecycle, actor, decision code và evidence refs.

## 4. Evidence lifecycle

`EvidenceGateway` validate mọi response bằng public evidence schema. Mỗi response
được giữ nguyên `evidence_ref` và emit `tool_result_consumed` ngay khi specialist sử
dụng. Verifier chỉ đưa refs nhận trong case hiện tại vào claim/output. Policy evidence
chọn remedy nhưng không được dùng như bằng chứng rằng issue đã xảy ra.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout/network | Tối đa 6 session | Backoff 5–20 giây; dừng case nếu hết retry | `EVIDENCE_UNAVAILABLE` khi tool riêng lẻ lỗi |
| Not found | Không đoán dữ liệu | Handoff evidence còn lại; verifier giảm khả năng kết luận | `EVIDENCE_UNAVAILABLE` |
| Source conflict | Không | Chọn record đúng transaction window và invariant timeline | `VERIFIER_CORRECTED_LLM` |
| Invalid LLM result | Không cho phép đi thẳng output | Deterministic evidence classifier | `DETERMINISTIC_FALLBACK` |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

- `case_id` output phải bằng case đang xử lý; ID entity chỉ lấy từ MCP data.
- Evidence refs chỉ lấy từ response đã validate trong case hiện tại.
- Record payment phải gần purchase/approval; record shipment phải khớp actual vs estimated.
- Split payment được xác nhận trước duplicate charge bằng tổng item + freight.
- Status, action, refund và responsible party luôn lấy từ policy rule của issue đã xác minh.
- Tổng `refund_lines` bằng `recommended_refund_brl`; confidence luôn trong `[0, 1]`.
- Output và từng trace event phải pass JSON Schema trước khi ghi.

## 7. Reproducibility

Model mặc định là `qwen/qwen3-8b` qua OpenRouter, temperature `0.1`, seed `9`,
reasoning ẩn tắt và JSON mode bật. Runner giới hạn hai case đồng thời, mỗi case có
MCP session riêng. Dependencies được pin theo `pyproject.toml`. Lệnh tái tạo:

```powershell
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Có thể tiếp tục một run bị gián đoạn bằng `day09 run --resume`. API key chỉ nằm trong
`.env` đã được Git ignore và không xuất hiện trong trace/submission.
