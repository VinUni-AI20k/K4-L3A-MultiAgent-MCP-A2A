# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
Input → Coordinator → Specialists → Verifier → Output
                         │              │
                         └── MCP ───────┴── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | TODO | TODO | TODO |
| Order/item | TODO | TODO | TODO |
| Payment | TODO | TODO | TODO |
| Shipment | TODO | TODO | TODO |
| Policy (`policy-agent`) | `CaseState.policy_version`, `primary_issue` | Gọi **duy nhất** `get_policy` (1 lần/case, có cache), tra rule theo `primary_issue`; rule thiếu/sai định dạng → fallback `needs_investigation`, refund 0. Policy **dùng chung cho mọi case**: `party_id` của seller trong rule chỉ là ví dụ, được thay bằng seller thật của case (`CaseState.case_seller_ids()`) hoặc `None` | `PolicyDecision` (status, action, refund, parties) + event `policy_decided` |
| Verifier (`verifier`) | `CaseState` + draft output từ Coordinator | **Không gọi MCP tool.** Kiểm tra hard gate, lọc evidence, đồng bộ status/refund/action/party theo policy, hiệu chuẩn confidence, validate schema | Output cuối + event `verification_completed` (target `coordinator`) |

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

Mô tả message envelope, correlation theo `case_id`, điều kiện handoff, timeout và cách tránh vòng lặp. Chỉ trace sự kiện/decision code quan sát được; không trace nội dung suy luận riêng.

## 4. Evidence lifecycle

Mô tả cách validate MCP response, lưu `evidence_ref`, map evidence vào claim/output và emit `tool_result_consumed`. Evidence không được tái sử dụng giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | TODO | TODO | TODO |
| Not found | TODO | TODO | TODO |
| Source conflict | TODO | TODO | TODO |
| Invalid specialist result | TODO | TODO | TODO |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, evidence ownership, claim linkage, money totals, responsibility/action consistency và confidence bounds.

Cài đặt tại `src/student_agent/agents/verifier_agent.py` (`VerifierAgent.verify`):

| Invariant | Cách kiểm tra | Khi vi phạm |
| --- | --- | --- |
| `case_id` khớp | draft `case_id` phải bằng `CaseState.case_id` | raise `VerificationError` |
| Evidence ownership | ref phải nằm trong `CaseState.evidence` (chỉ có được qua `CaseState.fetch`, luôn truyền đúng `case_id` và đã emit `tool_result_consumed`) | loại bỏ ref (bịa, chéo case, sai kiểu) |
| Evidence precision | domain của ref phải thuộc `RELEVANT_DOMAINS[primary_issue]` | loại bỏ ref lạc đề (vd. `customer`, `product`) |
| Required evidence | mỗi domain trong `REQUIRED_DOMAINS[primary_issue]` (+ `policy` nếu áp rule) phải được trích dẫn | tự thêm ref đã thu thập; nếu chưa từng thu thập → giảm confidence, `VERIFIED_MISSING_EVIDENCE` |
| Claim linkage | `claim_id` phải có trong input; verdict ≠ `insufficient_evidence` phải có ref hợp lệ | bỏ claim lạ; hạ verdict về `insufficient_evidence` |
| Policy consistency | `case_status`, `resolution_actions`, `responsible_parties`, refund lấy từ rule `get_policy` (policy chung cho mọi case) | ghi đè theo policy |
| Money totals | Decimal làm tròn cent; `sum(refund_lines) == recommended_refund_brl`; `no_action` → refund 0, không có line | dựng lại 1 line duy nhất |
| Seller responsibility | seller trong `responsible_parties` phải có trong `affected_entities.seller_ids`; chỉ thêm seller vào `seller_ids` khi evidence của case (`CaseState.case_seller_ids()`: `seller_ids` do coordinator truyền, hoặc `shipment_data.shipping_limits` trong cửa sổ thời gian) có seller đó | seller không có trong evidence → `party_id = None` (`unverified_seller_cleared`); đúng 1 seller → tự điền |
| Confidence bounds | kẹp trong [0.05, 0.95]; trừ điểm khi thiếu evidence, không có rule, có warning hoặc bị loại ref | điều chỉnh giá trị |
| Schema | `Contracts.validate_output` trước khi trả về | raise `ContractError` |

Tinh chỉnh theo leaderboard public (chưa đổi):

- `REQUIRED_DOMAINS` / `RELEVANT_DOMAINS` là giả thuyết; chỉnh theo điểm `evidence`, phối hợp với `adjudicator.ISSUE_DOMAINS` (TV1) vì cả hai cùng tham gia chọn ref.
- Confidence tối đa 0.95 (79/100 case đang ở mức này); nếu điểm `calibration` public thấp, hạ các case có tín hiệu cạnh tranh.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và các giới hạn tài nguyên. Không ghi API key.
