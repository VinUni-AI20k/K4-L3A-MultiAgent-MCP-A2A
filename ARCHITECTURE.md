# L3A Architecture Record

Tài liệu này mô tả quyết định có thể kiểm chứng của workflow được cài trong
`src/student_agent/workflow.py`. JSON Schema trong `contracts/schemas/` là contract
bắt buộc; workflow không được tự thêm field ngoài schema.

## 1. System overview

Workflow dùng Python async state-machine thuần, không phụ thuộc framework LLM.
Mỗi case đi qua một DAG cố định; các specialist stage được thực thi tuần tự để
reconnect không hủy request của agent khác. Các case cũng được CLI xử lý tuần tự.

```text
inputs/<case_id>.json
        │
        ▼
Coordinator / Router ── task_assigned
        │
        ├──► Order Agent ──┐
        ├──► Payment Agent ─────┼── Handoff + evidence registry
        └──► Shipment Agent ────┘
                                  │
                                  ▼
                            Policy Agent
                                  │  policy_decided
                                  ▼
                            Verifier Agent
                                  │  verified output
                                  ▼
                 outputs/<case_id>.json + trace.jsonl
```

Luồng thực thi:

1. CLI kiểm tra tool inventory, phát `case_received`, rồi gọi `solve_case`.
2. Coordinator kiểm tra toàn bộ claim để biết có cần gọi
   `get_refund_timeline` hay không; topic chỉ định tuyến, không phải ground truth.
3. Ba specialist thu thập evidence theo quyền tool riêng. Mỗi tool result hợp lệ
   được đăng ký vào evidence registry, phát `tool_result_consumed`, rồi handoff
   artifact đã hoàn tất tới Policy Agent.
4. Policy Agent đối chiếu order, payment timeline, refund timeline, shipment và
   policy để chọn `primary_issue`, status, refund, responsible parties và action.
5. Verifier kiểm tra invariant nghiệp vụ rồi validate output bằng
   `l3a-output-v2.schema.json`.
6. CLI ghi output atomically, phát `case_finalized`, rồi mới đóng gói submission.

### Scoring policy guard

Policy Agent nạp `contracts/scoring/scoring-policy-v2.json`, kiểm tra variant
`l3a`, hard gates và required workflow events trước khi quyết định. Version được
ghi trong `policy_decided.attributes`. File scoring quản lý governance/lifecycle;
`get_policy` từ MCP vẫn là nguồn business rule cho status, refund, action và
responsible party. `validate_artifacts` chặn package nếu lifecycle thiếu, sai
thứ tự, hoặc output/claim evidence không xuất hiện trong `tool_result_consumed`.

## 2. Agent ownership và tool permissions

| Actor | Input | Trách nhiệm | Tool được phép | Handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Input case, tool inventory | Validate input, route và join specialist; không quyết định business truth | Không có evidence tool; chỉ dùng `list_tools` ở preflight | Giao task cho 3 specialist, Policy, Verifier |
| Order Agent | `claimed_order_id` | Resolve order, item, seller và phát hiện row trùng ID | `get_order`, `get_order_items`, `get_sellers` | Order/item/seller evidence cho Policy |
| Payment Agent | Order đã resolve | Đối soát payment row, capture và refund lifecycle | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | Payment/refund evidence cho Policy |
| Shipment Agent | Order đã resolve | Đánh giá status, delivery timestamp, seller handoff limit | `get_shipment_summary` | Shipment evidence cho Policy |
| Policy Agent | Evidence registry | Phân loại issue và áp dụng `get_policy` | `get_policy` | Decision + toàn bộ evidence refs cho Verifier |
| Verifier Agent | Candidate output, registry | Kiểm tra scope, linkage, money, consistency và public schema | Không gọi MCP | Output đã validate cho Coordinator |

`get_customer_history` và `get_product_context` không thuộc L3A workflow này.
Coordinator chỉ gọi tool đã xuất hiện trong `list_tools`; thiếu tool bắt buộc thì
abort, không đoán tên tool. Một case dùng tối đa 7 core calls, thêm 1 refund call
khi claim route là `refund_pending` hoặc `refund_failed`.

