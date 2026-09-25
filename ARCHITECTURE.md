# L3A — Kiến trúc Multi-Agent MCP + A2A

## 1. Mục tiêu thiết kế

Hệ thống điều tra 100 khiếu nại thương mại điện tử bằng dữ liệu có thẩm quyền từ
MCP Evidence Gateway. Customer message chỉ dùng để xác định phạm vi điều tra;
không được dùng làm ground truth. Mọi kết luận phải thỏa đồng thời bốn yêu cầu:

1. đúng nghiệp vụ tại thời điểm case được mở;
2. có evidence thật, đúng case/order/run và liên quan trực tiếp;
3. nhất quán giữa issue, status, entity, trách nhiệm, action và refund;
4. quan sát được quá trình phối hợp agent qua trace.

Workflow được cài đặt bằng Python thuần, không gọi LLM và không phụ thuộc vào
random seed. Các “agent” là vai trò có ownership, input/output và quyền gọi tool
riêng; chúng trao đổi `Finding` qua một evidence ledger dùng chung trong từng case.

```text
Input case
   │
   ▼
Coordinator
   ├──► Order/Item Agent ──► get_order, get_order_items, get_sellers khi cần
   ├──► Payment Agent ─────► payments, payment timeline, refund timeline khi cần
   ├──► Shipment Agent ────► shipment summary
   ├──► Policy Agent ──────► policy đúng version
   └──► Verifier ──────────► schema + provenance + business invariants
                                  │
                                  ▼
                         Output JSON + trace JSONL
```

## 2. Thành phần và ownership

