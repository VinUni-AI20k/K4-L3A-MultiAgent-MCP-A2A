# L3A Architecture Record — Team Violet

## 0. Thông tin nhóm (Team Information)
- **Tên nhóm:** Violet
- **Thành viên:**
  1. Nguyễn Phát Thịnh (2A202602645) — Team Lead / Architecture / Coordinator & Verifier
  2. Lê Nguyễn Thái Dương (2A202602383) — Specialist: Order, Item & Shipment
  3. Nguyễn Minh Lương (2A202602618) — Specialist: Payment, Policy & Financial Resolution

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
Input → Coordinator → Specialists → Verifier → Output
                         │              │
                         └── MCP ───────┴── Trace
```

## 2. Agent ownership

| Actor | Người phụ trách | Input | Trách nhiệm | Quyền gọi Tool MCP | Output / Handoff |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Coordinator** | Nguyễn Phát Thịnh | Case input JSON (`customer_message`, `claims`) | Tiếp nhận case, phân tích sơ bộ, chia task cho các Specialist | Không gọi trực tiếp tool dữ liệu (chỉ discovery tools) | Handoff task tới Order, Shipment, Payment |
| **Order/Item** | Lê Nguyễn Thái Dương | `order_id`, `item_id` từ Coordinator | Kiểm tra trạng thái đơn hàng, thông tin sản phẩm, danh tính seller | `get_order`, `get_order_items`, `get_seller` | Kết quả đơn hàng, `order_ids`, `item_ids`, `seller_ids`, `evidence_refs` |
| **Shipment** | Lê Nguyễn Thái Dương | `order_id`, ngày mua hàng, thông tin giao hàng | Kiểm tra hành trình đơn, đối chiếu ngày giao hàng dự kiến vs thực tế, xác định lỗi trễ do seller hay logistics | `get_shipment`, `get_carrier_status` | Trạng thái vận chuyển, `shipment_ids`, xác định lỗi `late_delivery_*`, `evidence_refs` |
| **Payment** | Nguyễn Minh Lương | `order_id`, số tiền khách khiếu nại | Kiểm tra giao dịch, các phương thức thanh toán, phát hiện trùng lặp (`duplicate_charge`), lệch tiền, trạng thái hoàn tiền | `get_payment`, `get_refund_status` | Trạng thái thanh toán, `payment_references`, bằng chứng thanh toán |
| **Policy** | Nguyễn Minh Lương | Dữ liệu từ Order, Payment, Shipment & các claims | Tra cứu chính sách sàn, xác định tính hợp lệ của khiếu nại, tính toán số tiền hoàn (`BRL`), đề xuất actions | `get_policy`, `get_refund_rules` | `claim_assessments`, `financial_resolution`, `resolution_actions` |
| **Verifier** | Nguyễn Phát Thịnh | Toàn bộ kết quả từ các Specialist | Kiểm tra chéo (consistency), ràng buộc schema, tính `confidence`, phát hiện xung đột dữ liệu | Không gọi tool (chỉ thẩm định kết quả) | Output JSON hoàn chỉnh hợp lệ, emit `verification_completed` |

## 3. A2A protocol

Mô tả message envelope, correlation theo `case_id`, điều kiện handoff, timeout và cách tránh vòng lặp. Chỉ trace sự kiện/decision code quan sát được; không trace nội dung suy luận riêng.

## 4. Evidence lifecycle

Mô tả cách validate MCP response, lưu `evidence_ref`, map evidence vào claim/output và emit `tool_result_consumed`. Evidence không được tái sử dụng giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | TODO | TODO | TODO |
| Not found | TODO | TODO | TODO |
| Source conflict | TODO | TODO | TODO |
| Invalid specialist result | TODO | TODO | TODO |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, evidence ownership, claim linkage, money totals, responsibility/action consistency và confidence bounds.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và các giới hạn tài nguyên. Không ghi API key.