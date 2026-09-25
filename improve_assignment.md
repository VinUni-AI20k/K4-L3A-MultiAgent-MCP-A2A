# Hướng dẫn cải thiện bài cho từng thành viên

> Viết ngày 25/09, sau khi TV1 tích hợp code của cả 5 người (`feat/tv1-integration`) và chạy thật trên 100 case.
> **Kết quả hiện tại: 100/100 case chọn đúng issue, `day09 validate` OK.**
>
> Pipeline đã đúng là nhờ coordinator (TV1) bọc code của mọi người bằng adapter và dùng thêm bộ quy tắc `evidence_rules.py`. Bản thân từng agent vẫn còn lỗi khi chạy với dữ liệu thật. Hiện tín hiệu của các agent chỉ được tính trọng số thấp (0.5), vì chúng hay sai.
> Sửa xong phần của mình thì tín hiệu của bạn sẽ khớp với evidence, và hệ thống vững hơn trên phần **private (80% điểm)**.

---

## Phần chung — ai cũng cần đọc

### 1. Dữ liệu có bản ghi "nhiễu" ngoài dòng thời gian của case

Mỗi tool MCP trả cả bản ghi thật của case lẫn bản ghi **nằm ngoài khoảng thời gian của case**. Ví dụ thật từ case 001 (mua 2017-12-20, mở case 2018-01-01):

| Tool | Bản ghi thật | Bản ghi nhiễu |
| --- | --- | --- |
| `get_order_items` | `shipping_limit_date` 2017-12-23 | cùng `order_item_id`, `shipping_limit_date` 2018-05-14 |
| `get_payment_timeline` | `captured` 79.00 ngày 2017-12-20 | `captured` 18.00 ngày 2018-05-11 |
| `get_shipment_summary.events` | — | `delivered_late` (seller) ngày 2018-05-25 |

Case 005 có refund `failed` ngày 2018-01-18, trong khi đơn mua ngày 2018-04-23. Nếu không lọc, agent sẽ kết luận nhầm là `refund_failed`.

**Quy tắc:** chỉ dùng bản ghi có thời gian trong khoảng

```text
[order_purchase_timestamp − 1 ngày,  opened_at]
```

- `order_purchase_timestamp` lấy từ `get_order.data`.
- `opened_at` lấy từ input case.
- Tên trường thời gian theo từng tool: items → `shipping_limit_date`; payment/refund/shipment events → `event_at`; `shipping_limits` → `shipping_limit_at`.

Hàm dùng chung đã có sẵn trong `src/student_agent/agents/evidence_rules.py`:

```python
from .evidence_rules import parse_ts, WINDOW_SLACK

start = parse_ts(order["order_purchase_timestamp"]) - WINDOW_SLACK
end = parse_ts(case["opened_at"])
def in_window(value: str | None) -> bool:
    ts = parse_ts(value)
    return ts is not None and start <= ts <= end
```

### 2. Cấu trúc `data` thật của từng tool

| Tool | Tham số | `data` thật |
| --- | --- | --- |
| `get_order` | `order_id` | **dict**: `order_id`, `customer_id`, `order_status` (`delivered`/`canceled`/`unavailable`), `order_purchase_timestamp`, `order_approved_at`, `order_delivered_carrier_date`, `order_delivered_customer_date`, `order_estimated_delivery_date`. **Không có `customer_unique_id`.** |
| `get_order_items` | `order_id` | **list**: `order_item_id`, `product_id`, `seller_id`, `shipping_limit_date`, `price`, `freight_value`. Số là **chuỗi** (`"79.00"`) |
| `get_order_payments` | `order_id` | **list**: `payment_sequential`, `payment_type`, `payment_installments`, `payment_value`. **Không có ngày, không có id giao dịch** |
| `get_payment_timeline` | `order_id` | **dict**: `payments` (giống trên) + `events[]`: `event_at`, `event_type` (`captured` \| `reconciliation_mismatch`), `amount_brl`, `status` |
| `get_refund_timeline` | `order_id` | **dict**: `events[]`: `event_at`, `event_type=refund_requested`, `amount_brl`, `status` (`pending` \| `failed`). **Trả lỗi nếu đơn không có refund**, nên phải `try/except` |
| `get_shipment_summary` | `order_id` | **dict**: `order_status`, `delivered_carrier_at`, `delivered_customer_at`, `estimated_delivery_at`, `shipping_limits[]` (`order_item_id`, `seller_id`, `shipping_limit_at`), `events[]` (`event_at`, `event_type=delivered_late`, `actor`, `status`) |
| `get_sellers` | **`order_id`** | **list**: `seller_id`, `seller_zip_code_prefix`, `seller_city`, `seller_state` |
| `get_product_context` | **`order_id`** | list sản phẩm và danh mục. **Không cần cho issue nào**, nên không nên gọi |
| `get_customer_history` | `customer_unique_id` | Không lấy được id này từ dữ liệu, nên **đừng gọi** |
| `get_policy` | `policy_version` | **dict**: `rules[primary_issue]` gồm `case_status`, `recommended_action`, `refund_brl`, `responsible_parties` |

