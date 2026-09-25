# Phụ lục tối ưu điểm — dùng kèm bảng phân công cũ

> **Không đổi ai làm gì.** Bảng phân công cũ vẫn giữ nguyên. File này bổ sung những thứ bảng cũ còn thiếu: tên tool MCP thật, "hợp đồng" trả kết quả cho coordinator, và cách tinh chỉnh để tăng điểm.
> Code tham chiếu: `main` (đã tích hợp code của cả 5 người).

---

## 0. Đọc trước: những gì dữ liệu MCP thật cho thấy (25/09)

Lấy mẫu 10 case (mỗi topic 1 case), gọi đủ tool. Có 3 phát hiện quyết định điểm:

**a) Dữ liệu có bản ghi "nhiễu" ngoài dòng thời gian của case.** Mỗi tool trả cả bản ghi thật của case lẫn bản ghi nằm **trước ngày mua** hoặc **sau `opened_at`**. Ví dụ:
- refund `failed` từ 3 tháng trước;
- sự kiện `delivered_late` xảy ra sau khi case đã mở;
- khoản `captured` thuộc về một đơn khác thời điểm.

→ Chỉ tin bản ghi có thời gian trong khoảng `[order_purchase_timestamp − 1 ngày, opened_at]`. Khi lọc như vậy, evidence khớp đúng issue ở **10/10 case**; nếu không lọc thì bị lừa ở ít nhất 5/10 case.

**b) Policy là chung cho mọi case.** `get_policy` trả các `rules` theo từng issue: `case_status`, `recommended_action`, `refund_brl`, `responsible_parties`. Chọn đúng issue thì tiền, hành động và trạng thái lấy thẳng từ rule. Lưu ý: `party_id` của seller trong rule chỉ là **ví dụ**, phải thay bằng seller thật của case.

**c) Mỗi issue có dấu hiệu tường minh (trong khoảng thời gian trên):**

| Issue | Dấu hiệu |
| --- | --- |
| `canceled_order_paid` / `unavailable_order_paid` | `get_order.order_status` = `canceled` / `unavailable` + có `captured` |
| `refund_pending` / `refund_failed` | `get_refund_timeline.events[].status` = `pending` / `failed` |
| `payment_mismatch` | `get_payment_timeline.events[]` có `reconciliation_mismatch` |
| `duplicate_charge` | ≥ 2 lần `captured` cùng số tiền, tổng > giá trị đơn (price + freight) |
| `valid_split_payment` | ≥ 2 lần `captured`, tổng = giá trị đơn |
| `late_delivery_seller` | `delivered_customer_at > estimated_delivery_at` **và** `delivered_carrier_at > shipping_limit_at` |
| `late_delivery_logistics` | giao trễ nhưng seller bàn giao đúng hạn |
| `unsupported_claim` | đã kiểm tra order + payment + shipment mà không thấy vấn đề |

Cài đặt: `src/student_agent/agents/evidence_rules.py` (TV1).

### Tên field thật (code hiện tại đang đoán sai)

| Tool | Cấu trúc `data` thật |
| --- | --- |
| `get_order` | dict: `order_status`, `order_purchase_timestamp`, `order_delivered_carrier_date`, `order_delivered_customer_date`, `order_estimated_delivery_date`, `customer_id` (**không có** `customer_unique_id`) |
| `get_order_items` | list: `order_item_id`, `product_id`, `seller_id`, `shipping_limit_date`, `price`, `freight_value` (chuỗi số) |
| `get_order_payments` | list: `payment_sequential`, `payment_type`, `payment_installments`, `payment_value` (không có ngày) |
| `get_payment_timeline` | dict: `payments` (như trên) + `events[]`: `event_at`, `event_type` (`captured`, `reconciliation_mismatch`), `amount_brl`, `status` |
| `get_refund_timeline` | dict: `events[]`: `event_at`, `event_type=refund_requested`, `amount_brl`, `status`. **Tool trả lỗi khi đơn không có refund**, nên phải bắt exception |
| `get_shipment_summary` | dict: `order_status`, `delivered_carrier_at`, `delivered_customer_at`, `estimated_delivery_at`, `shipping_limits[]` (`seller_id`, `shipping_limit_at`), `events[]` (`event_type=delivered_late`, `actor`) |
| `get_sellers` | list: `seller_id`, `seller_city`, `seller_state` — tham số là **`order_id`** |

