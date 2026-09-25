# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

```text
inputs/<case_id>.json
      │
      ▼
Coordinator ── task_assigned ──► Order agent ── get_order, get_order_items
      │ ◄──────── handoff ──────────┘   (tạo case window)
      │
      ├── task_assigned ──► Payment agent  ── get_payment_timeline, get_refund_timeline ┐ chạy
      ├── task_assigned ──► Shipment agent ── get_shipment_summary                       ┘ song song
      │ ◄──────── handoff ──────────┘
      │
      │  rules.detect_issues + choose_primary (deterministic, không dùng LLM)
      │
      ├── task_assigned ──► Policy agent ── get_policy ──► policy_decided
      ├── task_assigned ──► Order agent  ── get_sellers   (chỉ khi bên chịu trách nhiệm là seller)
      │
      ├── handoff (DRAFT_READY) ──► Verifier ──► verification_completed (PASS/FAIL)
      ▼
outputs/<case_id>.json + traces/trace.jsonl
```

Code: `src/student_agent/workflow.py` (agents, coordinator, verifier, output) và `src/student_agent/rules.py` (luật thuần, không I/O).

**Case window.** Mọi order trên Gateway có các dòng "mồi" (item, capture, refund, shipment event) mang timestamp nằm ngoài khoảng `[order_purchase_timestamp, opened_at]`, hoặc là bản sao y hệt một dòng thật (trùng cả timestamp). Chỉ các bản ghi phân biệt nằm trong khoảng này được dùng làm sự thật. Mồi đôi khi nằm ngay trong khoảng, nên mỗi `order_item_id` chỉ giữ dòng có `shipping_limit_date` sớm nhất, và tiền hoàn cho đơn hủy hoặc hết hàng bị giới hạn bởi tổng giá sản phẩm. Dữ liệu bị loại được ghi vào `data_conflicts`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case input | Tạo message, giao việc, gộp facts, chọn `primary_issue`, dựng output | Draft output → Verifier |
| Order/item (`order-agent`) | `claimed_order_id`, `opened_at` | Lấy order, item; tạo case window; lọc item theo `shipping_limit_date`; xác minh seller | order, window, items trong window |
| Payment (`payment-agent`) | window | Lấy capture, `reconciliation_mismatch`, refund event trong window | captures, mismatches, refunds |
| Shipment (`shipment-agent`) | window | Lấy mốc giao hàng, shipment event trong window | carrier/customer/estimated timestamps |
| Policy (`policy-agent`) | `policy_version`, issue | Tra `rules[issue]` → status, action, loại bên chịu trách nhiệm | `policy_decided` |
| Verifier | Draft output, evidence ledger | Kiểm tra invariant (mục 6) | `verification_completed` PASS/FAIL |

Quyền gọi tool (`TOOL_GRANTS`, kiểm tra khi chạy, sai quyền → `PermissionError`):

| Actor | Tool |
| --- | --- |
| order-agent | `get_order`, `get_order_items`, `get_sellers` |
| payment-agent | `get_payment_timeline`, `get_refund_timeline` |
| shipment-agent | `get_shipment_summary` |
| policy-agent | `get_policy` |

Không gọi `get_order_payments` (dòng payment không có timestamp nên không lọc được mồi; `get_payment_timeline` đã chứa cùng dữ liệu kèm event), `get_product_context` (không ảnh hưởng quyết định) và `get_customer_history` (order không trả `customer_unique_id`).

## 3. A2A protocol

- Envelope: `Message(case_id, sender, recipient, kind, payload)`. `kind` ∈ `ORDER_CONTEXT`, `PAYMENT_RECONCILIATION`, `SHIPMENT_TIMELINE`, `POLICY_LOOKUP`, `SELLER_CHECK`.
- Correlation: mọi message và trace event mang `case_id`; mỗi case có `CaseContext` riêng, không chia sẻ state giữa các case.
- Handoff: coordinator emit `task_assigned` (target = agent, `decision_code` = kind); agent trả report và emit `handoff` (target = coordinator, `decision_code` = `<kind>_DONE`). Draft gửi verifier bằng `handoff` `DRAFT_READY`.
- Không vòng lặp: luồng là DAG cố định (order → payment ∥ shipment → policy → seller check tùy chọn → verifier), mỗi agent được gọi tối đa một lần cho mỗi kind.
- Timeout: HTTP timeout 300 s đọc / 30 s kết nối (`mcp_gateway.py`).
- Trace chỉ chứa sự kiện, tool, evidence ref, decision code và số liệu đã quan sát; không có nội dung suy luận.

## 4. Evidence lifecycle