### 3. Dấu hiệu của từng issue (chỉ xét bản ghi trong khoảng thời gian case)

| Issue | Dấu hiệu | Owner |
| --- | --- | --- |
| `canceled_order_paid` | `order_status == "canceled"` + có `captured` | TV2 |
| `unavailable_order_paid` | `order_status == "unavailable"` + có `captured` | TV2 |
| `late_delivery_seller` | `delivered_customer_at > estimated_delivery_at` **và** `delivered_carrier_at > shipping_limit_at` | TV3 |
| `late_delivery_logistics` | giao trễ, nhưng `delivered_carrier_at <= shipping_limit_at` | TV3 |
| `refund_pending` / `refund_failed` | refund event mới nhất có `status` = `pending` / `failed` | TV4 |
| `payment_mismatch` | có event `reconciliation_mismatch` | TV4 |
| `duplicate_charge` | ≥ 2 lần `captured` **cùng số tiền**, tổng > giá trị đơn (Σ price + freight của item trong khoảng thời gian) | TV4 |
| `valid_split_payment` | ≥ 2 lần `captured`, tổng = giá trị đơn | TV4 |
| `unsupported_claim` | đã xem order, payment, shipment mà không có dấu hiệu nào ở trên | cả nhóm |

### 4. Coordinator gọi code của bạn như thế nào — ĐỪNG đổi chữ ký hàm khi chưa báo TV1

| Agent | Coordinator gọi | Coordinator đọc |
| --- | --- | --- |
| TV2 | `OrderAgent(gateway, trace)` → `await agent.run(case, state)` | `state["order_data"]`, `state["claim_assessments"]` (`claim_id`, `verdict`) |
| TV3 | `ShipmentAgent(gateway, trace)` → `await agent.investigate(case_id, order_id, [], opened_at)` | `.primary_issue_candidate`, `.evidence_refs` |
| TV4 | `PaymentAgent()` → `await agent.investigate(case_id=, order_id=, gateway=, trace=, order_status=)` | `.detected_issue`, `.evidence_refs` |
| TV5 | `PolicyAgent().load(state, gateway, trace)`, `.decide(state, issue, trace)`, `VerifierAgent(policy).verify(state, draft, trace)` | output cuối |

- `gateway` truyền vào là `RecordingGateway`: mọi evidence bạn lấy đều được ghi lại, nên **bạn không cần tự gom `evidence_refs` cho output**.
- Vẫn phải tự emit `tool_result_consumed` sau mỗi lần gọi tool, như code hiện tại đang làm.
- Nếu agent ném exception, coordinator bắt lỗi và ghi `agent_error` vào trace. Case vẫn ra output, nhưng tín hiệu của bạn bị mất.

### 5. Cách test với dữ liệu có cấu trúc thật

- `tests/test_tv1_workflow.py` có `base_data()` và `FakeGateway`: dữ liệu **tổng hợp** nhưng đúng cấu trúc thật, kèm bản ghi nhiễu. `FakeGateway` báo lỗi khi gọi sai tham số, giống server thật.
- **Không commit response MCP thật vào repo.** Đó là dữ liệu thi đấu, và `test_release_safety` sẽ bắt lỗi.

```bash
.venv/Scripts/python.exe -m pytest -q --deselect tests/test_release_safety.py::test_repository_contains_no_competition_payload
```

```bash
.venv/Scripts/ruff.exe check .
```

---

## 👤 TV2 — Khánh — `src/student_agent/agents/order_agent.py`

**Tình trạng:** đọc `order_status` đúng field, và nhận diện đúng `canceled` / `unavailable`. Còn 7 vấn đề:

