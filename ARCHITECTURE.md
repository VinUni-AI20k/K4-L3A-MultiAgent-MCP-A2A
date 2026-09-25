# L3A Architecture Record

Tài liệu mô tả kiến trúc có thể kiểm chứng của bài nộp. Không chứa API key, prompt bí
mật hoặc chain-of-thought.

## 1. System overview

```text
inputs/<case_id>.json
        │
        ▼
   Coordinator ── task_assigned ──┬── Order/item agent ── MCP
        │                         ├── Payment agent ───── MCP
        │                         ├── Shipment agent ──── MCP
        │                         └── Policy agent ────── MCP
        │                                  │
        └──────── validated handoffs ◄─────┘
                           │
                           ▼
              Deterministic candidate output
                           │
                           ▼
             Qwen3 8B verifier + invariants
                           │
                           ▼
                schema-validated output
```

`cli.py` đọc tuần tự 100 case. Mỗi case được xử lý trong cùng một MCP session, nhưng
mọi state evidence trong `workflow.py` chỉ tồn tại trong phạm vi lời gọi `solve_case`.
CLI validate output ngay trước khi ghi file theo phương thức temporary-file replace.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool được phép gọi | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Case, claims, policy version | Chọn tool tối thiểu theo issue, giao việc, tổng hợp | Không gọi trực tiếp ngoài kế hoạch | Candidate output |
| Order/item agent | `case_id`, `order_id` | Xác minh order, item và seller liên quan | `get_order`, `get_order_items`, `get_sellers` | Order/entity facts + evidence refs |
| Payment agent | `case_id`, `order_id` | Xác minh payment, duplicate, mismatch và refund lifecycle | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | Payment/refund facts + evidence refs |
| Shipment agent | `case_id`, `order_id` | Xác minh giao trễ và actor chịu trách nhiệm | `get_shipment_summary` | Shipment facts + evidence ref |
| Policy agent | `case_id`, `policy_version` | Lấy rule, status, action, refund và responsible party | `get_policy` | Policy decision + evidence ref |
| Verifier | Case, candidate, evidence data | So sánh issue bằng model 8B và chạy invariant xác định | Không gọi MCP | `verification_completed` |

Tool plan nằm trong `ISSUE_TOOLS`. Mỗi actor chỉ nhận tool thuộc domain của mình.

## 3. A2A protocol

Handoff nội bộ dùng `EvidenceRecord`:

```text
tool_name, actor, evidence_ref, domain, data, warnings
```

`case_id` là correlation key duy nhất và luôn được truyền trực tiếp vào mọi MCP call.
Lifecycle quan sát được:

```text
case_received → task_assigned → tool_result_consumed → handoff
              → policy_decided → verification_completed → case_finalized
```

Workflow không có vòng lặp agent tự do. Tool plan hữu hạn theo issue, mỗi tool được gọi
tối đa một lần trong một case, vì vậy không thể phát sinh vòng lặp vô hạn.

## 4. Evidence lifecycle

1. Coordinator lấy issue được khách hàng claim nhưng chưa coi đó là ground truth.
2. Tool plan yêu cầu evidence có thẩm quyền ở đúng domain.
3. `EvidenceGateway` validate mọi response bằng public MCP evidence schema.
4. Specialist kiểm tra lifecycle fact cụ thể trước khi xác nhận issue.
5. `evidence_ref` được giữ nguyên, không sửa hoặc tự sinh.
6. Evidence được handoff về coordinator và map vào output/claim assessment.
7. Policy chỉ được áp dụng khi domain evidence thật sự hỗ trợ issue.
8. State evidence bị hủy khi `solve_case` kết thúc, tránh dùng chéo case.

Model chỉ nhận facts tối thiểu đã loại bỏ message, ID, evidence ref và timestamp để kiểm
tra issue. Model không được tạo hoặc sửa evidence refs, số tiền hay action. Candidate
cuối vẫn bị kiểm tra bằng invariant xác định.

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout/connection tạm thời | Tối đa 2 retry, exponential backoff | Dừng case nếu vẫn lỗi | Lỗi runtime, không tạo evidence |
| Not found/permanent tool error | Không | Dừng hoặc kết luận thiếu evidence nếu gateway trả envelope hợp lệ | Không tạo ref giả |
| Source conflict | Không | Verifier chọn nguồn hoặc để unresolved | `verification_completed`; conflict vào output |
| Invalid agent result | 1 structured repair | Dừng case nếu vẫn sai | Không ghi nội dung sai vào trace |

Model mặc định là `qwen/qwen3-8b` qua OpenRouter. Model có 8,2B tham số và đáp ứng
yêu cầu dưới 10B. Allowlist trong `model_client.py` chỉ cho phép:

- `qwen/qwen3-8b`;
- `qwen/qwen3-8b:free`.

Cấu hình:

```dotenv
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=qwen/qwen3-8b
```

Request dùng temperature 0, JSON mode, giới hạn 500 output token và tắt reasoning
output. Nếu model lỗi hoặc trả JSON sai, deterministic candidate vẫn được giữ nhưng
confidence bị hạ và trace ghi `MODEL_UNAVAILABLE`. Nếu model bất đồng, trace ghi
`MODEL_DISAGREED` và confidence cũng bị giới hạn.

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