### Việc từng người nên sửa trong file của mình

Coordinator đã có adapter nên pipeline vẫn chạy đúng khi chưa sửa. Sửa xong thì tín hiệu của từng agent sẽ khớp với evidence rules.

- **TV2 (order_agent.py):**
  - `get_product_context` cần `order_id`, không phải `product_id`.
  - `get_customer_history` cần `customer_unique_id`, mà `get_order` không trả trường này, nên bỏ lời gọi đó.
  - Còn 4 dòng quá 100 ký tự (CI đỏ).
- **TV3 (shipment_agent.py):**
  - Đổi tên field theo bảng trên (`delivered_carrier_at`, `estimated_delivery_at`, `shipping_limits[].shipping_limit_at`...).
  - `get_sellers` cần `order_id`.
  - Lọc bản ghi theo khoảng thời gian của case.
- **TV4 (payment_agent.py):**
  - Timeline và refund nằm trong key `events`, không phải `timeline`/`refunds`; số tiền ở `amount_brl`.
  - Bọc `get_refund_timeline` bằng try/except.
  - `test_floating_point_sum_is_exact` đang fail vì cộng float.
- **TV5:** policy/verifier dùng tốt. Coordinator đã thay seller ví dụ trong policy bằng seller thật trước khi gọi `verify`.

---

## 1. Tên tool MCP thật (bảng cũ đoán sai tên)

Lấy từ `day09 mcp-tools` ngày 25/09. **Mọi tool đều cần `case_id`.** Gọi qua `self.call_tool(tool, case_id, ...)`; hàm này tự ghi trace và tự lưu evidence.

| Tool | Tham số (ngoài `case_id`) | Domain trả về | Owner (bảng cũ) |
| --- | --- | --- | --- |
| `get_order` | `order_id` | order | TV2 |
| `get_order_items` | `order_id` | item | TV2 |
| `get_product_context` | `order_id` | product | TV2 |
| `get_customer_history` | `customer_unique_id` | customer | TV2 |
| `get_shipment_summary` | `order_id` | shipment | TV3 |
| `get_sellers` | `order_id` | seller | TV3 |
| `get_order_payments` | `order_id` | payment | TV4 |
| `get_payment_timeline` | `order_id` | payment | TV4 |
| `get_refund_timeline` | `order_id` | refund | TV4 |
| `get_policy` | `policy_version` | policy | TV5 |

- Không có `get_payments`, `get_refunds`, `get_items`, `get_shipment`, `get_seller` như bảng cũ ghi.
- File logistics trong code tên là **`shipment_agent.py`**, không phải `logistics_agent.py`.
- `get_customer_history` và `get_product_context` hiếm khi cần. Chỉ gọi khi thật sự phục vụ kết luận, vì trích dẫn domain không liên quan sẽ bị trừ điểm evidence.

---

## 2. Mỗi specialist cần thêm đúng 1 key: `issues`

Coordinator **không tự đoán** issue. Nó chọn `primary_issue` từ các tín hiệu specialist gửi lên. Specialist nào không trả `issues` thì coi như domain đó không tìm thấy gì.

```python
from .base import BaseAgent

class ShipmentAgent(BaseAgent):
    async def run(self, case_id, context):
        order_id = context["order_id"]
        ship = await self.call_tool("get_shipment_summary", case_id, order_id=order_id)
        sellers = await self.call_tool("get_sellers", case_id, order_id=order_id)
        issues = []
        if seller_handed_over_late(ship["data"]):              # logic của bạn
            issues.append(self.signal(
                "late_delivery_seller", 0.9,
                [ship["evidence_ref"], sellers["evidence_ref"]],  # CHỈ ref chứng minh
            ))
        self.emit_handoff(case_id, target="coordinator")
        return {"shipment_ids": [...], "seller_ids": [...], "issues": issues}
```

