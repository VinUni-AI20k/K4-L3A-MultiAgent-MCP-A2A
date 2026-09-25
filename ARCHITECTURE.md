# L3A Architecture Record

## 1. System overview

L3A xử lý từng case độc lập. Customer request chỉ là dữ liệu để định tuyến và tạo các
nhiệm vụ xác minh. Dữ kiện dùng để kết luận phải đến từ MCP Evidence Gateway và có
`evidence_ref` hợp lệ.

```text
Input case
    |
    v
Coordinator / Router
    |---- Order/Item Agent ----|
    |---- Payment Agent -------|--> MCP Evidence Gateway
    |---- Shipment Agent ------|
    |
    v
Policy Agent
    |
    v
Verifier Agent
    |
    v
Validated L3A output + observable trace
```

`solve_case(case, gateway, trace)` là điểm vào duy nhất. Coordinator nhận case,
phát nhiệm vụ cho các specialist cần thiết, chuyển facts đã xác minh cho Policy,
rồi gửi toàn bộ candidate result đến Verifier. Chỉ Verifier được phép trả output cuối.

Mỗi case có evidence registry riêng. Registry lưu `evidence_ref`, domain, tool đã gọi
và các claim mà evidence hỗ trợ. Registry bị hủy khi `solve_case` kết thúc, vì evidence
không được dùng chéo case.

## 2. Public contracts

Các schema trong `contracts/schemas/` là public contract đã phát hành và được giữ nguyên:

| File | Vai trò |
| --- | --- |
| `l3a-output-v2.schema.json` | Output của từng case L3A |
| `trace-event-v1.schema.json` | Sự kiện trace quan sát được |
| `submission-manifest-v2.schema.json` | Manifest của submission |
| `mcp-evidence-response-v1.schema.json` | Envelope evidence từ MCP Gateway |

Các object public đều được validate qua `Contracts`. Output, trace và manifest không
được thêm field ngoài schema. `l3a-output-v2.schema.json` là schema output của variant
hiện tại; không dùng schema L3B. `EvidenceGateway.call()` validate evidence envelope
trước khi trả dữ liệu cho workflow.

## 3. Agent ownership và tool permissions

| Actor | Nhận gì | Trách nhiệm | Quyền MCP | Handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Case, catalog tool đã discovery | Kiểm tra `case_id`, đọc claim/identifier, chọn domain cần xác minh | Chỉ `list_tools()` | Tạo task cho specialist và Policy |
| Order/Item Agent | Order ID và claim liên quan | Xác minh order, item, seller và trạng thái order | Chỉ các tool đã discovery dành cho order, item, seller | Facts order/item + refs |
| Payment Agent | Order ID, payment claim | Xác minh payment, charge và refund | Chỉ các tool đã discovery dành cho payment, refund | Facts payment + refs |
| Shipment Agent | Order ID, delivery claim | Xác minh shipment, mốc thời gian và logistics | Chỉ các tool đã discovery dành cho shipment | Facts shipment + refs |
| Policy Agent | Claim, facts, policy version | Đối chiếu facts với policy evidence có sẵn | Chỉ tool policy đã discovery | Policy decision + refs |
| Verifier Agent | Candidate output, evidence registry | Kiểm tra scope, evidence linkage, consistency và schema | Không gọi MCP | Output đã validate hoặc lỗi workflow |

Tên tool và tham số chỉ được lấy từ `gateway.list_tools()` và tool description tại
runtime. Workflow không gọi tool dựa trên tên đoán trước. Khi thiếu identifier hoặc
không có tool phù hợp, agent trả trạng thái thiếu evidence, không suy đoán dữ liệu.

## 4. A2A handoff protocol

Message nội bộ giữa các agent dùng envelope sau:

```text
case_id, task_id, from_actor, to_actor, domain, claim_ids,
identifiers, facts, evidence_refs, status, error_code
```

`case_id` phải khớp case hiện tại trên mọi message và mọi MCP call. `task_id` là định
danh duy nhất trong case. `status` chỉ mô tả tiến độ như `completed`, `not_found`,
`unavailable` hoặc `insufficient_evidence`; đây là cấu trúc nội bộ, không xuất trực
tiếp trong output.

Coordinator phát `task_assigned` trước specialist task. Specialist phát `handoff` khi
trả facts cho Coordinator. Coordinator phát `handoff` khi chuyển facts sang Policy và
Verifier. Các event trace luôn dùng actor/target thật, `decision_code` ngắn và chỉ
metadata an toàn trong `attributes`; không ghi payload evidence, prompt hay suy luận
riêng.

