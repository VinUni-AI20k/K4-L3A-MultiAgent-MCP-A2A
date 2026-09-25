# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

**Trạng thái: đã triển khai (rules `l3a-rules-v2`).** Module tương ứng:
`a2a.py` (message contract), `coordinator.py` (intake, routing, budget, handoff,
validate result, candidate, remediation), `agents.py` (order/payment/shipment),
`policy.py`, `verifier.py`, `ledger.py` (Evidence Ledger + allowlist tool),
`mcp_gateway.py` (MCP streamable HTTP có deadline/retry), `competition.py`
(mở run), `workflow.py`.

Phát hiện từ MCP evidence thật (quyết định rules v2):

- MCP chỉ trả evidence khi team có run đang mở; `day09 run` gọi `POST /api/v2/runs`
  (giống workspace) trước khi chạy, và ref chỉ hợp lệ trong run đó.
- Evidence của một order có thể lẫn dòng thuộc cửa sổ thời gian khác. Specialist neo
  theo `order_purchase_timestamp`: capture trong [-1h, +1d], item có
  `shipping_limit_date` trong [0, +6d], refund trong [0, +25d], event giao trễ trùng
  ngày giao thực tế (±1d). Dòng trùng lặp hoàn toàn được loại.
- Nhãn chọn theo thứ tự: canceled/unavailable có tiền đã capture → refund failed →
  refund pending → `reconciliation_mismatch` → capture trùng → giao trễ (theo `actor`)
  → split khớp tổng đơn → `unsupported_claim`. `case_status`, action và loại bên chịu
  trách nhiệm lấy từ `get_policy` (EC_POLICY_V1); `party_id` seller lấy từ item.
- Case không thu được evidence nào bị đánh `failed` (không ghi output), để batch
  không bao giờ được đóng gói với toàn output rỗng evidence.

Điểm lệch có chủ đích so với đặc tả gốc bên dưới:

- Retry hạ tầng: tối đa 3 lần retry (backoff 1s/2s/4s) cho lỗi tạm thời, vì mạng
  thi đấu thực tế chập chờn; vẫn nằm trong deadline task và không retry 4xx/`isError`.
- `PolicyTaskPayload` có thêm `policy_version` và `intent_hints` (cần cho `get_policy`).
- Mỗi `AgentResult` được Coordinator chấp nhận sinh một event `handoff`
  (agent → coordinator); handoff request giữa domain sinh `handoff` + `task_assigned`.
- Transport tự viết thay cho `mcp` client: server trả `202` chunked cho notification
  mà không đóng body, khiến client cũ treo khi đóng session.
- `get_customer_history` (domain `customer`) chưa có owner nên không được gọi.
- Tool lỗi (`isError`) là coverage gap; khi không có evidence nào, kết luận là
  `insufficient_evidence` / `needs_investigation` và không có evidence ref.
Public contracts trong `contracts/` là chuẩn của cuộc thi và không được sửa để phù
hợp implementation. Các budget, timeout, interface nội bộ và quy tắc kiểm tra bổ sung
trong tài liệu là quyết định của nhóm, không phải yêu cầu đã được scorer xác nhận.

| Thành phần | Hiện trạng trong starter | Việc cần triển khai |
| --- | --- | --- |
| Public schema và validator | Có | Giữ nguyên contract; dùng lại validator |
| CLI, trace writer, packaging | Có | Cách ly lỗi case, kiểm tra lifecycle và báo batch chưa hoàn tất |
| MCP gateway | Có kết nối, list tên tool, validate envelope | Discovery metadata, allowlist, deadline/retry, ghi ledger |
| Coordinator, specialists, policy, verifier | `solve_case()` còn `NotImplementedError` | Hiện thực theo thiết kế bên dưới |
| A2A models và Evidence Ledger | Chưa có module triển khai | Chốt model có validation và context chỉ đọc |
| Dependency lock, workflow tests | Chưa có lock; test hiện tại chỉ kiểm tra starter | Tạo lock và test integration trước final run |

Các đoạn model bên dưới là đặc tả nội bộ; chúng không bổ sung field vào public output,
trace hay manifest. Mọi thay đổi candidate output phải được verify lại trước khi ghi.

## 1. System overview

Hệ thống sử dụng mô hình điều phối **supervisor-centric**. `Coordinator` sở hữu vòng
đời của từng case và là thành phần duy nhất được quyền giao nhiệm vụ, chọn agent tiếp
theo, yêu cầu sửa kết quả và tạo output cuối cùng.

Các domain specialist không gọi trực tiếp lẫn nhau. Khi một specialist cần thông tin
từ domain khác, agent trả một handoff request có cấu trúc về `Coordinator`.
`Coordinator` kiểm tra yêu cầu và giao task mới trong cùng phạm vi `case_id`.

Luồng xử lý chuẩn:

```text
                         ┌──────────────────────────┐
                         │        Input Case        │
                         │ inputs/<case_id>.json    │
                         └────────────┬─────────────┘
                                      │ case_received
                                      ▼
                         ┌──────────────────────────┐
                         │   Coordinator / Router   │
                         └────────────┬─────────────┘
                                      │ task_assigned / handoff
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
          ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
          │ Order/Item     │ │ Payment Agent  │ │ Shipment Agent │
          │ Agent          │ │                │ │                │
          └────────┬───────┘ └────────┬───────┘ └────────┬───────┘
                   └──────────────────┼──────────────────┘
                                      │ scoped MCP calls
                                      ▼
                         ┌──────────────────────────┐
                         │  MCP Evidence Gateway    │
                         │ validated evidence only  │
                         └────────────┬─────────────┘
                                      │ evidence returned to requester
                                      ▼
                         ┌──────────────────────────┐
                         │ Domain Specialist Result │
                         │ findings + evidence_refs │
                         └────────────┬─────────────┘
                                      ▼
                         ┌──────────────────────────┐
                         │ Coordinator / Aggregator │
                         └────────────┬─────────────┘
                                      ▼
                         ┌──────────────────────────┐
                         │       Policy Agent       │── policy MCP call
                         └────────────┬─────────────┘
                                      ▼
                         ┌──────────────────────────┐
                         │ Coordinator builds       │
                         │ candidate output         │
                         └────────────┬─────────────┘
                                      ▼
                         ┌──────────────────────────┐
                         │      Verifier Agent      │
                         └──────┬────────────┬──────┘
                                │ reject     │ accept
                                ▼            ▼
                     ┌──────────────────┐  ┌──────────────────────────┐
                     │ Coordinator /    │  │ Coordinator / Finalizer  │
                     │ remediation      │  │ validated output         │
                     └────────┬─────────┘  └────────────┬─────────────┘
                              │                         ▼
                              │              ┌──────────────────────────┐
                              │              │ outputs/<case_id>.json   │
                              │              └──────────────────────────┘
                              │
                              └── rerun affected specialist/policy,
                                  then return to Verifier (tối đa 1 vòng)

   Mọi actor ── observable events ──▶ traces/trace.jsonl
```