### Thang `strength` (dùng thống nhất cả nhóm, vì ảnh hưởng điểm calibration)

| strength | Khi nào |
| ---: | --- |
| 0.9 | Evidence trực tiếp và rõ ràng (vd hai giao dịch trùng số tiền, trùng thời điểm) |
| 0.7 | Suy luận từ nhiều nguồn khớp nhau |
| 0.4–0.5 | Có dấu hiệu nhưng dữ liệu thiếu hoặc mâu thuẫn |
| — | Không có evidence → **không** gửi tín hiệu (dưới 0.3 bị bỏ qua) |

### `unsupported_claim` — ai gửi?

Khách khai một topic thuộc domain của bạn (xem `context["claim_topics"]`) nhưng evidence **bác bỏ** → gửi `unsupported_claim` kèm ref chứng minh.
Ví dụ: khách khai `late_delivery_seller` nhưng seller bàn giao đúng hạn và khách nhận đúng hạn → TV3 gửi `signal("unsupported_claim", 0.85, [ship_ref])`.

> Claim của khách chỉ là **giả thuyết cần kiểm tra trước**. Adjudicator chỉ cộng +0.1 cho tín hiệu khớp với claim, và không bao giờ kết luận nếu không có tín hiệu.

---

## 3. Coordinator đọc những key nào

| Agent | Key trả về (ngoài `issues`) | Ghi chú |
| --- | --- | --- |
| order (TV2) | `order_ids`, `item_ids`, `seller_ids`, `claim_assessments` *(tùy chọn)* | Không có `claim_assessments` thì coordinator tự sinh từ quyết định |
| payment (TV4) | `payment_references`, `financial_resolution` | Chỉ cần `refund_lines` đúng; tổng tiền guard tự tính |
| shipment (TV3) | `shipment_ids`, `seller_ids`, `root_cause_analysis` hoặc `responsible_parties` | RCA của shipment được ưu tiên hơn RCA của policy |
| policy (TV5) | `resolution_actions`, `data_conflicts`, `assessment` *(chỉ dùng khi không ai gửi `issues`)* | Chạy **sau** adjudicator, đọc `context["decision"]` |
| verifier (TV5) | trả về **toàn bộ** output dict | Không được bỏ key nào |

Specialist nào cũng có thể trả `data_conflicts` (coordinator sẽ gộp lại).

**`context` có sẵn:** `case_id`, `order_id`, `claims`, `claim_topics`, `policy_version`, `opened_at`, `seller_ids`/`item_ids` (sau khi order chạy xong), `order_result`, `payment_result`, `shipment_result`, và `decision` (`primary_issue`, `case_status`, `confidence`; chỉ có từ lúc policy chạy).

**Thứ tự chạy:** order → payment → shipment → adjudicator → policy → verifier → guard.

---

## 4. TV1 đã làm sẵn — các bạn KHÔNG cần làm lại

| Rủi ro | Đã xử lý ở | Cách xử lý |
| --- | --- | --- |
| Ref giả / ref của case khác (hard gate) | `base.EvidenceLedger`, `guard` | Output chỉ giữ ref mà MCP đã trả cho **chính case đó** |
| Trích dẫn thừa (evidence F1) | `adjudicator.ISSUE_DOMAINS` | Chỉ trích ref thuộc domain liên quan tới issue đã chọn + ref gắn trong tín hiệu |
| Sai `case_id`, lỗi schema | `guard`, `coordinator.finalize` | Tự sửa; nếu vẫn fail schema thì trả output `insufficient_evidence` an toàn |
| 1 case crash làm dừng batch | `workflow.solve_case`, `coordinator._dispatch` | Specialist lỗi được cô lập và ghi `agent_error` vào trace |
| Tổng tiền lệch / sai số float | `guard.money` | Dùng `Decimal`, làm tròn 2 chữ số, tổng = Σ `refund_lines` |
| Hoàn tiền dù `no_action` | `guard` | Không phải `action_required` → refund = 0 |
| Lỗi seller nhưng thiếu seller trong RCA | `guard` | Tự thêm seller từ `seller_ids` |
| Trace thiếu event | coordinator | `task_assigned`, `policy_decided`, `verification_completed` đã có sẵn |
| MCP timeout | `BaseAgent.call_tool` | Retry tối đa 2 lần, chỉ khi lỗi mạng |

