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

## 5. Model policy

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

## 6. Failure policy

| Failure | Retry? | Fallback | Trace/behavior |
| --- | --- | --- | --- |
| MCP timeout/tool error | Không retry tự động trong bản hiện tại | Dừng run để không tạo output thiếu provenance | CLI báo lỗi, không finalize case |
| Entity không tồn tại | Không | Dừng hoặc `insufficient_evidence` nếu response hợp lệ nhưng không hỗ trợ claim | Không tạo dữ liệu giả |
| Source conflict | Không tự chọn bằng tổng/cộng cơ học | Ưu tiên lifecycle event và policy | Candidate dựa trên nguồn có thẩm quyền |
| Model timeout/HTTP/JSON lỗi | Không | Giữ candidate đã kiểm chứng, hạ confidence | `MODEL_UNAVAILABLE` |
| Model bất đồng | Không | Giữ rule/evidence decision, hạ confidence | `MODEL_DISAGREED` |
| Invalid final output | Không | Từ chối ghi file | `VERIFICATION_FAILED` hoặc contract error |

Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 7. Verification invariants

Trước khi finalize, workflow kiểm tra:

- output `case_id` trùng input;
- evidence refs là danh sách duy nhất;
- action không trùng;
- tổng refund lines bằng `recommended_refund_brl` sau khi chuẩn hóa hai chữ số;
- issue phải thuộc public enum;
- model phải thuộc allowlist dưới 10B;
- model confidence phải là số trong `[0, 1]`;
- model không có quyền thay đổi candidate;
- policy chỉ được dùng sau khi domain evidence xác nhận issue.

Sau đó `Contracts.validate_output` kiểm tra toàn bộ JSON Schema, enum, pattern, giới
hạn số phần tử và additional properties.

## 8. Reproducibility

- Python: từ 3.11; CI dùng 3.11.
- Dependency: khai báo và giới hạn version trong `pyproject.toml`.
- Model: `qwen/qwen3-8b`, temperature 0, max tokens 500.
- Concurrency: tuần tự, một case tại một thời điểm.
- Random seed: không sử dụng; trace event ID dùng random an toàn và không ảnh hưởng output.
- Input order: theo `case-set.json`.
- Chạy: `python -m student_agent.cli run`.
- Tiếp tục batch gián đoạn: `python -m student_agent.cli run --resume`.
- Validate: `python -m student_agent.cli validate`.
- Đóng gói: `python -m student_agent.cli package --output dist/submission.zip`.

Không ghi API key vào source, trace, output hoặc tài liệu.