`Coordinator` chỉ định tuyến và tổng hợp kết quả; coordinator không được tự tạo dữ
liệu nghiệp vụ hoặc evidence. Mọi fact dùng để kết luận phải đến từ MCP response đã
được validate. `Policy Agent` áp dụng quy tắc xử lý trên các finding có evidence, còn
`Verifier` là quality gate cuối cùng trước khi finalize.

Nếu verifier phát hiện lỗi có thể khắc phục, `Coordinator` được mở tối đa một vòng
remediation rồi gửi lại cho verifier. Không cho phép specialist gọi trực tiếp specialist
khác hoặc tạo vòng lặp handoff không giới hạn. Nhánh reject cuối cùng dẫn đến case
`failed`, không quay lại remediation vô hạn và không phát `case_finalized`.
Coordinator sở hữu quyết định finalize; CLI ghi output và phát lifecycle event thay
mặt Coordinator. `solve_case()` không phát trùng `case_received`/`case_finalized`.

## 2. Agent ownership

Hệ thống áp dụng nguyên tắc **least privilege**: mỗi agent chỉ được dùng MCP tool
thuộc domain cần thiết cho trách nhiệm của agent đó.

| Actor | Input | Trách nhiệm | MCP domain được phép | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator | Input case và kết quả từ các agent | Nhận diện claim/entity ban đầu, giao task, tổng hợp kết quả và finalize | Không gọi MCP | Agent task, handoff hoặc final output |
| Order/item | Task cùng order/item identifier | Kiểm tra order, item, seller và product liên quan | `order`, `item`, `seller`, `product` | Finding về order/item, evidence refs, conflict hoặc handoff request |
| Payment | Task cùng order/payment identifier | Đối chiếu giao dịch, split payment, duplicate charge và refund | `payment`, `refund` | Finding tài chính, evidence refs, conflict hoặc handoff request |
| Shipment | Task cùng order/shipment identifier | Kiểm tra trạng thái, mốc thời gian và trách nhiệm vận chuyển | `shipment` | Finding vận chuyển, evidence refs, conflict hoặc handoff request |
| Policy | Các finding đã có evidence | Áp dụng chính sách và đề xuất resolution phù hợp | `policy` | Policy decision, resolution actions và evidence refs |
| Verifier | Candidate output và kết quả của các agent | Kiểm tra schema, scope, evidence linkage, consistency, refund và confidence | Không gọi MCP | `accept` hoặc `reject` kèm reason code và remediation request |

Khi bắt đầu run, lớp khởi tạo hạ tầng thực hiện discovery; việc này không phải quyền
gọi evidence tool của Coordinator. Gateway hiện chỉ trả tên tool: implementation
phải bổ sung description, input schema và phân trang discovery nếu server có hỗ trợ.
Sau khi xem metadata thực tế, nhóm định nghĩa mapping rõ ràng
`tool_name → allowed_actor → expected_domain → argument_schema`, rồi kiểm tra mapping
đó với discovery mỗi run. Không suy domain chỉ từ tên tool và không gọi tool chưa rõ
contract. Trước request, gateway kiểm tra actor, allowlist, arguments và `case_id`;
sau response, kiểm tra domain thực tế trước khi ghi ledger. Mapping đã xác minh có
thể lưu trong source; tên tool trong ví dụ không thay thế discovery.

Domain `customer` có trong evidence schema nhưng chưa có owner trong baseline. Nếu
case/tool thực tế cần domain này, nhóm phải bổ sung mapping và owner rõ ràng trước
khi dùng, không mặc định cấp quyền cho mọi agent.

`Coordinator` chỉ được dùng customer message để nhận diện claim và identifier ban đầu;
nội dung khách hàng không phải ground truth. Khi cần dữ liệu từ domain khác,
specialist trả handoff request về `Coordinator` thay vì tự gọi tool ngoài quyền.

Mỗi specialist trả về finding, affected entities, evidence refs, warning/conflict và
handoff request nếu có. Agent phải emit `tool_result_consumed` khi một evidence thực
sự được dùng để tạo finding. `Policy Agent` chỉ áp dụng policy lên các fact đã có
evidence. `Verifier` không tự gọi thêm tool hoặc âm thầm sửa candidate output.

## 3. A2A protocol

Mọi giao tiếp giữa `Coordinator` và các agent sử dụng hai message contract nội bộ:
`AgentTask` để giao việc và `AgentResult` để trả kết quả. Envelope giữ correlation;
payload được validate theo `task_type`. Các type trong ví dụ được đặc tả ở bảng dưới
và cần hiện thực thành model dùng chung trước khi các thành viên tích hợp agent.

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AgentTask:
    local_run_id: str
    case_id: str
    task_id: str
    sender: str
    target: str
    task_type: Literal["investigate_order", "investigate_payment",
                       "investigate_shipment", "apply_policy", "verify"]
    attempt: Literal[0, 1]
    payload: DomainTaskPayload | PolicyTaskPayload | VerificationTaskPayload


@dataclass(frozen=True)
class AgentResult:
    local_run_id: str
    case_id: str
    task_id: str
    actor: str
    status: Literal["completed", "needs_handoff", "insufficient_evidence", "failed"]
    payload: DomainResult | PolicyDecision | VerificationResult | None
    error_code: str | None
