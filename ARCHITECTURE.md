# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System Overview

```text
inputs/<case_id>.json
        │
        ▼
┌──────────────────────────┐
│   Coordinator / Router   │  ← solve_case() in workflow.py
└─────────────┬────────────┘
              │ (sequential handoffs)
   ┌──────────┼──────────┬──────────┐
   ▼          ▼          ▼          │
Order/    Payment    Shipment       │
Item       Agent      Agent         │
Agent       │          │            │
   │        │          │            │
   └────────┴──────────┘            │
              │ (MCP Evidence)       │
              ▼                     │
     ┌──────────────────┐           │
     │   Policy Agent   │◄──────────┘
     └────────┬─────────┘
              │
              ▼
     ┌──────────────────┐
     │  Verifier Agent  │  ← pure Python validation
     └────────┬─────────┘
              │ (Validated Output)
              ▼
         [END OUTPUT]   +   traces/trace.jsonl
```

**LLM Backend:** NVIDIA NIM `nvidia/nemotron-3-ultra-550b-a55b` qua OpenAI-compatible API
**Coordination pattern:** Pure Python async state-machine (no external framework)

---

## 2. Agent Ownership

| Actor | Input | Trách nhiệm | Output / Handoff |
|---|---|---|---|
| Coordinator | `case` dict từ input file | Điều phối thứ tự, phát trace `task_assigned`, `handoff` | Truyền `case`, `gateway`, `trace` cho từng specialist |
| Order/Item Agent | `case`, `gateway` | Gọi `get_order`, `get_order_items`; LLM phân tích status và anomalies | `{evidence_refs, analysis}` → Payment Agent & Policy Agent |
| Payment Agent | `case`, `gateway`, `order_result` | Gọi `get_order_payments`, `get_payment_timeline`, `get_refund_timeline`; LLM tính toán refund | `{evidence_refs, analysis}` → Policy Agent |
| Shipment Agent | `case`, `gateway` | Gọi `get_shipment_summary`; LLM xác định delay & responsibility | `{evidence_refs, analysis}` → Policy Agent |
| Policy Agent | `case`, `gateway`, kết quả từ 3 agents trên | Gọi `get_policy`; LLM áp dụng policy → kết luận `primary_issue`, `claim_assessments`, `resolution_actions` | `{evidence_refs, analysis}` → Verifier |
| Verifier Agent | Kết quả từ tất cả agents | Merge evidence refs, sanitize, đảm bảo khớp schema | Output dict cuối cùng (`day09-l3a-output-v2`) |

**Tool permissions theo agent:**