Luồng không có vòng lặp giữa specialist. Verifier được quyền yêu cầu Coordinator thực
hiện tối đa một nhiệm vụ bổ sung khi thiếu bằng chứng bắt buộc. Sau đó case được chốt
với evidence hiện có hoặc kết quả `insufficient_evidence` / `needs_investigation` phù
hợp schema.

## 5. Evidence lifecycle

1. Specialist gọi MCP với đúng `case_id` và arguments được tạo từ identifier đã biết.
2. `EvidenceGateway` validate response theo `mcp-evidence-response-v1.schema.json`.
3. Specialist chỉ tạo fact từ `data` của response đã validate và đăng ký `evidence_ref`
   trong registry của case.
4. Khi fact được dùng cho claim hoặc quyết định, workflow emit `tool_result_consumed`
   kèm đúng `tool_name` và `evidence_refs`.
5. Policy và Verifier chỉ dùng refs đã có trong registry. Output chỉ đưa các refs thực
   sự hỗ trợ assessment, claim, root cause hoặc financial resolution.
6. Trước finalize, Verifier xác nhận mỗi ref thuộc đúng case/run và không trùng lặp.

Không tạo, sửa, tái sử dụng hoặc suy diễn `evidence_ref`. Response lỗi hay chưa được
dùng không được ghi là `tool_result_consumed`.

## 6. Failure policy

| Tình huống | Xử lý | Trace decision code |
| --- | --- | --- |
| Timeout hoặc lỗi kết nối MCP | Retry một lần với cùng tool, case và arguments; sau đó trả unavailable | `MCP_TIMEOUT` hoặc `MCP_UNAVAILABLE` |
| Không tìm thấy dữ liệu | Không đoán ID; trả insufficient evidence cho claim bị ảnh hưởng | `EVIDENCE_NOT_FOUND` |
| Evidence nguồn mâu thuẫn | Giữ facts/refs mâu thuẫn để Verifier đánh giá; không tự chọn nguồn | `SOURCE_CONFLICT` |
| Specialist trả message sai | Coordinator từ chối message và tạo lại task tối đa một lần | `SPECIALIST_RESULT_INVALID` |
| Evidence envelope sai schema | Bỏ response, không dùng ref/data; chỉ retry nếu lỗi là transport | `MCP_ENVELOPE_INVALID` |
| Không có tool hoặc identifier phù hợp | Không gọi tool đoán; chuyển claim sang thiếu evidence | `LOOKUP_UNAVAILABLE` |

Retry chỉ áp dụng cho thao tác đọc idempotent. Mọi retry có giới hạn để tránh loop và
được ghi lại bằng `attempt` trong `attributes`.

## 7. Verification invariants

Trước khi output được trả về, Verifier kiểm tra:

1. `case_id` trong input, output, task và evidence call hoàn toàn khớp.
2. Output đúng `l3a-output-v2.schema.json`, không có field dư và confidence nằm trong
   khoảng 0 đến 1.
3. Mọi `evidence_ref` trong output/trace tồn tại trong registry, có từ MCP audit và
   đúng case/run.
4. `affected_entities` chỉ chứa ID từ input hoặc evidence đã validate.
5. Claim verdict, root cause, responsible party, refund và action không mâu thuẫn với
   nhau hoặc evidence được trích dẫn.
6. Tổng các `refund_lines.amount_brl` bằng `recommended_refund_brl`; chỉ có refund khi
   evidence/policy hỗ trợ.
7. Trace đúng schema và có các lifecycle event bắt buộc: `case_received`,
   `task_assigned`, `handoff`, `verification_completed`, `case_finalized`.

Nếu invariant bắt buộc không đạt, workflow không được tạo output với evidence giả.

## 8. Reproducibility

- Python 3.11+; dependencies được khai báo trong `pyproject.toml`.
- Chạy discovery trước: `day09 mcp-tools`.
- Chạy toàn bộ flow: `day09 validate-inputs`, `day09 run`, `day09 validate`.
- Đóng gói sau khi validate: `day09 package --output dist/submission.zip`.
- Không có quyết định nghiệp vụ dựa trên random. Nếu triển khai concurrency, kết quả
  specialist phải được tổng hợp theo thứ tự domain ổn định.