| Thành phần | Trách nhiệm | MCP được phép dùng |
| --- | --- | --- |
| `coordinator` | Nhận case, giao task, kết hợp findings, tạo candidate | Không gọi MCP |
| `order-item-agent` | Xác minh order scope, item và seller liên quan | `get_order`, `get_order_items`, `get_sellers` |
| `payment-agent` | Đối soát charge, split, mismatch, duplicate và refund lifecycle | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` |
| `shipment-agent` | Đánh giá giao trễ và phân biệt seller/logistics | `get_shipment_summary` |
| `policy-agent` | Áp dụng đúng `policy_version`, status, action và refund | `get_policy` |
| `verifier` | Chặn output sai schema, scope hoặc bất biến | Không gọi MCP |
| `EvidenceGateway` | Discovery, input-schema validation và MCP transport | Tất cả tool đã discovery |
| `CaseEvidence` | Ledger evidence riêng cho một case/order | Không tự chọn nghiệp vụ |

Quyền của actor được giới hạn tại call site. Evidence không được cache hoặc tái
sử dụng giữa các case. Coordinator và verifier không thể tự tạo evidence.

## 3. Luồng xử lý một case

1. CLI phát `case_received`; coordinator kiểm tra `claimed_order_id` và
   `opened_at` có timezone.
2. Order/item agent lấy order bắt buộc. Khi order tồn tại, agent lấy item để phục
   vụ invoice, entity và shipping limit.
3. Payment agent lấy cả payment rows và confirmed lifecycle. Refund timeline chỉ
   được lấy khi claim hoặc payment event cho thấy có refund cần điều tra.
4. Shipment agent lấy delivery timeline. Việc gọi shipment ở giai đoạn điều tra
   không đồng nghĩa evidence đó luôn được trích dẫn trong output.
5. Hàm nghiệp vụ thuần chọn `primary_issue` từ facts, không dùng topic của khách
   để quyết định đáp án.
6. Policy agent lấy đúng policy version. Seller profile chỉ được lấy khi cần quy
   trách nhiệm cho seller (`late_delivery_seller`, `unavailable_order_paid`).
7. Coordinator chọn tập evidence liên quan, dựng entities, claim verdict, root
   cause, conflict, action và financial resolution.
8. Verifier kiểm tra toàn bộ invariants. Chỉ khi pass mới phát
   `verification_completed`; CLI sau đó phát `case_finalized`.

## 4. Xử lý thời gian và dữ liệu nhiễu

Tất cả quyết định dùng trạng thái “as of case”: sự kiện phải nằm trong khoảng từ
`order_purchase_timestamp` đến `opened_at`. Event tương lai hoặc thuộc lifecycle
cũ dưới cùng order ID không được cộng vào kết quả hiện tại.

- Các dòng giống hệt nhau được deduplicate trước khi cộng tiền.
- Base payment không có timestamp chỉ được ghép với capture trong cửa sổ thời
  gian khi số tiền khớp một-một.
- Installment không phải split payment. Split hợp lệ cần nhiều
  `payment_sequential` riêng biệt và tổng declared/captured khớp.
- Duplicate được xác định từ marker rõ ràng, nhiều transaction ID của cùng
  payment reference, hoặc nhiều capture bằng nhau vượt invoice đã xác minh.
- `reconciliation_mismatch` là bằng chứng mismatch có thẩm quyền.
- Refund được nhóm theo refund ID khi có; trạng thái mới nhất quyết định kết quả.
  `completed` mới hơn sẽ ghi đè `pending`/`failed` cũ.
- Tiền được tính bằng `Decimal`, không dùng float trong phép đối soát.

## 5. Phân loại shipment và conflict

Seller delay xảy ra khi carrier handoff muộn hơn shipping limit của item/seller.
Logistics delay xảy ra khi seller bàn giao đúng hạn nhưng giao tới khách sau ngày
dự kiến. Không quy trách nhiệm cho tất cả seller chỉ vì cùng thuộc một order.

Thứ tự xử lý conflict:

| Conflict | Nguồn được chọn | Kết quả |
| --- | --- | --- |
| Payment sequence mơ hồ nhưng confirmed lifecycle có timestamp | `get_payment_timeline` | Ghi conflict đã giải quyết, giảm confidence |
| Generic `delivered_late` nhưng delivered/estimated timestamps đầy đủ và đúng hạn | `shipment_timestamps` | Ghi conflict đã giải quyết, tiếp tục kết luận |
| Order và shipment bất đồng trực tiếp về status/timestamp | Không chọn | `insufficient_evidence` |
| Event actor và shipping-limit attribution mâu thuẫn | Không chọn | `insufficient_evidence` |

Conflict đã giải quyết luôn có `selected_source`; conflict chưa giải quyết có
`selected_source: null`. Không âm thầm bỏ qua hoặc chọn nguồn tùy ý.

## 6. Ma trận evidence của output

Gateway có thể thu thập nhiều dữ liệu để điều tra, nhưng output chỉ trích dẫn
nhóm thực sự hỗ trợ kết luận. Hai payment tool là hai evidence records độc lập.

| Primary issue | Evidence được chọn trong output |
| --- | --- |
| `canceled_order_paid` | order, order payments, payment timeline, policy |
| `unavailable_order_paid` | order, items, payments, payment timeline, sellers, policy |
| `late_delivery_seller` | order, items, payments, payment timeline, shipment, sellers, policy |
| `late_delivery_logistics` | order, items, payments, payment timeline, shipment, policy |
| `valid_split_payment` | order, items, payments, payment timeline, policy |
| `payment_mismatch` | order, payments, payment timeline, policy |
| `duplicate_charge` | order, items, payments, payment timeline, policy |
| `refund_pending` / `refund_failed` | order, payments, payment timeline, refund timeline, policy |
| `unsupported_claim` | order, payments, payment timeline, shipment, policy |
| `insufficient_evidence` | Evidence đã thu thập cần để mô tả thiếu hụt/conflict |

Evidence ở cấp claim tiếp tục được lọc để cân bằng coverage và precision:

- claim giao trễ dùng order, item, shipment, policy và seller nếu quy trách nhiệm
  seller; payment evidence không được thêm vào chỉ để tăng số ref;
- claim tài chính/refund dùng toàn bộ causal chain đã chọn, vì verdict phụ thuộc
  cả nguyên nhân, payment state và policy remedy;
- mọi claim ref phải là tập con của output refs và phải có
  `tool_result_consumed` tương ứng trong trace.

`affected_entities` chỉ được dựng từ evidence đã trích dẫn. Seller trong
`responsible_parties` phải nằm trong seller entities của chính case đó.

## 7. Policy và financial resolution

Policy được gọi cho từng case bằng đúng `policy_version`. Rule quyết định
`case_status`, `recommended_action`, refund và loại responsible party. Giá trị
refund không được suy đoán từ lời khách hàng và không được vượt capture còn lại
sau các refund đã hoàn tất.

- `no_action` luôn có refund bằng 0;
- `action_required` phải có action thực thi;
- tổng `refund_lines` phải bằng `recommended_refund_brl`;
- entity của refund line phải nằm trong affected entities;
- `insufficient_evidence` chỉ cho phép `needs_investigation`, refund 0 và action
  yêu cầu bổ sung evidence.

## 8. Provenance và trace A2A

Mỗi MCP response phải pass envelope schema, đúng domain và đúng order/case scope.
Ledger giữ nguyên `evidence_ref`; ref dùng chéo case hoặc chéo tool bị từ chối.

Trace tối thiểu của mỗi case gồm:

```text
case_received
  → task_assigned
  → tool_result_consumed
  → handoff
  → policy_decided
  → verification_completed
  → case_finalized