```

Contract của các payload (field bắt buộc trừ khi có ghi `optional`):

| Type | Field và ý nghĩa |
| --- | --- |
| `Claim` | `claim_id: str`, `text: str`; là lời khai chưa xác minh, ID giữ nguyên từ input hoặc do Coordinator cấp ổn định nếu input chưa có |
| `Finding` | `finding_id: str`, `finding_code: str`, `value: JSONValue`, `entity_ids: EntityIds`, `claim_ids: tuple[str, ...]`, `evidence_refs: tuple[str, ...]`; mọi finding nghiệp vụ phải có ref hỗ trợ |
| `HandoffRequest` | `target: str`, `task_type: str`, `reason_code: str`, `entity_ids: EntityIds`, `claim_ids: tuple[str, ...]`, `evidence_refs: tuple[str, ...]`; chỉ Coordinator được chuyển request thành task |
| `DomainTaskPayload` | `entity_ids: EntityIds`, `claims: tuple[Claim, ...]`, `findings: tuple[Finding, ...]`, `evidence_context: tuple[str, ...]`; cung cấp cả finding và ref từ task trước |
| `DomainResult` | `findings: tuple[Finding, ...]`, `affected_entities: EntityIds`, `evidence_refs: tuple[str, ...]`, `conflicts: tuple[DataConflict, ...]`, `warnings: tuple[str, ...]`, `handoff_requests: tuple[HandoffRequest, ...]` |
| `PolicyTaskPayload` | `claims`, `findings`, `affected_entities`, `evidence_context`, `conflicts`, `coverage_gaps: tuple[str, ...]`; snapshot tổng hợp kết quả và dữ liệu còn thiếu |
| `PolicyDecision` | `assessment`, `claim_assessments`, `root_cause_analysis`, `financial_resolution`, `resolution_actions`, `data_conflicts`, `evidence_refs`, `support_links: tuple[SupportLink, ...]`; các field nghiệp vụ theo public output schema, `claim_assessments` optional |
| `SupportLink` | `output_path: str` (JSON Pointer), `finding_ids: tuple[str, ...]`, `evidence_refs: tuple[str, ...]`; liên kết kết luận và phép tính với nguồn hỗ trợ, chỉ dùng nội bộ |
| `VerificationTaskPayload` | `candidate_output` đúng hình dạng public output, `candidate_revision: int`, `policy_decision: PolicyDecision`, `findings`, `coverage_gaps`, `trace_snapshot` chứa các event đã xảy ra |
| `VerificationResult` | `candidate_revision: int`, `accepted: bool`, `reason_codes: tuple[str, ...]`, `remediation_requests: tuple[RemediationRequest, ...]`; nằm trong `AgentResult.payload` |
| `RemediationRequest` | `target: str`, `reason_code: str`, `output_paths: tuple[str, ...]`, `required_domains: tuple[str, ...]`; mô tả phần cần sửa hoặc bổ sung evidence |

`EntityIds` dùng năm key của `affected_entities` trong public schema với tuple ID
không trùng. `DataConflict` dùng các field công khai của `data_conflicts`.
`JSONValue` chỉ cho phép dữ liệu JSON, không cho phép object tùy ý, NaN hoặc Infinity.
Các field `claims`, `findings`, `conflicts`, `evidence_context` giữ cùng kiểu ở mọi
payload. `trace_snapshot` là danh sách event đúng trace schema, không phải suy luận.

Actor ID chuẩn: `coordinator`, `order-agent`, `payment-agent`, `shipment-agent`,
`policy-agent`, `verifier`. Chỉ `coordinator` gửi task. `local_run_id` do runner cấp
mỗi lần chạy; `task_id` duy nhất trong run, cấp theo thứ tự ổn định trong case.

Runner cung cấp view ledger chỉ đọc, gắn với `(local_run_id, case_id)` cho agent;
message chỉ mang ref và snapshot. Policy nhận toàn bộ finding liên quan; Coordinator
dùng `PolicyDecision` để tạo candidate đúng public schema và tăng `candidate_revision`
mỗi lần sửa. Verifier nhận chính snapshot candidate đó. Finalizer chỉ được ghi bản
candidate có revision được accept, không thay đổi field nghiệp vụ sau verification.

`frozen=True` chỉ khóa việc gán lại field, không làm bất biến dict bên trong. Khi hiện
thực, phải validate tại boundary và dùng snapshot được copy sâu hoặc cấu trúc chỉ đọc;
agent không được sửa context, ledger hoặc candidate mà agent khác đang sử dụng.

`AgentResult.status` chỉ nhận một trong các giá trị `completed`, `needs_handoff`,
`insufficient_evidence` hoặc `failed`. Coordinator chỉ chấp nhận result khi:

- `result.case_id == task.case_id`;
- `result.local_run_id == task.local_run_id`;
- `result.task_id == task.task_id`;
- `result.actor == task.target`;
- status thuộc tập giá trị cho phép;
- mọi evidence ref thuộc đúng case;
- finding dựa trên MCP data có liên kết đến evidence ref tương ứng.

Payload phải đúng loại task: ba task điều tra trả `DomainResult`, `apply_policy` trả
`PolicyDecision`, `verify` trả `VerificationResult`. `failed` được phép có payload
`None` nhưng phải có `error_code`; lỗi kỹ thuật không biến thành verdict nghiệp vụ.
Verifier chạy thành công trả status `completed` kể cả khi `accepted=False`.
Coordinator chỉ nhận verification result có `candidate_revision` khớp task đang chờ.
`accepted=True` yêu cầu reason_codes/remediation_requests rỗng; `accepted=False`
phải có reason code, có thể không có remediation request nếu lỗi không thể sửa.
`needs_handoff` yêu cầu có `HandoffRequest` trong `DomainResult`; Policy và Verifier
trả thiếu sót về Coordinator qua coverage/verification, không tự giao việc.

Result vi phạm một trong các điều kiện trên bị từ chối với decision code cho specialist
result không hợp lệ. Nội dung customer message trong `claims` chỉ là dữ liệu cần xác
minh, không được chuyển thành fact nếu chưa có evidence hỗ trợ.

Specialist chỉ tạo handoff request khi cần dữ liệu thuộc domain khác, thiếu identifier
mà domain khác có thể cung cấp, hoặc cần agent khác xác nhận một data conflict. Mọi
handoff phải quay về `Coordinator`; specialist không giao task trực tiếp cho nhau.

```text
Specialist → handoff request → Coordinator → AgentTask mới → Target specialist
```

Budget nội bộ của baseline được tính rõ như sau:

- Tối đa 6 domain task ở vòng đầu (`attempt=0`), bao gồm task phát sinh từ handoff.
  Policy và Verifier không tính vào sáu task này; mỗi agent chạy một task ở vòng đầu.
- Nếu verifier reject lỗi có thể sửa, có tối đa một vòng remediation (`attempt=1`):
  tối đa 3 domain task, mỗi domain một task chứa toàn bộ yêu cầu sửa, rồi Policy và
  Verifier chạy lại mỗi agent tối đa một lần. Task sửa dùng `task_id` mới.
- Hạ tầng retry không tạo task mới. Sửa format result tối đa một lần trong deadline
  task hiện tại, không mở thêm MCP call hoặc vòng handoff.
- Coordinator không giao lại cùng `(target, task_type, entity_ids)` nếu payload,
  evidence context và yêu cầu sửa không thay đổi. Lưu lịch sử task và route.

Khi hết budget, dừng thu thập, đánh giá evidence đã có và vẫn chạy Policy/Verifier.
Nếu đủ dữ liệu thì giữ kết luận được hỗ trợ; chỉ dùng `insufficient_evidence` khi
thật sự chưa đủ cơ sở. Hết budget là trạng thái thực thi, không tự quyết định nhãn.
Sau verification cuối cùng, case chỉ chuyển sang `finalized` hoặc `failed` như mục 5.

Mỗi hành động A2A được ánh xạ vào observable trace như sau:

| Hành động | Trace event |
| --- | --- |
| Coordinator tạo `AgentTask` | `task_assigned` |
| Agent yêu cầu hoặc hoàn tất bàn giao | `handoff` |
| Agent sử dụng MCP evidence | `tool_result_consumed` |
| Verifier kiểm tra candidate output | `verification_completed` |

Trace lưu `task_id`, `attempt`, `candidate_revision` và status trong `attributes`
dạng scalar; `decision_code` dùng field top-level đã có trong public schema.
Không lưu toàn bộ task/result, prompt hoặc nội dung suy luận riêng.

## 4. Evidence lifecycle

Mỗi case có một **Evidence Ledger** độc lập trong bộ nhớ. Ledger là nguồn duy nhất
được `Coordinator` và `Verifier` sử dụng để kiểm tra nguồn thu nhận evidence nội bộ
trong `(local_run_id, case_id)` hiện tại. Chỉ lớp gateway được đăng ký evidence từ
response thật; specialist không được tự thêm record. Server MCP audit mới là nguồn
xác nhận cuối cùng rằng ref thuộc đúng team, server run và case. Local ledger không
thay thế audit và không được tuyên bố đã xác thực provenance phía server.

```text
MCP call → Validate envelope → Register Evidence Ledger
         → Specialist finding → Policy → Verifier → Output
