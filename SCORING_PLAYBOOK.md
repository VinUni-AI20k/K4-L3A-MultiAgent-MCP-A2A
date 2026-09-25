# Phụ lục tối ưu điểm — dùng kèm bảng phân công cũ

> **Không đổi ai làm gì.** Bảng phân công cũ vẫn giữ nguyên. File này bổ sung những thứ bảng cũ còn thiếu: tên tool MCP thật, "hợp đồng" trả kết quả cho coordinator, và cách tinh chỉnh để tăng điểm.
> Code tham chiếu: branch `feat/tv1-coordinator-trace` (TV1).

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
