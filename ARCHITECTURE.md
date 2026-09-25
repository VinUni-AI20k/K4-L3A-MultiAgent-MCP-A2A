# L3A Architecture Record

Tài liệu này mô tả implementation hiện tại của hệ thống điều tra khiếu nại L3A. Nội
dung chỉ ghi quyết định có thể kiểm chứng qua source, output và trace; không ghi API
key, prompt bí mật hoặc chain-of-thought.

## 1. System overview

```text
case-set.json + inputs/<case_id>.json
                  |
                  v
             Coordinator
                  |
       +----------+-----------+----------------+
       |          |           |                |
       v          v           v                v
 Order/item   Payment     Shipment          Policy
   agent       agent        agent            agent
       |          |           |                |
       +----------+----- MCP Evidence Gateway--+
                          |
                          v
                 validated evidence envelopes
                          |
                          v
                       Verifier
                          |
              +-----------+------------+
              v                        v
   outputs/<case_id>.json      traces/trace.jsonl
```

CLI đọc đúng 100 case theo thứ tự trong `case-set.json`. Mỗi case được xử lý tuần tự
với concurrency limit bằng 1 để giữ trace dễ kiểm toán và tránh gây tải lớn cho MCP.
Tool được discovery lúc chạy; solver không tự đoán tool không tồn tại.

Output hoàn thành được ghi atomically qua file `.tmp`, sau đó xuất hiện ngay tại
`outputs/<case_id>.json`. Artifact trung gian đồng thời được giữ trong
`.run-staging/` để có thể tiếp tục khi SSE hoặc MCP connection bị ngắt.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool được phép dùng | Handoff |
| --- | --- | --- | --- | --- |
| `coordinator` | case, customer claims, tool metadata | correlation bằng `case_id`, chọn profile điều tra, phân công và tổng hợp | discovery; không tự tạo evidence | specialist và verifier |
| `order-item-agent` | order ID và ID tìm thấy từ evidence | xác minh order, item, seller và entity linkage | order, item, seller tools | coordinator/verifier |
| `payment-agent` | order ID, payment/refund context | payment split, mismatch, duplicate charge và refund lifecycle | payment và refund tools | coordinator/verifier |
| `shipment-agent` | order/shipment context | delivery timeline, seller handoff và logistics responsibility | shipment tools | coordinator/verifier |
| `policy-agent` | `policy_version`, evidence nghiệp vụ | lấy policy có thẩm quyền và policy decision | policy tool | verifier |
| `verifier` | draft + evidence refs | kiểm tra schema, scope, consistency, refund và confidence | không gọi MCP | coordinator finalize |

Tool routing được giới hạn theo primary investigation topic:

- mọi case: `get_order`, `get_policy`;
- canceled/unavailable: payment, và item/seller khi cần;
- late delivery: item, seller, shipment;
- payment mismatch/duplicate: payment rows và payment timeline;
- refund pending/failed: payment và refund timeline;
- unsupported claim: order, item, payment, shipment và policy.

Workflow chỉ gọi tool khi toàn bộ required argument ngoài `case_id` có trong context.
Một tool được gọi tối đa một lần cho mỗi case và routing có tối đa ba vòng.

## 3. A2A protocol and trace

Trao đổi giữa các actor được biểu diễn bằng public trace contract
`day09-trace-event-v1`:

1. CLI emit `case_received`.
2. Coordinator emit `task_assigned` trước mỗi specialist task.
3. Specialist emit `tool_result_consumed` sau khi MCP envelope đã validate.
4. Coordinator emit `handoff` sang verifier.
5. Policy agent emit `policy_decided` khi có policy evidence.
6. Verifier emit `verification_completed` sau khi invariant pass.
7. CLI emit `case_finalized` sau khi output hợp lệ.

`case_id` là correlation ID bắt buộc. `actor`, `target` và `decision_code` mô tả sự
phối hợp quan sát được. `evidence_refs` chỉ chứa reference MCP thật. Trace không chứa
nội dung suy luận riêng.

## 4. Evidence lifecycle and provenance

1. `EvidenceGateway` discovery name, description và input schema của MCP tools.
2. Mọi call luôn truyền đúng `case_id` và chỉ dùng argument lấy từ input/evidence cùng
   case.
3. Response phải pass `mcp-evidence-response-v1.schema.json` trước khi được sử dụng.
4. `evidence_ref` được giữ nguyên; client không sửa, hash lại hoặc tự sinh ref.
5. Evidence đã dùng được liên kết vào output và trace `tool_result_consumed`.
6. Context/evidence được khởi tạo riêng cho từng `solve_case`; không tái sử dụng chéo
   case.
7. Submission phải dùng evidence của fresh MCP run hiện tại; không copy refs từ ZIP
   hoặc submission cũ vì scorer kiểm tra team, run và case scope.

Nếu một case không lấy được bất kỳ evidence hợp lệ nào, solver fail-fast và không
publish run. Điều này ngăn tạo submission `insufficient_evidence` hàng loạt hoặc ref
giả dẫn tới hard gate.

## 5. Business decisions