| # | Vấn đề | Hậu quả | Cách sửa |
| --- | --- | --- | --- |
| 1 | `get_product_context(..., product_id=...)` gọi **trong vòng lặp từng item** | Server báo `order_id Field required`, và mỗi case có vài lần gọi lỗi bị audit | **Xóa hẳn** (domain `product` không cần cho issue nào; trích dẫn nó còn bị trừ điểm evidence) |
| 2 | `get_customer_history(..., customer_id=...)` | Tool cần `customer_unique_id`, mà dữ liệu không có trường này, nên luôn lỗi | **Xóa hẳn** |
| 3 | Không lọc theo thời gian | Mỗi đơn có 2 dòng item trùng `order_item_id`, trong đó 1 dòng là nhiễu | Chỉ giữ item có `shipping_limit_date` trong khoảng thời gian case |
| 4 | `claim_assessments` dùng `confidence = 1.0` | Điểm calibration tính `1 − (đúng − conf)²`, nên sai một lần là mất trọn | Dùng 0.9 khi có evidence rõ ràng |
| 5 | Topic `unsupported_claim` luôn cho `verdict="unsupported"`, `1.0` mà không có evidence | Verdict không dựa trên dữ liệu | Bỏ nhánh này; coordinator tự đánh giá claim từ quyết định cuối |
| 6 | Topic khác `canceled`/`unavailable` bị bỏ qua, kể cả khi đơn thật sự bị hủy | Bỏ sót tín hiệu | Luôn báo khi `order_status` là `canceled`/`unavailable`, bất kể khách khai gì |
| 7 | Dùng `print(...)` để báo lỗi, không emit `handoff` | Log bẩn; coordinator đang phải emit `handoff` thay | Bỏ `print`; cuối `run()` emit `handoff` tới `coordinator` |

**Gợi ý code** (giữ nguyên chữ ký `run(case, state)`):

```python
async def run(self, case: dict[str, Any], state: dict[str, Any]) -> None:
    case_id = case["case_id"]
    order_id = case["customer_request"].get("claimed_order_id")
    if not order_id:
        return
    order_ev = await self._fetch("get_order", case_id, order_id=order_id)
    items_ev = await self._fetch("get_order_items", case_id, order_id=order_id)
    order = (order_ev or {}).get("data") or {}
    state["order_data"] = order

    start = parse_ts(order.get("order_purchase_timestamp"))
    end = parse_ts(case.get("opened_at"))
    items = [
        i for i in ((items_ev or {}).get("data") or [])
        if start and end and start - WINDOW_SLACK <= (parse_ts(i.get("shipping_limit_date")) or end + WINDOW_SLACK) <= end
    ]
    state["items_in_window"] = items

    status = order.get("order_status")
    topics = {c["claim_id"]: c["topic"] for c in case["customer_request"].get("claims", [])}
    for claim_id, topic in topics.items():
        if topic not in ("canceled_order_paid", "unavailable_order_paid"):
            continue
        expected = "canceled" if topic == "canceled_order_paid" else "unavailable"
        state.setdefault("claim_assessments", []).append({
            "claim_id": claim_id,
            "verdict": "supported" if status == expected else "unsupported",
            "confidence": 0.9 if order_ev else 0.3,
            "evidence_refs": [order_ev["evidence_ref"]] if order_ev else [],
        })
    self.trace.emit(case_id=case_id, event_type="handoff", actor="order-agent",
                    target="coordinator", decision_code=f"ORDER_{(status or 'unknown').upper()}")
```

`_fetch` = `gateway.call` bọc `try/except`, kèm emit `tool_result_consumed` như code hiện tại.

**DoD của TV2:** không còn lời gọi tool nào bị lỗi do sai tham số; 10 case topic canceled/unavailable cho verdict `supported`.

---

## 👤 TV3 — vxtor012 — `src/student_agent/agents/shipment_agent.py`

**Tình trạng:** logic so mốc thời gian đúng hướng (bàn giao trễ → lỗi seller, giao trễ → lỗi vận chuyển). Nhưng **đọc sai tên field nên chưa bao giờ phát hiện được giao trễ** trên dữ liệu thật.

