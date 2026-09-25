# L3A Architecture Record

Workflow L3A dùng Python async và các vai trò có quyền tool riêng trong
`src/student_agent/workflow.py`. Không dùng LLM, không cần API key của nhà cung cấp
model. Public schemas trong `contracts/schemas/` được giữ nguyên.

## 1. System overview

```text
CLI: case_received
  → Coordinator
    → Order/item agent → get_order, get_order_items
    → Payment agent    → get_payment_timeline, get_refund_timeline (khi cần)
    → Shipment agent   → get_shipment_summary
    → Policy agent     → get_policy → quyết định dựa trên evidence
    → Verifier         → schema + invariants
  → CLI: lưu output → case_finalized

Mọi tool call đi qua EvidenceGateway; mọi event đi qua TraceWriter.
```

Specialists chạy tuần tự để giới hạn tải MCP và giữ thứ tự audit ổn định. Mỗi case
có một `_CaseWorkflow` riêng; không chia sẻ evidence giữa các case. `solve_case()`
chỉ trả dictionary; CLI chịu trách nhiệm ghi `outputs/<case_id>.json`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case và danh sách tool discovery | Giao việc, quản lý state riêng từng case | `task_assigned` tới specialist |
| Order/item | `claimed_order_id` | Chỉ `get_order`, `get_order_items`; xác minh scope, item/seller | Evidence order/item tới policy |
| Payment | Order ID | Chỉ `get_payment_timeline`, `get_refund_timeline`; kiểm tra capture/refund | Evidence payment/refund tới policy |
| Shipment | Order ID | Chỉ `get_shipment_summary`; xác minh timeline | Evidence shipment tới policy |
| Policy | Evidence specialists, `policy_version` | Chỉ `get_policy`; chọn issue, responsibility, refund, actions, confidence | Output dự kiến tới verifier |
| Verifier | Output và evidence đã tiêu thụ | Không gọi tool; kiểm tra schema và invariants | Output đã validate tới CLI |

Quyền được kiểm tra qua `PERMISSIONS` trước call. Tên tool phải xuất hiện trong
discovery. Claim của khách chỉ giúp định tuyến việc lấy refund lifecycle, không
được dùng làm bằng chứng kết luận. Không truy vấn customer/product ngoài nhu cầu.

## 3. A2A protocol

Handoff nội bộ dùng envelope evidence nguyên gốc trong state theo tên tool.
Correlation dùng `case_id` của state; trace chứa `actor`, `target`, `tool_name`,
`evidence_refs`, `decision_code` khi phù hợp. Specialist bàn giao sau khi validate
scope/domain; lỗi được bàn giao bằng mã lỗi và không có evidence giả.

Không có vòng lặp A2A. Sau các specialist, policy emit `policy_decided`, bàn giao
verifier; verifier chỉ emit `verification_completed` sau khi tất cả kiểm tra pass.
CLI emit `case_received`/`case_finalized`, workflow không emit trùng. Chỉ dùng 7
event type công khai; retry được biểu diễn bằng `task_assigned` với code `RETRY`.
Không ghi API key, prompt, raw exception hoặc nội dung suy luận vào trace.

## 4. Evidence lifecycle

Gateway validate JSON Schema envelope. Workflow kiểm tra thêm domain, kiểu payload,
order ID ở cả object lồng nhau và policy version. Chỉ envelope hợp lệ mới được
lưu và emit `tool_result_consumed`; `evidence_ref` không được tạo lại hoặc sửa.

Policy trích dẫn các tool dùng để xác định issue, entity và giải quyết mâu thuẫn;
claim assessments tham chiếu cùng bộ bằng chứng quyết định. Shipment không được
trích dẫn cho canceled/unavailable nếu không tham gia giải quyết mâu thuẫn.
Server audit là nơi xác minh team/run/case; client không thể tự chứng minh ownership
trên server chỉ bằng việc kiểm tra định dạng ref.

Payment/refund events được xét từ ngày mua tới `opened_at`. Các item trùng ID chỉ
được gộp khi còn đúng một bản ghi duy nhất có shipping limit trong khoảng ngày mua
và hạn giao; trường hợp không phân giải được chuyển sang điều tra. Timestamp giữa
order/shipment khác nhau không được tự chọn một nguồn là đúng.