| Agent | MCP Tools được phép |
|---|---|
| Order/Item | `get_order`, `get_order_items` |
| Payment | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` |
| Shipment | `get_shipment_summary` |
| Policy | `get_policy` |
| Verifier | Không gọi MCP |

---

## 3. A2A Protocol

- **Correlation:** Mọi MCP call và trace event đều truyền `case_id` làm khóa định danh.
- **Message envelope:** Kết quả mỗi agent là dict `{evidence_refs: [...], analysis: {...}}` được truyền trực tiếp qua tham số hàm Python.
- **Handoff condition:** Mỗi agent phải hoàn thành thành công (không raise exception) trước khi coordinator handoff sang agent kế tiếp.
- **Loop prevention:** Luồng là DAG (directed acyclic graph) — không có vòng lặp.
- **Timeout:** httpx2 timeout 300s per MCP call (cấu hình trong `mcp_gateway.py`).
- **Session scope:** Mỗi case mở một MCP session riêng để lỗi transport không làm hỏng toàn bộ batch.
- **Resume:** Output hợp lệ kèm `case_finalized` là checkpoint; lần chạy sau bỏ qua case đó và loại trace dở của case chưa hoàn thành.
- **Trace rule:** Chỉ trace sự kiện nằm trong public contract (`task_assigned`, `handoff`, `tool_result_consumed`, `policy_decided`, `verification_completed`). Không trace nội dung LLM prompt/response.

---

## 4. Evidence Lifecycle

1. Agent gọi `gateway.call(tool_name, case_id=..., ...)`.
2. `EvidenceGateway` validate response với `mcp-evidence-response-v1.schema.json`.
3. Agent lấy `evidence["evidence_ref"]` (dạng `ev_...`) và `evidence["data"]`.
4. Agent emit trace `tool_result_consumed` với `evidence_refs=[ref]`.
5. `evidence_ref` được đưa vào danh sách kết quả của agent.
6. Verifier Agent merge tất cả refs, dedup, giới hạn 30 refs tối đa.
7. **Evidence không được tái sử dụng giữa các case** — mỗi `gateway.call` đều bao gồm `case_id` để server audit.

---

## 5. Failure Policy

| Failure | Retry? | Fallback | Trace event / code |
|---|---|---|---|
| MCP connect/initialize | Tối đa 3 session mới, backoff 0s/1s/3s | Hết retry thì dừng tại case hiện tại; lần chạy sau resume | Không làm mất checkpoint case trước |
| MCP tool transport/timeout | Tối đa 3 lần, backoff 0s/0.5s/1.5s | Hết retry thì fail case, không tạo evidence giả | Không emit `tool_result_consumed` khi chưa có response hợp lệ |
| Case bị gián đoạn | Tối đa 5 lần với MCP session mới, backoff 0s/2s/5s/10s/20s | Xóa trace dở của lần thử trước; hết retry thì dừng để lần chạy sau resume | Chỉ giữ trace của lần hoàn thành |
| MCP response báo `is_error` | Không | Raise `RuntimeError`; không retry lỗi nghiệp vụ | CLI log exception |
| LLM 429/503 | Tối đa 6 lần, exponential backoff 10/20/40/80s rồi cap 120s | Hết retry thì fail case | CLI log exception |
| LLM JSON/validation error khác | Không | Exception propagate | CLI log |
| MCP tool `not_found` | Không | RuntimeError: "MCP tool X failed" | Propagate |
| Source conflict (data_conflicts) | N/A | Policy Agent ghi vào `data_conflicts` với `resolution_code` | `tool_result_consumed` + policy output |
| Invalid specialist result | Không | Verifier sanitize, sau đó JSON Schema quyết định pass/fail | `verification_completed` chỉ emit sau khi validate thành công |

MCP chỉ retry lỗi transport trước khi nhận được response. Response lỗi nghiệp vụ không retry để tránh gọi lặp không cần thiết; toàn bộ tools hiện dùng là truy vấn chỉ đọc. Batch ghi output atomically rồi phát `case_finalized`; chỉ cặp này mới được xem là checkpoint hoàn chỉnh.

---

## 6. Verification Invariants

Verifier Agent sanitize output rồi gọi `gateway.validate_output()`. JSON Schema là chốt cuối trước khi phát `verification_completed` và trả output:

1. **Schema compliance:** `schema_version = "day09-l3a-output-v2"`, `case_id` khớp pattern.
2. **Entity scope:** `order_ids`, `item_ids`, v.v. đều là list[str], max 20 items.
3. **Evidence ownership:** Chỉ dùng `evidence_ref` thực sự nhận được từ MCP trong run này.
4. **Claim linkage:** Mỗi `claim_assessment` phải có `evidence_refs` là subset của refs đã thu thập.
5. **Financial consistency:** `recommended_refund_brl >= 0`, currency = "BRL".
6. **Confidence bounds:** Tất cả confidence trong `[0.0, 1.0]`.
7. **Resolution actions:** Max 8 actions, mỗi action ≤ 80 ký tự.
8. **additionalProperties:** Không thêm field ngoài schema.

---

## 7. Reproducibility

| Item | Value |
|---|---|
| LLM Model | `nvidia/nemotron-3-ultra-550b-a55b` |
| LLM Endpoint | `https://integrate.api.nvidia.com/v1` |
| LLM SDK | `openai>=1,<3` |
| Python | 3.13 (py launcher) |
| MCP SDK | `mcp>=2,<3` |
| Concurrency | Sequential (1 case tại một thời điểm) |
| Random seed | Không dùng random |
| Run command | `.\.venv\Scripts\Activate.ps1 && day09 run` |
| Validate | `day09 validate` |
| Package | `day09 package --output dist/submission.zip` |