| # | Vấn đề | Code hiện tại đọc | Field thật |
| --- | --- | --- | --- |
| 1 | Hạn seller bàn giao | `shipping_limit_date` / `seller_shipping_limit_date` / `limit_date` ở cấp gốc | `data["shipping_limits"][i]["shipping_limit_at"]` (list, cần lọc theo thời gian rồi lấy **min**) |
| 2 | Ngày bàn giao cho hãng vận chuyển | `order_delivered_carrier_date` / `carrier_handover_date` | `data["delivered_carrier_at"]` |
| 3 | Ngày dự kiến giao | `order_estimated_delivery_date` | `data["estimated_delivery_at"]` |
| 4 | Ngày khách nhận | `order_delivered_customer_date` | `data["delivered_customer_at"]` |
| 5 | Seller id | `data["seller_id"]` / `data["seller_ids"]` | `data["shipping_limits"][i]["seller_id"]` |
| 6 | `get_sellers(..., seller_id=s_id)` | — | Tool cần **`order_id`**; gọi **1 lần** mỗi đơn |
| 7 | `party_id=carrier_name` với giá trị mặc định là chuỗi `"logistics_provider"` | — | Dùng `None` (dữ liệu không có tên hãng vận chuyển) |
| 8 | Không lọc theo thời gian | — | `shipping_limits` có dòng nhiễu (vd hạn 2018-05-14 cho đơn mua 2017-12-20). `events` có `delivered_late` nhiễu sau `opened_at`, và **đừng tin `actor` của event nhiễu** |
| 9 | Đơn `canceled`/`unavailable` vẫn bị xét giao trễ | Có thể ra tín hiệu sai | Nếu `data["order_status"]` ∈ {`canceled`, `unavailable`} thì bỏ qua phần xét giao trễ |

**Gợi ý code** (thay bước 1 → 3 trong `investigate`):

```python
ship = await self.gateway.call("get_shipment_summary", case_id=case_id, order_id=order_id)
data = ship["data"]
opened = _parse_iso(case_opened_at)
purchase = None  # nếu có order_data thì truyền vào; nếu không, lấy min event_at trong khoảng hợp lý
limits = [l for l in data.get("shipping_limits", [])
          if (ts := _parse_iso(l.get("shipping_limit_at"))) and opened and ts <= opened
          and (purchase is None or ts >= purchase - timedelta(days=1))]
limit_date = min((_parse_iso(l["shipping_limit_at"]) for l in limits), default=None)
carrier = _parse_iso(data.get("delivered_carrier_at"))
estimated = _parse_iso(data.get("estimated_delivery_at"))
arrived = _parse_iso(data.get("delivered_customer_at"))
seller_ids = sorted({l["seller_id"] for l in limits if l.get("seller_id")})

if data.get("order_status") not in ("canceled", "unavailable") and estimated:
    late = (arrived and arrived > estimated) or (not arrived and opened and opened > estimated)
    if late:
        seller_late = bool(limit_date and carrier and carrier > limit_date)
        primary_issue = "late_delivery_seller" if seller_late else "late_delivery_logistics"

sellers_ev = await self.gateway.call("get_sellers", case_id=case_id, order_id=order_id)
```

Lưu ý:
- Hàm `_parse_iso` hiện tại đọc được định dạng `2018-03-05T09:00:00-03:00`.
- Muốn lọc chuẩn cần ngày mua. Có thể thêm tham số tùy chọn `order_purchase_at: str | None = None` vào `investigate` (thêm ở **cuối**, có giá trị mặc định, để không phá coordinator), rồi báo TV1 để truyền vào.

**DoD của TV3:**
- 10 case `late_delivery_seller` → `primary_issue_candidate == "late_delivery_seller"`.
- 10 case `late_delivery_logistics` → `"late_delivery_logistics"`.
- Case `unsupported_claim` (có `delivered_late` nhiễu sau `opened_at`) → `None`.

---

## 👤 TV4 — nan-bi — `src/student_agent/agents/payment_agent.py` (+ `tests/test_payment_agent.py`)

**Tình trạng:** cấu trúc tốt, dùng `Decimal`, có tách `_reconcile` để test. Nhưng **đọc sai key**, và **exception làm hỏng 60/100 case**.