`get_policy` cung cấp rule nghiệp vụ và số tiền. File scoring chỉ mô tả cách chấm,
không chứa luật hoàn tiền. Số tiền dùng Decimal, không cộng trùng item/payment rows.
Seller ID trong policy template được đối chiếu và thay bằng seller có evidence
thuộc order, đồng thời ghi `data_conflicts` và giảm confidence.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout/transport lỗi | Tối đa 3 lần/tool | Thiếu bằng chứng, policy xét khả năng kết luận | `task_assigned/RETRY`, `handoff/MCP_TRANSIENT_FAILURE` |
| HTTP 429/502/503/504 | Tối đa 3 lần/tool | Như trên | `RETRY`, `MCP_HTTP_FAILURE` |
| Tool không có, access denied, not found, lỗi server không phân loại | Không | Không coi lỗi là kết quả rỗng hợp lệ | `handoff/TOOL_UNAVAILABLE` hoặc `INVALID_OR_UNAVAILABLE_EVIDENCE` |
| Source conflict | Không gọi lặp vô hạn | Phân giải bằng timeline/scope; nếu còn mơ hồ, điều tra | `data_conflicts`, `policy_decided` |
| Invalid specialist result | Không | Không chấp nhận evidence; nếu thiếu dữ liệu cốt lõi, điều tra | `handoff/INVALID_OR_UNAVAILABLE_EVIDENCE` |

Deadline mỗi call 45 giây, backoff 0.5 rồi 1 giây. Chỉ retry tool đọc dữ liệu.
Nếu tool hoàn tiền lỗi khi đang xác minh claim refund, không kết luận hoàn tiền
thành công hoặc thất bại. Thiếu evidence cốt lõi trả `insufficient_evidence`,
`needs_investigation`, số tiền 0 (chưa đề xuất chi tiền), confidence 0.25.
Lỗi verifier dừng case thay vì ghi output không hợp lệ. Mất kết nối cả session có
thể làm CLI dừng; không có tự động resume hoặc giả lập evidence.

## 6. Verification invariants

- Output đúng public schema, `case_id` khớp case đang chạy.
- Mọi ref đã được gateway trả về, validate và tiêu thụ trong state của case.
- Claim refs thuộc tập refs của output; claim IDs khớp yêu cầu đầu vào.
- Entity IDs có trong evidence được trích dẫn; seller chịu trách nhiệm thuộc order.
- Tổng refund lines bằng tổng refund, entity hoàn tiền đúng order.
- Rule policy, status, số tiền và action nhất quán; `no_action` không có hoàn tiền.
- Lỗi seller/logistics/payment không được gán sang bên không phù hợp.
- Confidence trong [0,1], bắt đầu 0.96, giảm 0.07/mâu thuẫn và 0.05/tool có warning;
  mức sàn cho kết luận có bằng chứng là 0.50. Đây là heuristic, chưa fit theo nhãn.
- Duplicate-charge suy ra từ nhiều capture bằng nhau và tổng vượt giá trị order
  bị giới hạn confidence 0.75 vì thiếu transaction identity. Split payment phải có
  nhiều capture và tổng khớp giá trị order; không coi nhiều payment rows là thu trùng.

Giới hạn: chưa có oracle để xác nhận độ đúng nghiệp vụ; mốc `opened_at` được dùng
làm cutoff. Trường hợp nhiều dispute cùng lúc dùng thứ tự refund lifecycle,
canceled/unavailable, reconciliation event, late delivery rồi kiểm tra tổng tiền.

## 7. Reproducibility

Python >=3.11, dependencies theo `pyproject.toml`; repo hiện dùng khoảng version,
chưa có lockfile. Không model, random seed hay package mới. Concurrency MCP = 1.
Quyết định xác định theo evidence/policy; event IDs và timestamps thay đổi mỗi lần.

```powershell
day09 validate-inputs
day09 mcp-tools
python -m pytest tests/test_starter.py -q
python -m ruff check src/student_agent/workflow.py src/student_agent/mcp_gateway.py tests/test_starter.py
day09 run
day09 validate
```

Test workflow dùng evidence tổng hợp trong file test hiện có, không dùng làm output
nộp bài. `test_release_safety.py` yêu cầu bản starter không chứa input/output nên
không phù hợp sau khi đã tải bộ đề. Smoke test thật dùng MCP và trace tạm.
`day09 validate` kiểm tra artifacts nhưng không thay thế server scoring/audit.
`day09 run` xóa outputs/trace cũ trước khi chạy, không tiếp tục từ case dang dở.