Các mapping chính được verifier dùng để giữ nhất quán giữa issue, cause,
responsibility, refund và action:

| Primary issue | Root cause | Responsible party | Resolution action |
| --- | --- | --- | --- |
| `canceled_order_paid` | `ORDER_CANCELED_AFTER_PAYMENT` | `platform / OLIST_PLATFORM` | `issue_full_refund` |
| `unavailable_order_paid` | `ORDER_UNAVAILABLE_AFTER_PAYMENT` | `platform / OLIST_PLATFORM` | `issue_full_refund` |
| `late_delivery_seller` | `SELLER_HANDOFF_AFTER_LIMIT` | seller vi phạm | `refund_freight`, `review_seller_handoff` |
| `late_delivery_logistics` | `CARRIER_DELIVERED_AFTER_ESTIMATE` | `logistics_provider / LOGISTICS_PROVIDER` | `refund_freight`, `review_carrier_delay` |
| `valid_split_payment` | `MULTIPLE_PAYMENTS_RECONCILED` | không có | `explain_valid_split_payment` |
| `payment_mismatch` | `PAYMENT_TOTAL_MISMATCH` | `payment_provider / PAYMENT_PROVIDER` | `reconcile_payment` |
| `duplicate_charge` | `DUPLICATE_PAYMENT_CAPTURE` | `payment_provider / PAYMENT_PROVIDER` | `refund_duplicate_charge` |
| `refund_pending` | `REFUND_PENDING_PROCESSING` | `platform / OLIST_PLATFORM` | `verify_refund_completion` |
| `refund_failed` | `REFUND_PROCESSING_FAILED` | `payment_provider / PAYMENT_PROVIDER` | `retry_refund`, `escalate_payment_provider` |
| `unsupported_claim` | `CLAIM_UNSUPPORTED` | không có | `reject_claim` |

Tiền được biểu diễn bằng BRL và làm tròn hai chữ số. Late-delivery refund lấy freight
từ item evidence, fallback sang shipment evidence. Refund lifecycle được ưu tiên hơn
payment amount khi MCP cung cấp refund evidence có thẩm quyền.

## 6. Failure and resume policy

| Failure | Xử lý | Artifact |
| --- | --- | --- |
| Tool error/not found | ghi `TOOL_RESULT_UNAVAILABLE`, tiếp tục evidence độc lập | không tạo ref giả |
| Invalid evidence envelope | reject response | không emit consumed ref |
| Không có evidence cho case | fail-fast | không publish run |
| SSE/transport disconnect | dừng và hướng dẫn `day09 run --resume` | giữ các case đã hoàn thành |
| Trace dở dang của case đang lỗi | loại khỏi staging khi resume | case đó được chạy lại sạch |
| Output/schema mismatch | dừng trước finalize | output lỗi không được publish |

Fresh run dùng:

```powershell
day09 run
```

Trong quá trình chạy, CLI in và ghi từng case:

```text
[1/100] wrote outputs/L3A_CASE_001.json
...
[100/100] wrote outputs/L3A_CASE_100.json
```

Nếu connection rớt, tiếp tục bằng:

```powershell
day09 run --resume
```

Resume lấy danh sách hoàn thành từ `.run-staging/outputs`, loại trace dở dang của case
chưa có output và chỉ chạy các case còn thiếu. Khi đủ 100 case, staging trace mới thay
thế `traces/trace.jsonl`.

## 7. Verification invariants

Trước finalize/package, hệ thống bảo đảm:

- đúng đủ 100 output, không thiếu hoặc thừa case;
- `case_id` của filename, input, output và trace khớp nhau;
- output pass `day09-l3a-output-v2`;
- evidence envelope và trace event pass public schemas;
- evidence refs thuộc case hiện tại và có consumed trace linkage;
- entity arrays unique và đúng giới hạn schema;
- primary issue, status, cause, responsible party và actions nhất quán;
- refund không âm, currency là BRL và tổng refund line khớp recommended refund;
- confidence nằm trong `[0, 1]` và phản ánh evidence coverage;
- event ID duy nhất, trace đúng lifecycle và không chứa Team API Key;
- ZIP chỉ có `manifest.json`, `trace.jsonl` và `outputs/*.json`.

## 8. Reproducibility and commands

- Runtime hiện tại: Python 3.10+.
- Dependency được khai báo trong `pyproject.toml` và `requirements.txt`.
- Solver không dùng generative model; kết quả deterministic với cùng input và MCP
  responses.
- Random chỉ được dùng tạo unique trace event ID.
- Concurrency limit: 1 case.
- Resource bounds: tối đa ba routing pass và một call/tool/case.

Quy trình chuẩn:

```powershell
python -m pip install -r requirements.txt
day09 validate-inputs
day09 mcp-tools
day09 run
# Nếu SSE bị ngắt:
day09 run --resume
day09 validate
day09 package --output dist/submission-fresh-run.zip
```

Không chạy script biến đổi output hoặc tái sử dụng ZIP/evidence refs cũ sau fresh run.
API key chỉ nằm trong `.env` và không được đưa vào source, trace, output hoặc ZIP.