| # | Vấn đề | Hậu quả | Cách sửa |
| --- | --- | --- | --- |
| 1 | `get_refund_timeline` không có `try/except` | Tool **báo lỗi khi đơn không có refund** (60/100 case), nên cả `investigate()` ném exception; trace ghi `payment-agent agent_error` 60 lần | Bọc `try/except`; lỗi → `refunds_raw = []`, ref rỗng |
| 2 | `_extract_list(ptl, "timeline")` | Key thật là `events`, nên nhận về `[data]` (1 dict chứa cả payments) | `_extract_list(ptl_ev["data"], "events")` |
| 3 | `_extract_list(rtl, "refunds")` | Key thật là `events` | `_extract_list(rtl_ev["data"], "events")` |
| 4 | Số tiền event đọc `amount` / `refund_amount` / `payment_value` | Field thật là `amount_brl` | Thêm `e.get("amount_brl")` vào đầu chuỗi `or` |
| 5 | Không lọc theo thời gian | Refund `failed` nhiễu làm case split payment bị hiểu nhầm; refund `pending` nhiễu làm case mismatch bị hiểu nhầm | Lọc `events` theo `event_at` trong khoảng thời gian case (cần `order_purchase_timestamp`: thêm tham số tùy chọn `order_purchase_at=None` + `opened_at=None` ở **cuối** chữ ký, rồi báo TV1) |
| 6 | Duplicate: dựa `external_transaction_id` (không tồn tại) hoặc `payment_type` + amount trên `get_order_payments` | Payment rows **không có ngày**, nên không lọc được nhiễu | Dùng **event `captured` trong khoảng thời gian**: ≥ 2 event cùng `amount_brl` và tổng > giá trị đơn → `duplicate_charge`; tổng = giá trị đơn → `valid_split_payment` |
| 7 | Mismatch: so `captured_total` với `expected_order_total` | Coordinator không truyền `expected_order_total` (dữ liệu nhiễu làm tổng sai) | Dựa vào event tường minh `event_type == "reconciliation_mismatch"` |
| 8 | Thứ tự ưu tiên | — | Giữ: refund failed/pending > duplicate > mismatch > split (khớp dữ liệu) |
| 9 | `assert _check == recommended_dec` trong code chạy thật | `assert` bị tắt khi chạy `python -O`; nếu sai thì crash | Đổi thành `if ...: raise ValueError(...)`, hoặc bỏ |
| 10 | `financial_resolution` tự tính | **Bị verifier (TV5) ghi đè theo `get_policy.rules[issue].refund_brl`** | Không cần tối ưu số tiền; tập trung **phát hiện đúng issue** |
| 11 | Test `test_floating_point_sum_is_exact` **đang fail** | `sum(0.1, 0.2)` bằng float luôn ra `0.30000000000000004` | Test nên so `round(sum(...), 2) == 0.30`, hoặc so bằng `Decimal(str(x))` |
| 12 | Lint: **27 lỗi ruff** (UP045 `Optional[X]` → `X \| None`, E501, E741 biến tên `l`, B007, F401, I001) | CI đỏ | `ruff check --fix` (sửa được 10 lỗi), phần còn lại sửa tay |
| 13 | Branch `nan-bi` có `workflow.py` 550 dòng riêng | Đã **không** được merge (dùng workflow của TV1). Workflow đó còn gọi `get_order_shipment`, một tool không tồn tại | Đừng thêm lại vào `workflow.py`; logic payment chỉ nằm trong `payment_agent.py` |

**Gợi ý code:**

```python
try:
    rtl_ev = await gateway.call("get_refund_timeline", case_id=case_id, order_id=order_id)
except Exception:
    rtl_ev = {"data": {"events": []}}
ptl_events = self._extract_list(ptl_ev.get("data"), "events")
refund_events = self._extract_list(rtl_ev.get("data"), "events")
if in_window:  # hàm lọc theo khoảng thời gian case, xem Phần chung mục 1
    ptl_events = [e for e in ptl_events if in_window(e.get("event_at"))]
    refund_events = [e for e in refund_events if in_window(e.get("event_at"))]

captured = [_dec(e["amount_brl"]) for e in ptl_events if e.get("event_type") == "captured"]
mismatch = any(e.get("event_type") == "reconciliation_mismatch" for e in ptl_events)
latest_refund = sorted(refund_events, key=lambda e: e.get("event_at", ""))[-1:] or [None]
```

**DoD của TV4:**
- Không còn `agent_error` do payment trong trace.
- Test `test_payment_agent.py` pass hết.
- `ruff check .` sạch.
- 5 nhóm case payment/refund (50 case) → `detected_issue` đúng topic.