```

Mọi MCP call phải truyền đúng `case_id`, dùng tool đã discovery trong allowlist của
agent và validate response theo `mcp-evidence-response-v1` trước khi đọc `data`. Một
response sai schema, thiếu evidence ref hoặc được MCP trả về dưới dạng lỗi không được
đăng ký vào ledger và không được dùng để tạo finding.

Model nội bộ cho một evidence record:

```python
@dataclass(frozen=True)
class EvidenceRecord:
    local_run_id: str
    case_id: str
    evidence_ref: str
    result_hash: str
    domain: str
    tool_name: str
    request_arguments: JSONValue
    data: JSONValue
    warnings: tuple[str, ...]


class EvidenceLedger:
    # Khởi tạo riêng cho từng instance; chỉ gateway được ghi.
    _records: dict[str, EvidenceRecord]
```

Record phải được bảo vệ khỏi mutation, kể cả `data` và `request_arguments`: gateway
lưu snapshot độc lập, getter trả bản copy hoặc view bất biến. Arguments lưu dưới dạng
JSON đã validate, không chứa authorization header/key. Ref trùng với record khác hash,
domain hoặc payload phải bị từ chối, không ghi đè im lặng.

MCP envelope không chứa team/run/case; gateway gắn `local_run_id` của runner và
`case_id` của request để kiểm tra scope nội bộ. Local run ID không phải server run ID,
không tự thêm nó vào MCP arguments. Cách server xác định run cần được xác nhận từ
contract/discovery thực tế; nếu server yêu cầu run token phải tích hợp theo tài liệu
đó trước final run. Không tái dùng ref từ output/trace của lần chạy trước.

Giữ nguyên `evidence_ref` và `result_hash` do MCP cấp. Schema chỉ xác nhận định dạng
hash, không chứng minh hash khớp payload; chỉ tự tính lại nếu server cung cấp quy tắc
canonicalization. Envelope hợp lệ cũng chưa xác nhận cấu trúc nghiệp vụ bên trong
`data`: specialist cần validate payload theo contract tool đã discovery.
Ledger được giải phóng khi case finalized/failed, sau các kiểm tra và ghi nhận cần
thiết. Nhiều agent có thể dùng cùng evidence trong một case nếu thực sự liên quan.

Mỗi finding dựa trên MCP data phải liên kết trực tiếp đến evidence hỗ trợ:

```python
{
    "finding_code": "ORDER_CANCELED_AFTER_PAYMENT",
    "value": True,
    "evidence_refs": ["ev_..."]
}
```

Finding không có evidence chỉ được xem là claim chưa xác minh, warning hoặc
`insufficient_evidence`; finding đó không được dùng làm fact cho kết luận nghiệp vụ.
Trong handoff, agent gửi normalized finding, affected entities, evidence refs và
conflict/warning. Agent nhận handoff có thể tra evidence trong ledger nhưng phải giữ
nguyên evidence ref và không sao chép evidence sang case khác.

Khi evidence thực sự được dùng để tạo finding, policy decision hoặc output, agent phát
`tool_result_consumed` với đúng `case_id`, `tool_name` và `evidence_refs`. Evidence đã
được gọi nhưng không dùng cho kết luận không phát sự kiện consumed. Trace chỉ chứa ref
và metadata quan sát được, không chứa toàn bộ evidence payload.

`output.evidence_refs` là hợp của những evidence thực sự hỗ trợ primary issue, claim
verdict, root cause, responsible party, refund hoặc resolution action. Không đưa toàn
bộ evidence đã thu thập vào output vì evidence không liên quan làm giảm precision.
Mọi ref trong `claim_assessments[].evidence_refs` phải đồng thời xuất hiện trong
`output.evidence_refs`.

Trước khi chấp nhận output, `Verifier` kiểm tra:

- mỗi ref tồn tại trong ledger của case hiện tại;
- không có ref tự tạo hoặc đến từ case khác;
- mỗi claim và finding quan trọng có evidence linkage;
- evidence domain phù hợp với kết luận sử dụng nó;
- claim-level refs là tập con của output-level refs;
- số lượng evidence không vượt giới hạn public schema;
- warning hoặc conflict của evidence đã được xử lý phù hợp.

Khi hai nguồn evidence trong cùng case mâu thuẫn, workflow không được âm thầm chọn
một giá trị. Agent tạo `data_conflicts`, ghi các source liên quan và chỉ chọn source
khi policy hoặc quy tắc ưu tiên cho phép. Nếu không thể phân xử, `selected_source`
phải là `null`, kết quả chuyển sang `needs_investigation` khi phù hợp. Đánh giá lại
confidence theo ảnh hưởng của conflict đến nhãn được chọn như mục 6.6; không áp trần
confidence chỉ vì có conflict.

## 5. Failure policy

Workflow phân biệt lỗi tạm thời, lỗi dữ liệu của một case và lỗi cấu hình toàn hệ
thống. Chỉ lỗi tạm thời được retry; missing evidence không bao giờ được chuyển thành
dữ liệu phỏng đoán.

Giới hạn thực thi:

- mỗi lần thử MCP call có timeout tối đa 60 giây;
- mỗi agent task có deadline tổng 120 giây tính bằng đồng hồ monotonic, bao gồm mọi
  call, backoff, xử lý dữ liệu và sửa format result;
- timeout từng call là `min(60 giây, thời gian task còn lại)`; hết deadline phải hủy
  phần đang chờ và trả lỗi, không kéo dài task qua retry;
- mỗi logical MCP call chỉ được retry tối đa một lần nếu còn deadline;
- retry dùng fixed backoff 1 giây;
- parse `Retry-After` dạng số giây hoặc HTTP date. Nếu thời gian chờ vượt 30 giây hoặc
  không còn thời gian để gọi lại trong deadline, dừng retry task đó; không rút ngắn
  thời gian chờ rồi gửi request sớm. Nếu hợp lệ và còn budget, chờ ít nhất
  `max(1 giây, Retry-After)` rồi gọi lại;
- retry hạ tầng không tăng `AgentTask.attempt`; `attempt == 1` chỉ dành cho vòng
  remediation sau verification.

Chỉ retry MCP connection reset, timeout, HTTP `429`, HTTP `502`, `503`, `504` hoặc
lỗi mạng tạm thời trước khi nhận response. Không retry HTTP `400`, `401`, `403`,
`404`, invalid arguments, evidence not found, response sai schema, evidence ref sai
định dạng, source conflict hoặc lỗi nghiệp vụ xác định.

Chỉ retry tool truy vấn được xác nhận read-only/idempotent từ contract thực tế.
Retry giữ nguyên arguments và case scope nhưng có thể tạo audit entry/evidence ref
mới; mọi lần thử đều được tính vào thống kê call. Gateway hiện có transport timeout
300 giây và chưa có tổng deadline/retry này; cần hiện thực thêm, không chỉ đổi một
giá trị timeout trong HTTP client.

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout, network hoặc 502/503/504 | Tối đa một lần trong deadline | Trả lỗi task và gap; Coordinator đánh giá evidence còn lại | `handoff / MCP_UNAVAILABLE` |
| HTTP 429 | Tối đa một lần trong deadline | Chờ đúng hạn hoặc dừng retry, trả gap về Coordinator | `handoff / MCP_RATE_LIMITED` |
| Task hết deadline | Không mở retry mới | Hủy công việc đang chờ, trả lỗi task và gap | `handoff / TASK_DEADLINE_EXCEEDED` |
| Evidence not found | Không | Ghi finding nếu có negative evidence hợp lệ; handoff khi cần identifier khác | `handoff / EVIDENCE_NOT_FOUND` |
| Invalid evidence envelope | Không | Loại response khỏi ledger và từ chối finding | `handoff / INVALID_EVIDENCE_ENVELOPE` |
| Source conflict | Không | Ghi `data_conflicts`; dùng policy phân xử hoặc chuyển `needs_investigation` | `policy_decided / SOURCE_CONFLICT` |
| Invalid specialist result | Một lần sửa contract | Yêu cầu agent trả result hợp lệ bằng evidence đã có | `handoff / INVALID_SPECIALIST_RESULT` |
| Verifier reject | Một remediation | Chạy lại phần bị ảnh hưởng rồi verify lại | `handoff / VERIFICATION_REJECTED` |
| Remediation vẫn thất bại | Không | Case `failed`, không ghi candidate chưa được accept | `verification_completed / REMEDIATION_FAILED` |
| Authentication 401/403 | Không | Dừng toàn bộ run | Không tiếp tục xử lý case |
| Tool discovery hoặc configuration lỗi | Không | Dừng toàn bộ run | Không tiếp tục xử lý case |

Nếu MCP trả một evidence envelope hợp lệ cho biết entity không tồn tại, evidence đó
có thể được dùng để bác bỏ claim chỉ khi source, phạm vi truy vấn và identifier đủ
để chứng minh điều đó. Entity không tìm thấy do ID chưa xác định không tự bác bỏ claim.
Transport failure hoặc HTTP 404 không có envelope không phải negative evidence.
Khi một call thất bại, giữ evidence hợp lệ đã có và ghi gap; Coordinator/Policy quyết
định kết luận dựa trên tổng dữ liệu, không tự đổi mọi case thành `insufficient_evidence`.

Khi specialist result sai contract, `Coordinator` từ chối result, trả các reason code
quan sát được và cho agent sửa một lần bằng evidence đã có. Coordinator không tự sửa
result hoặc thêm evidence. Lần sửa format không gọi MCP và nằm trong deadline task.
Nếu vẫn sai, đánh dấu task `failed`; nhu cầu lấy thêm evidence phải đi qua một task
mới trong budget điều tra hoặc remediation, không dùng sửa format để lách budget.

Team API Key sai hoặc hết quyền, MCP session/tool discovery không khởi tạo được,
public contract không load được hoặc output directory không ghi được là lỗi toàn hệ
thống và phải dừng batch run. Lỗi chỉ thuộc một case không làm dừng các case còn lại,
nhưng workflow không được tạo output hoặc evidence giả chỉ để tiếp tục batch.

Đây là failure isolation cần bổ sung vào runner; CLI hiện tại chưa bắt lỗi riêng từng
case. Chỉ tiếp tục khi session/gateway còn dùng được; lỗi session toàn cục dừng run.
`insufficient_evidence` là nhãn nghiệp vụ, `failed` là trạng thái thực thi nội bộ,
không được thêm vào enum `case_status` của public output.

Case chỉ finalized khi candidate pass schema và được verifier accept. Nếu kết quả
bảo thủ có căn cứ, nó cũng phải qua verification; không có nhánh bỏ qua verifier.
Case failed không ghi output mới, không emit `case_finalized`; runner lưu lý do ở
báo cáo cục bộ ngoài submission. Kết thúc batch có case failed thì trả trạng thái
chưa hoàn tất, không package. `validate_artifacts()` yêu cầu đủ 100 output đúng case
set. Baseline chạy lại toàn bộ sau khi sửa lỗi; resume chọn lọc chỉ được bổ sung khi
đã xác định rõ server run scope, không ghép evidence/output từ các run khác nhau.

Các nguyên tắc diễn giải bắt buộc:

```text
Missing evidence != negative evidence
Timeout != claim sai
Not-found evidence hợp lệ != MCP transport failure
Source conflict != tự chọn nguồn thuận tiện nhất
```

## 6. Verification invariants

Verifier chạy các nhóm kiểm tra theo thứ tự cố định. Nếu vi phạm hard invariant,
candidate output không được finalize.

```text
Schema → Case scope → Evidence provenance → Semantic consistency
       → Financial consistency → Workflow/trace → Accept hoặc Reject
