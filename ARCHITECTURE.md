# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng từ `inputs/<case_id>.json` đến MCP, các agent, output và trace:

```text
Input → Coordinator → concurrent MCP collector
                              ↓
             Order/Payment ───┬─── Shipment/Seller (conditional)
                              └─── Policy/Resolution
                              ↓ join
                           Verifier → output builder → Output + Trace
```

LangGraph giữ state riêng cho từng case. Ba specialist dùng chung Qwen3 1.7B và được gọi
đồng thời với concurrency tối đa 3. Shipment/Seller được skip deterministic khi không có
claim giao hàng. Verifier chỉ được trả một nhiệm vụ bổ sung về đúng một specialist;
collector chỉ gọi tool mới được discovery và graph chặn vòng sửa thứ hai.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case, claims, discovered tool metadata | Xác định focus và tool cần dùng; không gọi MCP | Task riêng cho ba specialist |
| Order/Payment | Task và evidence tài chính | Order, item, payment và refund | Finding + evidence refs |
| Shipment/Seller | Task và evidence giao hàng | Timeline và seller/logistics responsibility; skip nếu không liên quan | Finding + evidence refs hoặc skipped |
| Policy/Resolution | Policy và fact MCP rút gọn | Quyền lợi, refund cap và action | Policy finding + evidence refs |
| Verifier | Tất cả domain findings và evidence rút gọn | Kiểm tra provenance, consistency, confidence | Decision hoặc một correction task có mục tiêu |

Tool ownership được tạo từ kết quả discovery. Coordinator và verifier chỉ đề xuất tên tool;
collector deterministic gọi đồng thời các tool độc lập sau khi kiểm tra tên và input schema.
Coordinator và verifier không trực tiếp gọi MCP.

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
`tool_result_consumed` ngay khi chuyển cho specialist sở hữu domain, rồi verifier chỉ được chọn ref
có trong store của case hiện tại. Output builder chỉ giữ ref verifier/claim đã chọn, loại
ref lạ và không tự map fallback theo topic. Entity ID cũng phải xuất hiện trong dữ liệu MCP;
claimed order ID trong customer message không tự động trở thành dữ liệu có thẩm quyền.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout/connection tạm thời | Tối đa 2 retry, exponential backoff | Dừng case nếu vẫn lỗi | Lỗi runtime, không tạo evidence |
| Not found/permanent tool error | Không | Dừng hoặc kết luận thiếu evidence nếu gateway trả envelope hợp lệ | Không tạo ref giả |
| Source conflict | Không | Verifier chọn nguồn hoặc để unresolved | `verification_completed`; conflict vào output |
| Invalid agent result | 1 structured repair | Dừng case nếu vẫn sai | Không ghi nội dung sai vào trace |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước finalize, output builder và contract validator kiểm tra:

- đúng case ID và đúng public output schema, không field ngoài schema;
- mọi evidence ref thuộc store của case và claim refs là tập con hợp lệ;
- entity list duy nhất, đúng độ dài và chỉ chứa ID có trong evidence;
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
| Coordinator | `qwen3:1.7b` | 1.7B |
| Order/Payment | `qwen3:1.7b` | 1.7B |
| Shipment/Seller | `qwen3:1.7b` | 1.7B |
| Policy/Resolution | `qwen3:1.7b` | 1.7B |
| Verifier | `qwen3:1.7b` | 1.7B |
| **Tổng theo vai trò** | | **8.5B** |

Concurrency giữa case là 1; concurrency specialist tối đa 3. Ollama dùng context 4096 và
cần được khởi động với `OLLAMA_NUM_PARALLEL=3` để thực thi request song song thực sự.
Model name, endpoint và timeout được cấu hình bằng `.env`.
Không ghi API key trong architecture, output hoặc trace. Kiểm tra bằng `pytest -q`,
`day09 validate-inputs`, `day09 run` (hoặc `--resume`), `day09 validate` và
`day09 package`. Mỗi case ghi trace vào file tạm; chỉ sau khi output validate thành công
mới append nguyên tử theo case vào `trace.jsonl`.

## 8. Repository directories

- `contracts/`: nguồn chuẩn chỉ đọc cho schema, registry và scoring policy.
- `inputs/`: payload case; customer message là claim, không phải ground truth.
- `outputs/`: một output JSON đã validate cho mỗi case.
- `tests/`: unit/graph/contract tests với fake MCP và fake model.
- `traces/`: observable JSONL lifecycle, không chứa dữ liệu suy luận riêng.