→ **TV5** nên tập trung vào **ngữ nghĩa**: `resolution_actions` theo policy, `data_conflicts`, quy tắc confidence. Phần format và gate đã có guard lo.

---

## 5. Bảng evidence theo issue (có thể tinh chỉnh)

Nằm trong `src/student_agent/agents/adjudicator.py → ISSUE_DOMAINS`. Nếu kết luận là `action_required` thì trích thêm `policy`.

| primary_issue | Domain được trích dẫn | Owner gửi tín hiệu (bảng cũ) |
| --- | --- | --- |
| `canceled_order_paid` | order, payment | TV2 |
| `unavailable_order_paid` | order, item, payment | TV2 |
| `late_delivery_seller` | order, shipment, seller | TV3 |
| `late_delivery_logistics` | order, shipment | TV3 |
| `valid_split_payment` | order, payment | TV4 |
| `payment_mismatch` | order, item, payment | TV4 |
| `duplicate_charge` | order, payment | TV4 |
| `refund_pending` | order, payment, refund | TV4 |
| `refund_failed` | order, payment, refund | TV4 |
| `unsupported_claim` | order + ref trong tín hiệu | owner của domain bị khai sai |

`case_status` mặc định: `valid_split_payment` và `unsupported_claim` → `no_action`; `insufficient_evidence` → `needs_investigation`; còn lại → `action_required`. Muốn khác thì truyền `case_status=` trong `self.signal(...)`.

---

## 6. Vòng tinh chỉnh bằng điểm public

Trước khi finalize, workspace hiện **điểm từng thành phần** của phần public. Mỗi lần nộp, xem thành phần nào thấp rồi chỉnh đúng chỗ:

| Thành phần thấp | Chỉnh ở đâu | Ai |
| --- | --- | --- |
| `semantic` | logic tín hiệu của specialist; `PRIORITY` trong adjudicator | TV2–4, TV1 |
| `evidence` | `ISSUE_DOMAINS`; bớt ref thừa trong tín hiệu | TV1 |
| `calibration` | thang strength; `CLOSE_MARGIN`, `INSUFFICIENT_CONFIDENCE` | TV1, TV5 |
| `consistency` | `guard.enforce_invariants`; `resolution_actions` | TV1, TV5 |
| `provenance` / `schema` / `workflow` | lẽ ra phải ~100%; nếu thấp thì báo TV1 ngay | TV1 |

**Lưu ý:**
- Phần private chiếm **80%** điểm cuối. Chỉ chỉnh quy tắc nghiệp vụ, **không** viết `if case_id == ...`.
- Bài nộp cuối phải từ **một lần `day09 run` sạch**, không ghép output từ nhiều lần chạy.
- Mỗi lần chạy thật đều bị audit. Không chạy thử liên tục cho vui.

---

## 7. Checklist khi mở PR (mỗi thành viên)

- [ ] Chỉ gọi tool trong bảng mục 1, qua `self.call_tool`
- [ ] Trả `issues` bằng `self.signal(...)`; mỗi tín hiệu chỉ gắn ref chứng minh nó
- [ ] Không kết luận từ `claims[].topic` hay `message` khi không có evidence
- [ ] Không `raise` vì dữ liệu thiếu: thiếu thì không gửi tín hiệu (hoặc strength thấp)
- [ ] `ruff check .` sạch; test của mình pass với gateway giả (xem `tests/test_tv1_workflow.py::FakeGateway`)

### Lệnh chạy trên Windows

```bash
.venv/Scripts/ruff.exe check .
```

```bash
.venv/Scripts/python.exe -m pytest -q --deselect tests/test_release_safety.py::test_repository_contains_no_competition_payload
```

```bash
.venv/Scripts/day09.exe run
```

```bash
.venv/Scripts/day09.exe validate
```

> `test_release_safety` fail ở máy local là **bình thường** nếu có `case-set.json` trong thư mục. File này nằm trong `.gitignore` nên CI vẫn xanh.
