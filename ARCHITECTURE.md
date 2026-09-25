# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
Input → Coordinator → Order/Payment → Shipment/Seller → Policy/Resolution → Verifier
                              │                │                 │              │
                              └──────────────── MCP ──────────────┴──────────────┤
                                                                                ↓
                                                              Output builder → Output + Trace
```

LangGraph giữ state riêng cho từng case. Các node chạy tuần tự để tránh nhiều model
tranh chấp VRAM. Verifier được phép trả một nhiệm vụ về đúng một specialist và graph
chặn vòng sửa thứ hai.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case, claims, discovered tool metadata | Xác định focus, giao việc; không gọi MCP | Ba task có correlation theo case |
| Order/payment | Task, order ID | Order, item, payment và refund evidence | Structured finding + evidence refs |
| Shipment/seller | Task, order ID | Shipment timeline và seller responsibility | Structured finding + evidence refs |
| Policy/resolution | Task, policy version | Policy, quyền lợi, refund và action | Policy decision + structured finding |
| Verifier | Tất cả finding và evidence | Kiểm tra provenance, consistency, confidence | Decision hoặc tối đa một correction task |

Tool ownership được tạo từ kết quả discovery:

- order/payment: `get_order`, `get_order_items`, `get_order_payments`,
  `get_payment_timeline`, `get_refund_timeline`;
- shipment/seller: `get_shipment_summary`, `get_sellers`;
- policy/resolution: `get_policy`;
- coordinator và verifier không gọi MCP.

Tool chỉ được gọi nếu tên thật sự xuất hiện trong discovery và payload pass input
schema do MCP công bố.

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

Message nội bộ gồm `case_id`, `task_id`, `sender`, `recipient`, `kind`, `payload`,
`evidence_refs` và `retry_count`. Mỗi handoff giữ nguyên `case_id`; message không được
đưa vào public output. Graph có cạnh cố định và `correction_count <= 1`, vì vậy không
thể hình thành vòng lặp vô hạn. Trace chỉ chứa event, actor, decision code và evidence
observable; không chứa prompt hay chain-of-thought.

## 4. Evidence lifecycle

Mỗi `solve_case` tạo evidence store mới. MCP envelope được validate trước khi lưu nguyên
`evidence_ref`, `result_hash`, domain và data. Evidence được gắn actor/tool, emit
`tool_result_consumed` ngay khi chuyển cho specialist, rồi verifier chỉ được chọn ref
có trong store của case hiện tại. Output builder loại ref lạ và map fallback theo loại
claim từ các tool đã gọi; không tạo hoặc sửa ref.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout/connection tạm thời | Tối đa 2 retry, exponential backoff | Dừng case nếu vẫn lỗi | Lỗi runtime, không tạo evidence |
| Not found/permanent tool error | Không | Dừng hoặc kết luận thiếu evidence nếu gateway trả envelope hợp lệ | Không tạo ref giả |
| Source conflict | Không | Verifier chọn nguồn hoặc để unresolved | `verification_completed`; conflict vào output |
| Invalid specialist result | 1 structured repair | Dừng case nếu vẫn sai | Không ghi nội dung sai vào trace |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước finalize, output builder và contract validator kiểm tra:

- đúng case ID và đúng public output schema, không field ngoài schema;
- mọi evidence ref thuộc store của case và claim refs là tập con hợp lệ;
- entity list duy nhất, đúng độ dài và chứa claimed order ID;
- confidence nằm trong `[0, 1]`;
- refund không âm và tổng `refund_lines` bằng `recommended_refund_brl`;
- refund/action kéo theo `action_required`;
- cause rank liên tục, party/action không trùng;
- đủ lifecycle trace và event đúng schema.

## 7. Reproducibility

Mặc định dùng Ollama OpenAI-compatible tại `http://127.0.0.1:11434/v1`, temperature
0 và structured JSON output. Model assignment:

| Actor | Model | Budget |
| --- | --- | ---: |
| Coordinator | `qwen3:0.6b` | 0.6B |
| Ba specialist | `qwen3:1.7b` | 5.1B |
| Verifier | `qwen3:4b` | 4.0B |
| **Tổng theo vai trò** | | **9.7B** |

Concurrency model là 1; model name, endpoint và timeout được cấu hình bằng `.env`.
Không ghi API key trong architecture, output hoặc trace. Kiểm tra bằng `pytest -q`,
`day09 validate-inputs`, `day09 run`, `day09 validate` và `day09 package`.

## 8. Repository directories

- `contracts/`: nguồn chuẩn chỉ đọc cho schema, registry và scoring policy.
- `inputs/`: payload case; customer message là claim, không phải ground truth.
- `outputs/`: một output JSON đã validate cho mỗi case.
- `tests/`: unit/graph/contract tests với fake MCP và fake model.
- `traces/`: observable JSONL lifecycle, không chứa dữ liệu suy luận riêng.