## 3. A2A protocol

Internal handoff là dataclass `Handoff`, không phải public output field:

```text
case_id, sender, receiver, task, evidence_refs
```

- `case_id` là correlation key; mọi tool call và trace event phải mang đúng key.
- Raw data không được ghi vào trace. Agent trao evidence qua scoped registry;
  `evidence_refs` trong handoff chỉ là danh sách reference trong registry.
- Coordinator phát `task_assigned` tới ba specialist và chạy các stage tuần tự.
  Sau khi consume tool result, từng specialist handoff artifact của mình tới Policy
  Agent; vì vậy trace bảo đảm `tool_result_consumed` đứng trước `handoff`.
- Policy handoff decision tới Verifier. Verifier không gọi lại specialist, nên
  không có vòng lặp hoặc delegation không giới hạn.
- Handoff là in-process nên không có network timeout riêng. Timeout áp dụng ở
  MCP transport. DAG không có cycle và mỗi tool chỉ được gọi tối đa một lần/case.

Các event public được dùng đúng mục đích:

| Event | Actor → target | Ý nghĩa |
| --- | --- | --- |
| `task_assigned` | coordinator → agent | Agent nhận trách nhiệm và decision code |
| `tool_result_consumed` | specialist → tool | Evidence envelope đã qua validation và được dùng |
| `handoff` | sender → receiver | Artifact/evidence boundary hoàn tất |
| `policy_decided` | policy → issue | Kết quả quyết định quan sát được |
| `verification_completed` | verifier → contracts | Output đã pass invariant và schema |
| `case_finalized` | coordinator | Output đã ghi atomically |

Trace không chứa prompt, chain-of-thought, raw MCP payload hoặc API key.

## 4. Evidence lifecycle

Mỗi response đi qua các gate sau:

1. `EvidenceGateway` đọc được cả `CallToolResult.is_error` của MCP hiện tại và
   `isError` của API cũ.
2. Validate envelope bằng `mcp-evidence-response-v1.schema.json`.
3. Tính SHA-256 của canonical JSON `data`
   (`sort_keys=true`, separators không whitespace) và so với `result_hash`.
4. Collector kiểm tra domain đúng tool và mọi `order_id` trong payload cùng scope.
5. `get_policy` phải trả đúng `policy_version` của case.
6. Chỉ sau đó evidence được đăng ký, dùng và trích dẫn.

Registry là per-case. Evidence không được tái sử dụng giữa các case, ref không
được sửa và output chỉ được trích dẫn ref vừa consume. `policy_decided`,
`handoff` và `verification_completed` đều liên kết ref để scorer kiểm tra
provenance. Claim nhận subset ref thực sự hỗ trợ claim đó. Mọi payment value và
capture amount được chuẩn hóa bằng `Decimal`; split payment chỉ hợp lệ khi cả hai
credit-card và voucher leg đều có capture confirmed.

Policy seller ID chỉ được giữ nguyên khi ID đó tồn tại trong scoped seller
evidence. Nếu policy ID null/stale và case chỉ có đúng một seller thì dùng seller
duy nhất; với nhiều seller không thể xác định duy nhất thì giữ null thay vì gán ID
sai.

`data_conflicts` ghi nhận duplicate item/payment ID có giá trị khác nhau thay vì
âm thầm chọn một row. Những field mâu thuẫn không có decisive evidence được giữ
`selected_source: null`; Verifier chỉ phát hành output khi mọi invariant và scope
check vẫn pass.

Policy Agent áp dụng thứ tự bằng chứng:

1. refund status pending/failed;
2. order status canceled/unavailable;
3. shipment event late chỉ được chấp nhận khi timestamp event khớp
   `delivered_customer_at`;
4. reconciliation mismatch;
5. duplicate payment type/value;
6. valid credit-card + voucher split;
7. unsupported nếu không có issue decisive;
8. insufficient nếu core evidence thiếu hoặc không đọc được.

## 5. Failure policy và retry

