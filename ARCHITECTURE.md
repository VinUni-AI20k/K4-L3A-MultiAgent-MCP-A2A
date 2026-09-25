# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
Input → Coordinator → Specialists → Policy → Verifier → Output
                         │              │
                         └── MCP ───────┴── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case JSON | route bounded tasks | specialist handoffs |
| Order/item | order id | order and item evidence | evidence refs |
| Payment | order id | payment evidence | evidence refs |
| Shipment | order id | shipment evidence | evidence refs |
| Policy | specialist refs | apply policy evidence | decision event |
| Verifier | draft output | schema/entity/evidence checks | validated output |

Tool permissions are narrow: order/item uses `get_order` and `get_order_items`,
payment uses `get_order_payments`, and shipment uses `get_shipment_summary`.

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

Mỗi handoff mang `case_id`, actor, target và evidence refs qua trace. Handoff chỉ đi
theo một chiều specialist → policy → verifier, nên không có vòng lặp. MCP timeout hoặc
lỗi response được bỏ qua với bounded retry ở caller; không tạo evidence giả.

## 4. Evidence lifecycle

Mô tả cách validate MCP response, lưu `evidence_ref`, map evidence vào claim/output và emit `tool_result_consumed`. Evidence không được tái sử dụng giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | Có giới hạn | needs_investigation | tool_result_consumed không phát |
| Not found | Không | needs_investigation | policy_decided/evidence_only |
| Source conflict | Không đoán | data_conflicts | verifier |
| Invalid specialist result | Không nhận | needs_investigation | verifier |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước finalize kiểm tra schema, case/entity scope, evidence refs thuộc chính case,
claim linkage, tổng tiền không âm, responsibility/action consistency và confidence 0..1.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và các giới hạn tài nguyên. Không ghi API key.