```

### 6.1 Schema và case scope

- Output phải pass `l3a-output-v2.schema.json`, có đúng `schema_version`, đúng
  `case_id` và không có field ngoài schema.
- Enum, số lượng phần tử, độ dài chuỗi và confidence `[0, 1]` phải hợp lệ.
- Mọi order, item, seller, payment và shipment ID phải đến từ input hoặc evidence của
  case hiện tại để làm khóa tra cứu. Trước khi đưa vào `affected_entities`, ID phải
  được evidence xác nhận phạm vi liên quan; danh sách không trùng.
- Entity chỉ xuất hiện trong customer message nhưng chưa được evidence xác nhận không
  được coi là affected entity.

Schema failure và case ID mismatch là hard failure.

### 6.2 Evidence và claim linkage

- Mỗi output evidence ref phải tồn tại trong Evidence Ledger của case/local run
  hiện tại; server team/run provenance được kiểm tra bởi MCP audit như mục 4.
- Không chấp nhận ref tự tạo, unknown hoặc cross-scope.
- Claim-level refs phải là tập con của output-level refs.
- Evidence xuất hiện trong output phải có `tool_result_consumed` tương ứng.
- Primary issue, root cause, responsible party, refund và action nghiệp vụ quan trọng
  phải có `SupportLink` nội bộ. Không thêm field linkage vào public output. Kết luận
  `insufficient_evidence` phải dẫn đến coverage gaps và evidence hiện có; không tạo
  ref giả cho dữ liệu không lấy được.
- Không đưa evidence không liên quan vào output.
- Mỗi claim chỉ có một assessment. Verdict `supported` phải có evidence trực tiếp;
  `unsupported` phải có negative evidence hợp lệ, không chỉ vì thiếu dữ liệu. Khi
  không đủ dữ liệu để xác nhận hoặc bác bỏ, verdict là `insufficient_evidence`.
- `case_status` phải phù hợp với primary issue và policy decision.

Unknown evidence, cross-scope evidence và missing required evidence là hard failure.
Verifier chỉ kiểm tra được yêu cầu evidence từ contract/policy đã biết và quy tắc
nhóm đã xác minh. Required evidence groups riêng của scorer không công khai; local
accept không bảo đảm đã qua toàn bộ hard gate phía server. Schema cho phép danh sách
evidence rỗng nhưng điều đó không bảo đảm case được chấm điểm.

### 6.3 Root cause và responsibility

- `ranked_causes` được sắp theo rank tăng dần; rank duy nhất và liên tục từ `1`.
- Mỗi cause phải được specialist findings hỗ trợ.
- Responsible party phải phù hợp với cause và không lặp cặp
  `(party_type, party_id)`.
- `party_id` phải có evidence hoặc là `null` khi chưa xác định.
- Không quy trách nhiệm cho seller, logistics hoặc payment provider chỉ từ customer
  claim.

### 6.4 Financial resolution

Tiền được tính bằng `Decimal` từ biểu diễn thập phân, không cộng bằng `float`.
Policy quyết định rounding; nếu policy không quy định, quy ước nội bộ là
`ROUND_HALF_UP` đến `0.01`, ghi rõ trong config để kiểm thử và điều chỉnh khi có
policy thực tế. Tổng đề xuất được tính từ các line đã làm tròn.

Tại boundary trả public output, amount phải là JSON number. Với serializer hiện tại,
chỉ chuyển Decimal sang số JSON-compatible sau khi kiểm tra
`Decimal(str(float(amount))) == amount`; nếu không bảo toàn số tiền thì reject và
yêu cầu serializer hỗ trợ Decimal. Không trả Decimal trực tiếp cho `json.dumps()` và
không chuyển tiền thành JSON string. Kiểm thử round-trip JSON bằng cách parse số tiền
trở lại Decimal và kiểm tra tổng theo cents; không dùng float để cộng/so sánh tổng.

- Currency luôn là `BRL`; amount không âm và được chuẩn hóa đến hai chữ số thập phân.
- `recommended_refund_brl` phải bằng tổng `refund_lines[].amount_brl`.
- Refund lớn hơn `0` phải có ít nhất một refund line, `action_required` và action
  tương ứng.
- Không lặp refund line có cùng `(reason_code, entity_id)`.
- Refund phải được evidence và policy hỗ trợ.
- `no_action` không được đi cùng đề xuất tạo refund mới.
- Đối chiếu số tiền đủ điều kiện, đã hoàn thành, đang xử lý và còn được phép hoàn
  theo từng giao dịch/item và policy. Không đề xuất lại phần đã hoàn hoặc đang xử lý.
  Refund đã hoàn thành một phần không ngăn đề xuất phần còn lại nếu evidence/policy
  hỗ trợ. Với refund failed, policy xác định retry hay tạo yêu cầu mới.
- Khoá đối chiếu chống hoàn trùng phải giữ transaction/item/refund identifier trong
  finding nội bộ. Cặp `(reason_code, entity_id)` trên output chỉ là quy tắc gộp line,
  không đủ để chứng minh chống duplicate refund.

### 6.5 Resolution actions và data conflicts

- `resolution_actions` không trùng, phù hợp với policy decision và `case_status`.
- Không vừa yêu cầu hành động vừa tuyên bố `no_action`.
- Khi cần điều tra thêm, status phải là `needs_investigation`.
- Mỗi data conflict có ít nhất hai source và một `resolution_code`.
- `selected_source` phải nằm trong `sources` hoặc là `null`.
- Conflict ảnh hưởng kết luận phải được Policy Agent xử lý; nếu chưa phân xử, đánh
  giá lại nhãn, status và confidence theo evidence còn lại. Không áp trần confidence
  tự động khi nhãn đúng có thể chính là `insufficient_evidence`.

### 6.6 Confidence

- Hard invariant công khai là số hữu hạn trong `[0, 1]`.
- Theo scoring policy, assessment confidence biểu thị mức tin cậy primary issue được
  chọn là đúng, không đồng nhất với phần trăm evidence đã thu thập.
- Không dùng các ngưỡng `0.90`, `0.60`, `0.50` làm điều kiện reject. Một kết luận
  `insufficient_evidence` có thể có confidence cao nếu evidence/gaps hỗ trợ nhãn đó.
- Claim confidence phản ánh độ tin cậy của verdict riêng cho claim, bao gồm
  `partially_supported` và `insufficient_evidence`.
- Nhóm phải xây quy tắc tính confidence có version và kiểm thử trên case gán nhãn
  hợp lệ. Heuristic chưa được hiệu chỉnh là giả định cần đánh giá, không phải luật
  của cuộc thi và không bảo đảm tối ưu điểm calibration.

Verifier kiểm tra phạm vi số và việc áp dụng nhất quán quy tắc đã công bố, không tự
đoán correctness hoặc âm thầm thay confidence. Vi phạm rule đã xác minh tạo reason
code; nghi ngờ chưa có căn cứ là warning để review, không tự thêm hard gate.

### 6.7 Trace lifecycle

Public scoring yêu cầu `case_received`, `task_assigned`, `handoff`,
`verification_completed`, `case_finalized`. `policy_decided` là event hợp lệ và được
nhóm yêu cầu khi có quyết định policy; nó không nằm trong danh sách required events
công khai. Chuỗi điển hình của một case thành công:

```text
case_received → task_assigned → handoff → policy_decided
              → verification_completed → case_finalized