---

## 👤 TV5 — dlvanh — `policy_agent.py`, `verifier_agent.py`, `state.py`, `tests/`

**Tình trạng: tốt nhất nhóm, đang được dùng nguyên vẹn.** Policy agent đọc đúng cấu trúc `rules`; verifier đảm bảo refund, action và status khớp policy, lọc evidence, validate schema.

| # | Vấn đề | Cách sửa |
| --- | --- | --- |
| 1 | **Policy là chung cho mọi case** (hash giống nhau ở 100 case). `rules.late_delivery_seller.responsible_parties[0].party_id = "seller-e58fb7bfd033"` chỉ là ví dụ, không phải seller của case | Hiện coordinator đã thay seller trước khi gọi `verify`. Nên sửa luôn ở gốc: trong `lookup()`, nếu `party_type == "seller"` thì thay `party_id` bằng seller thật của case (lấy từ evidence item/shipment trong khoảng thời gian), hoặc `None` nếu không biết |
| 2 | `_entities()` tự thêm seller của policy vào `seller_ids` (`responsible_seller_added`) | Nếu `verify()` tự gọi lại `decide()` (khi `state.policy_decision` lệch issue), seller ví dụ sẽ lọt vào output. Chỉ thêm seller khi seller đó có trong evidence của case |
| 3 | `REQUIRED_DOMAINS` / `RELEVANT_DOMAINS` là giả thuyết | Giữ nguyên, rồi tinh chỉnh theo điểm `evidence` trên leaderboard public (phối hợp TV1, vì `adjudicator.ISSUE_DOMAINS` cũng tham gia chọn ref) |
| 4 | Confidence bị kẹp tối đa 0.95 | Hợp lý. Hiện 79/100 case ở 0.95. Nếu điểm calibration public thấp, cân nhắc hạ các case có tín hiệu cạnh tranh |
| 5 | Test | Thêm test "policy chung + seller ví dụ" (mục 1) và test `verify()` với draft có ref ngoài `CaseState` |

Commit `81c1d64` (sửa lint cho file TV2/TV3, gộp export) rất tốt. `__init__.py` trên branch tích hợp đã export thêm `PaymentAgent` / `PaymentInvestigationResult`.

---

## 👤 TV1 — (lead) — việc còn lại của mình

| # | Việc | Ghi chú |
| --- | --- | --- |
| 1 | Merge `feat/tv1-integration` → `main` sau lần `day09 run` sạch | Đã merge `main` mới nhất (có commit của TV5) trong worktree; test pass |
| 2 | Đăng nhập workspace `/l3a` và nộp `dist/submission.zip` | Phải tự nhập Team API Key (Claude không được nhập key) |
| 3 | Tinh chỉnh bằng điểm public | `evidence` → `adjudicator.ISSUE_DOMAINS`; `calibration` → strength trong `evidence_rules.py`; `semantic` → đối chiếu `payment_references` (đang để rỗng vì dữ liệu không có id giao dịch) và `data_conflicts` (đang báo bản ghi nhiễu ngoài khoảng thời gian case) |
| 4 | Khi TV2 – TV4 sửa xong | Có thể nâng `SPECIALIST_STRENGTH` trong `coordinator.py` từ 0.5 lên 0.7 |
| 5 | `src/student_agent/llm.py` + `openai` trong `pyproject.toml` | Chưa commit; có ai đó đang thêm. **Pipeline hiện không cần LLM**; nếu dùng thì không đưa key vào repo |

---

## Checklist khi mở PR (mọi người)

- [ ] Chỉ sửa file mình sở hữu; giữ nguyên chữ ký hàm ở Phần chung mục 4 (thêm tham số mới thì đặt ở **cuối** và có giá trị mặc định)
- [ ] Gọi tool đúng tham số (bảng mục 2); không gọi `get_product_context` / `get_customer_history`
- [ ] Lọc bản ghi theo khoảng thời gian case trước khi kết luận
- [ ] Bọc `try/except` quanh mỗi lần gọi tool; không `print`, không `assert` trong code chạy thật
- [ ] Không dùng `confidence = 1.0`
- [ ] `pytest` và `ruff check .` đều pass
- [ ] Branch mới nhất từ `main`; báo TV1 trước khi merge để chạy lại 100 case