```

Một case có nhiều lần `task_assigned`, `tool_result_consumed` và `handoff`, tương
ứng với các specialist. Trace chỉ ghi actor, target, decision code, tool name,
evidence refs và thuộc tính kết quả cần audit; không ghi API key, prompt, chain of
thought hoặc nguyên văn lỗi server.

## 9. Failure policy và atomic execution

| Tình huống | Xử lý |
| --- | --- |
| Timeout/transport error | Tối đa 2 lần, 45 giây/lần, backoff 0,25 giây |
| Required order hoặc policy không lấy được | Dừng run; không tạo fallback giả |
| Optional refund evidence không lấy được | Ghi failure; case cần điều tra nếu không thể xác minh refund |
| Evidence sai schema/domain/scope | Reject ngay, không finalize |
| Policy thiếu rule hoặc rule không hợp lệ | `insufficient_evidence`, refund 0 |
| Conflict không có nguồn ưu tiên | `insufficient_evidence` |

CLI chạy tối đa hai case song song, nhưng mỗi case có ledger độc lập. Toàn bộ 100
case được tạo trong staging directory. Chỉ sau khi tất cả case pass validator,
outputs và trace mới atomically thay bộ cũ; bộ cũ được sao lưu dưới
`dist/previous-runs/`.

Trước mỗi lần chạy, CLI tạo/gia hạn L3A run bằng `POST /api/v2/runs`, rồi kiểm tra
variant, case-set version và MCP endpoint. Tạo run không phải upload và không dùng
lượt nộp.

## 10. Verifier và quality gates

Verifier trong workflow kiểm tra:

- output schema, `case_id` và evidence ownership;
- claim refs là tập con của output refs;
- entity ID thực sự xuất hiện trong evidence được trích dẫn;
- refund total, refund lines, action và status nhất quán;
- seller responsibility nằm trong seller entities;
- conflict source hợp lệ và ranked causes liên tiếp.

`day09 validate` kiểm tra thêm đủ 100 output, lifecycle trace, receive/finalize
ordering, actor collaboration, evidence-to-trace linkage và không có ref dùng
chéo case. `day09 package` chỉ đóng gói:

```text
manifest.json
trace.jsonl
outputs/L3A_CASE_001.json
...
outputs/L3A_CASE_100.json
```

Input, source code, `.env`, API key và debug logs không được đưa vào ZIP.

## 11. Calibration và tái lập

Confidence phản ánh độ mạnh của evidence, không phản ánh lời khẳng định của khách:

| Điều kiện | Confidence tối đa |
| --- | ---: |
| Evidence trực tiếp, đầy đủ, không conflict | 0,96 |
| Unsupported claim với negative evidence đầy đủ | 0,95 |
| Duplicate suy luận từ capture + invoice | 0,94 |
| Conflict đã giải quyết theo quy tắc nguồn | 0,90 |
| Evidence có warning | 0,78 |
| Thiếu evidence hoặc conflict chưa giải quyết | 0,45 |

Các ngưỡng này là heuristic cố định, không fit trên private labels và không dùng
case ID làm shortcut.

Môi trường yêu cầu Python 3.11+. Dependency snapshot nằm trong
`requirements-lock.txt`. Unit test dùng fixture tổng hợp và evidence ref giả chỉ
trong thư mục pytest tạm; ref giả không bao giờ đi vào artifacts nộp bài.

```bash
source .venv/bin/activate
pytest -q
ruff check src tests
day09 validate-inputs
day09 run
day09 validate
day09 package --output dist/submission.zip
```

CLI không tự upload để tránh sử dụng nhầm quota submission có giới hạn.