```

Chuỗi trên là thứ tự phụ thuộc, không phải pattern mỗi event chỉ xuất hiện một lần.
Task/handoff/consumed có thể lặp; sau verification reject có thể có remediation,
policy decision mới và verification lần hai. Trace phải phản ánh các actor thực sự
tham gia, không tạo event để giả lập cộng tác.

Kiểm tra được tách thành hai giai đoạn:

1. Verifier kiểm tra trace snapshot trước finalize: đã nhận case, có task/handoff
   tương ứng, policy decision cho candidate hiện tại và mọi output ref đã có event
   consumed trước lúc verify. Không đòi `case_finalized` hoặc event verification của
   chính lần đang chạy phải tồn tại. Sau khi kiểm tra xong, phát
   `verification_completed` với `accepted` và `candidate_revision` trong attributes.
2. Coordinator nhận `AgentResult`, CLI ghi đúng candidate được accept bằng atomic
   replace rồi emit `case_finalized`. Bộ kiểm tra artifact sau run kiểm tra toàn bộ
   lifecycle, event ID không trùng, không có finalize sau reject cuối cùng và không
   có sự kiện xử lý thêm sau finalize. Case failed không được giả lập finalize.

Lớp audit artifact này cần bổ sung; `validate_artifacts()` hiện chủ yếu kiểm tra schema,
inventory và event ID, chưa thực thi đầy đủ thứ tự lifecycle/evidence linkage.
Mỗi event có tối đa 20 evidence refs trong khi output có thể có 30; chia thành nhiều
event consumed theo tool/actor khi cần, không vượt schema. Trace không chứa prompt,
chain-of-thought hoặc API key.

Verifier trả một `VerificationResult` có cấu trúc và không âm thầm sửa candidate:

```python
@dataclass(frozen=True)
class VerificationResult:
    candidate_revision: int
    accepted: bool
    reason_codes: tuple[str, ...]
    remediation_requests: tuple[RemediationRequest, ...]