1. `EvidenceGateway.call` validate response theo `mcp-evidence-response-v1`.
2. `Agent.fetch` lưu `evidence_ref` vào `CaseContext.refs[group]` và ledger `ref → case_id`, rồi emit `tool_result_consumed` (actor, tool, ref, domain).
3. Output chỉ trích dẫn ref trong ledger, theo nhóm evidence gắn với `primary_issue` (`CITATIONS`). Ví dụ: `late_delivery_seller` → order, items, sellers, shipment, policy. Claim `requested_full_refund` → payment, refund, policy.
4. Ref không bao giờ được tạo, sửa hoặc dùng lại giữa các case; mỗi lần `day09 run` sinh ref mới, nên bài nộp phải lấy từ một lần chạy đầy đủ.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| Lỗi kết nối (connect timeout/error) | Có, tối đa 5 lần ở tầng HTTP transport; request chưa được gửi nên không trùng, và vẫn giữ một MCP session | Hết lượt → dừng run, không nộp output thiếu | — |
| Lỗi tool khác không phải "not found" | Có, tối đa 3 lần, backoff 1 s, 2 s (call chỉ đọc nên idempotent) | Hết lượt → dừng run | — |
| Not found (`get_order`, window không hợp lệ) | Không | `primary_issue = insufficient_evidence`, `needs_investigation`, refund 0 | `handoff` `EVIDENCE_UNAVAILABLE` |
| Not found (`get_refund_timeline`) | Không | Coi như đơn không có refund event, không trích dẫn | — |
| Policy không tải được | Không | Status `needs_investigation`, action `escalate_manual_review`, party `unknown` | `handoff` `POLICY_UNAVAILABLE` |
| Source conflict (dòng ngoài case window hoặc bản sao y hệt) | Không | Loại khỏi facts, ghi `data_conflicts` với `EXCLUDED_OUT_OF_CASE_WINDOW` | `tool_result_consumed` của tool tương ứng |
| Seller trong item không có trong `get_sellers` | Không | Bỏ seller đó khỏi `responsible_parties`; còn rỗng → `unknown` | `handoff` `SELLER_CHECK_DONE` |
| Invalid specialist result / verifier FAIL | Không | Hạ về `needs_investigation`, confidence ≤ 0.4 | `verification_completed` `FAIL` + `problems` |

Evidence thiếu không bao giờ được thay bằng dữ liệu phỏng đoán.

## 6. Verification invariants

Trước khi finalize, verifier kiểm tra:

- Mọi ref trong `evidence_refs` và `claim_assessments` có trong ledger của đúng case (`EVIDENCE_OUT_OF_SCOPE`), và có ít nhất một ref (`NO_EVIDENCE`).
- `affected_entities.order_ids` đúng bằng `claimed_order_id` (`ENTITY_SCOPE`).
- Tổng `refund_lines` bằng `recommended_refund_brl` (`REFUND_LINES_MISMATCH`).
- Issue không cần hành động (`valid_split_payment`, `unsupported_claim`) hoặc status `no_action` thì refund = 0 (`NO_ACTION_WITH_REFUND`, `STATUS_REFUND_INCONSISTENT`).
- Confidence nằm trong [0, 1] (`CONFIDENCE_BOUNDS`).

CLI validate output theo `l3a-output-v2` và kiểm tra `case_id` trước khi ghi file.

## Luật quyết định (`rules.py`)

| Issue | Điều kiện trên dữ liệu trong window | Refund | Bên chịu trách nhiệm |
| --- | --- | --- | --- |
| `canceled_order_paid` | status `canceled`, có capture, chưa refund `completed` | min(tổng giá sản phẩm, tổng capture) | theo policy (platform) |
| `unavailable_order_paid` | status `unavailable`, có capture, chưa refund `completed` | min(tổng giá sản phẩm, tổng capture) | seller của order |
| `refund_failed` | refund event `failed` | tổng refund failed | theo policy |
| `refund_pending` | refund event `pending` | 0 | theo policy |
| `payment_mismatch` | `reconciliation_mismatch` đang `open` | tổng mismatch open | theo policy |
| `duplicate_charge` | có capture trùng số tiền và tổng capture > tổng order | số tiền bị trùng | theo policy |
| `valid_split_payment` | ≥ 2 capture, tổng = giá + phí ship | 0 | theo policy |
| `late_delivery_seller` | giao khách sau ngày dự kiến và giao hãng sau `shipping_limit_date` | min(phí ship, tổng capture) | seller của order |
| `late_delivery_logistics` | giao khách sau ngày dự kiến, giao hãng đúng hạn | min(phí ship, tổng capture) | theo policy |
| `unsupported_claim` | không điều kiện nào ở trên đúng | 0 | theo policy |
| `insufficient_evidence` | không lấy được order hoặc window | 0 | unknown |

Chọn `primary_issue`: nếu issue trong claim đầu tiên được dữ liệu xác nhận thì chọn nó (confidence 0.9, hoặc 0.8 nếu dữ liệu còn khớp issue khác). Nếu không, lấy issue đầu tiên theo thứ tự ưu tiên ở bảng trên (confidence 0.6). Không có issue nào → `unsupported_claim` (0.8 nếu khách cũng khai như vậy, ngược lại 0.7).

`case_status` và `resolution_actions` lấy từ `get_policy`. Số tiền và `party_id` luôn tính từ dữ liệu của order, vì giá trị trong policy chỉ là ví dụ.

## 7. Reproducibility

- Không dùng LLM; quyết định deterministic, không có random seed (chỉ `event_id` của trace là ngẫu nhiên).
- Python ≥ 3.11 (đã chạy với 3.12); dependency theo khoảng version trong `pyproject.toml`.
- Các case chạy tuần tự; trong một case, payment và shipment chạy song song (tối đa 2 call đồng thời).
- Mỗi case gọi 6 tool, thêm `get_sellers` khi bên chịu trách nhiệm là seller.
- Lệnh: `day09 run && day09 validate && day09 package --output dist/submission.zip`.
- Test offline: `pytest -q tests/test_workflow.py` (dùng gateway giả, không gọi mạng).
