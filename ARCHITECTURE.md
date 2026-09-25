# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng xử lý từ input case tới các agent chuyên môn (specialists), tổng hợp qua policy và được xác thực qua verifier để ra output cuối cùng.

```text
           [ Coordinator / Router ]
                      | (Handoff)
        +-------------+-------------+
        |             |             |
[Order/Item Agent] [Payment Agent] [Shipment Agent]
        |             |             |
        +-------------+-------------+
                      | (MCP Evidence Collector)
                      v
               [ Policy Agent ]
                      |
                      v
               [ Verifier Agent ]
                      | (Validated Output)
                      v
                 [END OUTPUT]
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff | Tool Permissions |
| --- | --- | --- | --- | --- |
| Coordinator / Router | `customer_message`, `case_id` | Phân tích khiếu nại, router luồng thu thập chứng cứ, điều hướng handoff | Lệnh điều động (Handoff) các Specialists | `get_customer_history` |
| Order/Item Agent | Handoff từ Coordinator | Lấy thông tin chi tiết về đơn hàng, sản phẩm, người bán liên quan | Order data, Item data (kèm `evidence_ref`) | `get_order`, `get_order_items`, `get_product_context`, `get_sellers` |
| Payment Agent | Handoff từ Coordinator | Kiểm tra các thanh toán, lịch sử giao dịch và hoàn tiền | Payment data (kèm `evidence_ref`) | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` |
| Shipment Agent | Handoff từ Coordinator | Theo dõi vận chuyển, ngày giao hàng dự kiến và thực tế | Shipment data (kèm `evidence_ref`) | `get_shipment_summary` |
| Policy Agent | Evidence từ các Specialists | Đối chiếu dữ liệu chứng cứ với chính sách hệ thống để đưa ra kết luận (Claim, Fault, Action) | Draft Resolution | `get_policy` |
| Verifier Agent | Draft Resolution | Xác thực ngặt nghèo JSON output với schema (l3a-output-v2), kiểm tra evidence ownership và money totals | Validated Output | *Không có* |

## 3. A2A protocol

- **Message Envelope:** Các agent giao tiếp qua State Object nội bộ (Pydantic models) mang case_id, customer_message và mảng evidence.
- **Handoff:** Coordinator gọi các Agent chuyên trách chạy song song hoặc tuần tự. Specialist gom evidence rồi pass cho Policy Agent.
- **Bảo mật Schema:** Áp dụng schema JSON-RPC strict, cấu trúc Pydantic được truyền trực tiếp vào structured output của Gemini để ép kiểu trả về.

## 4. Evidence lifecycle

- Mỗi MCP response thành công phải được lưu lại `evidence_ref`.
- Mỗi evidence_ref được ánh xạ thẳng vào output (`evidence_refs` field) hoặc claim assessments.
- `trace.emit(event_type="tool_result_consumed", evidence_refs=[...])` được phát lập tức sau khi có evidence. Evidence được check chéo id để không xài của case khác.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | 3 lần, exponential backoff | Return Error Message | tool_timeout |
| Not found | Không | Báo cáo là không tồn tại dữ liệu (không hallucinate) | not_found |
| Invalid specialist result | 1 lần yêu cầu parse lại | Dùng dữ liệu rỗng (cần bypass lỗi để đi tiếp) | invalid_format |

## 6. Verification invariants

- Schema Validation: Mọi output phải parse được bằng class `L3AOutput` Pydantic.
- Entity Scope: Không sử dụng evidence_ref nằm ngoài order_ids hiện hành.
- Bắt buộc kiểm tra tiền tệ (BRL) > 0 và `refund_lines` (nếu có).

## 7. Reproducibility

- Model: Gemini-1.5-Pro / Flash.
- Framework: Pure Python Async State-Machine + Pydantic.
- Nhiệt độ LLM (Temperature): 0.0 để tối đa reproducibility.