```

Các reason code hard failure tối thiểu gồm `CASE_ID_MISMATCH`, `UNSCORABLE_SCHEMA`,
`MISSING_REQUIRED_EVIDENCE`, `INVALID_EVIDENCE_REFS`, `UNKNOWN_EVIDENCE_REF` và
`CROSS_SCOPE_EVIDENCE_REF`.

## 7. Reproducibility

Mục tiêu là **semantic reproducibility**: cùng source commit, input và MCP evidence
phải tạo cùng kết luận nghiệp vụ. Artifact không bắt buộc giống từng byte vì
`event_id`, timestamp và manifest `generated_at` được tạo mới trong mỗi run.
So sánh semantic theo data/result hashes và cấu hình rule, không yêu cầu evidence ref
do server cấp phải giống giữa các run. Fixture offline chỉ dùng test, không được đưa
ref giả/replayed vào submission thật.

### 7.1 Runtime và dependency

- Runtime chuẩn của nhóm là Python `3.11.x` trong `.venv`; không dùng package từ
  Conda base hoặc global environment.
- `pyproject.toml` là nguồn khai báo dependency. Trước final run, nhóm tạo và commit
  `requirements-lock.txt` từ một môi trường Python 3.11 sạch để khóa phiên bản đã
  resolve, bao gồm dependency chạy test/lint và dependency gián tiếp. File này hiện
  chưa tồn tại; cài sạch và kiểm tra lại lock trước khi dùng cho final run.
- Không commit `.venv`, cache hoặc package đã cài.

Core workflow dùng deterministic Python rules và MCP evidence. Nếu bổ sung LLM, LLM
chỉ hỗ trợ phân tích intent/claim, không được tạo evidence, amount, entity ID hoặc
policy fact. Model/version phải được khóa, temperature đặt về `0`, output phải được
validate và workflow phải có deterministic fallback khi model lỗi.
Nếu model không hỗ trợ temperature thì ghi cấu hình được hỗ trợ thực tế; temperature
0 không tự bảo đảm tái lập. Khi có LLM, chỉ cam kết tái lập phần nghiệp vụ với cùng
normalized claims/routing input; phải ghi nhận cấu hình model và phiên bản parser.

### 7.2 Concurrency và ordering

- Các case được xử lý tuần tự theo thứ tự trong `case-set.json`; baseline case
  concurrency là `1`.
- Trong mỗi case, task queue dùng priority ổn định:

```text
Order/Item → Payment → Shipment → Policy → Verifier
```

- Handoff task được xếp theo priority rồi `task_id`, không theo thời điểm hoàn thành
  ngẫu nhiên. Chỉ task đủ dependency mới được chạy; Policy chạy khi pha điều tra đã
  kết thúc, Verifier chạy sau khi Coordinator tạo candidate. Priority không thay
  thế dependency gate. Chỉ chọn domain cần thiết theo case, không bắt buộc gọi cả ba.
- Sơ đồ specialist độc lập về logic không bắt buộc thực thi song song.
- Nếu sau này cho phép chạy specialist song song, kết quả phải được sort trước khi
  tổng hợp và trace writer phải chống concurrent writes.

Workflow nghiệp vụ không dùng random. Nếu framework yêu cầu seed, seed cố định là
`0`. Timestamp dùng UTC. Event ID có thể ngẫu nhiên nhưng không được ảnh hưởng quyết
định. Entity, evidence, cause, conflict, refund line và action được sắp xếp ổn định
trước khi ghi output.

### 7.3 Configuration và resource limits

Runtime đọc `COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY` và `MCP_ENDPOINT`
từ environment; `.env` bổ sung các biến chưa có theo `load_dotenv()` hiện tại.
Biến đã có trong shell được ưu tiên, nên sửa `.env` chưa chắc thay đổi giá trị hiệu
lực. Không hard-code key, không ghi secret vào output, trace, log hoặc tài liệu và
không commit `.env`. `.env.example` có endpoint mẫu và key placeholder, không chứa
key thật. Cấu hình thiếu hoặc sai làm run thất bại trước khi xử lý case.

Budget task theo mục 3: tối đa 6 domain task ban đầu, 3 domain task remediation,
Policy/Verifier mỗi agent tối đa 2 task. Retry/deadline theo mục 5: tối đa một retry
mỗi logical call, 60 giây mỗi lần thử trong deadline task tổng 120 giây. Lưu cấu hình
này và version rule/rounding/confidence trong run report ngoài ZIP.
`submission.py` giới hạn mỗi file 1 MiB (bao gồm toàn bộ `trace.jsonl`) và tổng nội
dung chưa nén 12 MiB. Không xóa event bắt buộc để né giới hạn; giữ metadata gọn.

### 7.4 Standard Windows runbook

Khởi tạo môi trường mới hoặc kích hoạt môi trường Python 3.11 đã có:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Cài từ lock sau khi nhóm đã tạo và kiểm tra file đó:

```powershell
python -m pip install -r requirements-lock.txt
python -m pip install -e . --no-deps
```

Trước khi có lock file, cài môi trường phát triển bằng:

```powershell
python -m pip install -e ".[dev]"
```

Trên checkout starter sạch, chưa tải dữ liệu cuộc thi và chưa có output, chạy toàn bộ
test bao gồm kiểm tra release:

```powershell
python -m pytest -q
```

`tests/test_release_safety.py` cố ý yêu cầu không có `case-set.json`, input JSON và
output JSON. Sau khi đã tải dữ liệu, dùng nhóm test runtime riêng; không xóa dữ liệu
để làm release test pass. Với bố cục test hiện tại:

```powershell
python -m pytest -q --ignore=tests/test_release_safety.py
python -m ruff check .
```

Nhóm phải bổ sung workflow tests sử dụng fixture cô lập trước final run; các test
starter hiện có chưa chứng minh coordinator, policy hoặc verifier hoạt động đúng.
Sau khi chuẩn bị `.env`, tải case set chính thức và hoàn tất implementation:

```powershell
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Chạy từng lệnh, chỉ tiếp tục nếu lệnh trước thành công. `day09 run` hiện xóa output
JSON và trace cũ ngay đầu run; lưu artifact cần giữ vào thư mục riêng trước khi chạy
lại. Với batch chưa đủ 100 output được accept, dừng trước bước package.

Với mỗi final run, nhóm lưu ngoài submission: Git commit hash, Python version,
dependency versions, case-set version, thời gian bắt đầu/kết thúc, số case thành
công/thất bại, số MCP call/retry và SHA-256 của ZIP. Không thêm các trường này vào
`manifest.json` vì public schema không cho phép field ngoài contract.

Một run được xem là reproducible khi dùng cùng source commit, dependency lock, case
set, cấu hình rule và MCP data/result hashes, tạo cùng assessment, amount và action;
đồng thời pass nhóm test phù hợp workspace, kiểm tra lifecycle/evidence linkage,
`day09 validate` và packaging. Đây là tiêu chí nghiệm thu cần hiện thực, không phải
cam kết rằng starter hiện tại đã đạt hoặc đã qua các kiểm tra private của scorer.