MCP tools là read-only/idempotent. Bước mở/initialize MCP session và
`EvidenceGateway.call` đều tối đa 3 lần cho transport timeout/connection error,
với exponential backoff `0.2s`, `0.4s`. Retry session chỉ áp dụng trước khi
workflow nhận gateway; lỗi phát sinh trong một case không mở lại toàn bộ case.
`MCPError(CONNECTION_CLOSED)` và `MCPError(REQUEST_TIMEOUT)` sẽ retire session
chết, tạo HTTP client/session mới rồi retry chính tool call đó. CLI xử lý 10
case/session; transport cancellation sẽ mở session mới và retry case hiện tại,
không chạy lại các case trước. HTTP timeout là 300
giây cho read và 30 giây cho connect/write/pool. Retry tool
giữ nguyên arguments và `case_id`; mọi server call vẫn được audit. Không retry
tool business error vì lỗi đó không mất khi gọi lại.

| Failure | Retry? | Fallback / stopping rule | Trace |
| --- | --- | --- | --- |
| MCP timeout, connection reset | Có, tối đa 3 | Hết retry thì abort case; không tạo evidence giả | Không có error event trong schema; các tool result đã consume vẫn giữ nguyên |
| Tool trả `is_error` | Không | Abort bằng `RuntimeError` | Không `case_finalized` |
| Tool bị thiếu khi discovery | Không | Abort trước khi gọi tool đoán tên | Không `case_finalized` |
| Envelope sai schema hoặc hash sai | Không | Abort; không retry payload không đáng tin cậy | Không `case_finalized` |
| Evidence sai domain/order/policy scope | Không | Abort trước handoff | Không `case_finalized` |
| Source conflict | Không retry network | Ghi `data_conflicts`; verifier phải xác nhận invariant | `verification_completed` khi pass |
| Output/invariant sai | Không | Không ghi output, không finalize | Không `verification_completed`, không `case_finalized` |

- `qwen/qwen3-8b`;
- `qwen/qwen3-8b:free`.

Trước `case_finalized`, Verifier bắt buộc kiểm tra:

- output khớp `case_id` và validate đúng `l3a-output-v2.schema.json`;
- mọi evidence ref trong output nằm trong registry của đúng case;
- mọi ref của claim là subset của evidence toàn case;
- order/item/seller/payment/shipment sets được deduplicate và giữ đúng giới hạn schema;
- `evidence_refs` bao phủ đúng artifact đã consume;
- tổng `refund_lines` bằng `recommended_refund_brl`;
- `no_action`/`needs_investigation` không có refund line;
- ma trận issue → responsible party và issue → action phải khớp; seller ID phải
  thuộc affected seller set và refund entity phải thuộc đúng payment/seller/order
  scope;
- assessment giữ đúng Policy decision;
- confidence nằm trong `[0, 1]`, bị trừ theo số conflict/warning và được giữ
  nguyên từ Policy sang Verifier;
- resolution action không rỗng và không trùng;
- schema version, currency và mọi enum đúng L3A.

## 7. Reproducibility và tài nguyên

- Python `>=3.11`; dependency được giới hạn theo range trong `pyproject.toml`.
- Không dùng LLM, random seed, prompt hay network model. Quyết định business là
  deterministic state-machine.
- CLI concurrency: 1 case; specialist stage và MCP transport đều concurrency 1.
- MCP inventory được cache trong gateway session; CLI rotate session mỗi 10 case
  và tắt terminate-on-close để cleanup không bị chặn bởi server shutdown. L3A
  không cộng điểm efficiency trực tiếp nhưng workflow giới hạn 7–8 audited
  calls/case.
- Evidence ref, trace event ID và timestamp do server/runtime sinh nên trace byte
  không giống nhau giữa các lần chạy; semantic evidence hash và decision thì
  deterministic.
- Output được ghi qua file `.tmp` rồi atomic replace. Submission chỉ gồm
  `manifest.json`, `trace.jsonl`, và `outputs/<case_id>.json`.

Lệnh tái lập:

```bash
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```
