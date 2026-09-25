# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Hệ thống Multi-Agent xử lý từng case khiếu nại thương mại điện tử theo luồng tuần tự: tiếp nhận → thu thập bằng chứng song song → tổng hợp chính sách → thẩm định → xuất kết quả.

```text
inputs/<case_id>.json
        │
        ▼
┌──────────────────────────┐
│   Coordinator / Router   │  (TV1) — phát task_assigned, điều phối luồng
└─────────────┬────────────┘
              │ (Handoff — song song)
 ┌────────────┼────────────────────────┐
 ▼            ▼                        ▼
┌─────────────────┐  ┌──────────────┐  ┌─────────────────┐
│ Order/Item      │  │ Payment      │  │ Shipment        │
│ Agent (TV2)     │  │ Agent (TV3)  │  │ Agent (TV2)     │
│ get_order       │  │ get_order_   │  │ get_shipment_   │
│ get_order_items │  │ payments     │  │ summary         │
└────────┬────────┘  └──────┬───────┘  └────────┬────────┘
         │                  │                   │
         └──────────────────┼───────────────────┘
                            │ (MCP Evidence Collector)
                            ▼
                 ┌──────────────────────┐
                 │    Policy Agent      │  (TV3) — tổng hợp bằng chứng,
                 │                      │  phát policy_decided
                 └──────────┬───────────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │   Verifier Agent     │  (TV1) — thẩm định, phát
                 │                      │  verification_completed + case_finalized
                 └──────────┬───────────┘
                            │ (Validated Output)
                            ▼
              outputs/<case_id>.json  +  traces/trace.jsonl
```

**Sơ đồ lifecycle event bắt buộc theo thứ tự:**
```
case_received → task_assigned → tool_result_consumed(×N) → handoff
             → policy_decided → verification_completed → case_finalized
```

## 2. Agent ownership

| Actor | Owner | Input | Trách nhiệm | MCP Tools được phép gọi | Output/handoff |
| --- | --- | --- | --- | --- | --- |
| **Coordinator** | TV1 | `inputs/<case_id>.json` | Tiếp nhận case, phân loại, điều phối agent. Phát `case_received` và `task_assigned`. | Không gọi MCP trực tiếp | Giao task cho 3 Specialist Agents |
| **Order/Item Agent** | TV2 | `case_id`, `order_id` từ Coordinator | Lấy trạng thái đơn hàng (`canceled`, `unavailable`, ...) và danh sách item. Phát `tool_result_consumed` sau mỗi lần gọi tool. | `get_order`, `get_order_items`, `get_product_context`, `get_sellers` | Dict kết quả order gửi về Policy Agent |
| **Shipment Agent** | TV2 | `case_id`, `order_id` | Lấy thông tin vận chuyển, kiểm tra ngày giao hàng, phát hiện giao trễ. Phát `tool_result_consumed`. | `get_shipment_summary` | Dict kết quả shipment gửi về Policy Agent |
| **Payment Agent** | TV3 | `case_id`, `order_id` | Đối soát giao dịch thanh toán, phát hiện `duplicate_charge`. Phát `tool_result_consumed` và `policy_decided`. | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | Dict kết quả payment + `is_duplicate` gửi về Policy Agent |
| **Policy Agent** | TV3 | Kết quả từ 3 Specialist Agents | Tổng hợp bằng chứng, áp dụng chính sách nghiệp vụ, xác định `primary_issue`, `responsible_party`, tính `recommended_refund_brl`. Phát `policy_decided`. | Không gọi MCP | Dict quyết định chính sách gửi Verifier |
| **Verifier** | TV1 | Quyết định từ Policy Agent + tất cả evidence_refs | Kiểm tra cross-field consistency, calibrate confidence, đảm bảo schema compliance. Phát `verification_completed` và `case_finalized`. Xuất output JSON cuối. | Không gọi MCP | `outputs/<case_id>.json` (validated) |

**Quy tắc phân quyền MCP:**
- Mỗi agent chỉ được gọi các tool trong phạm vi trách nhiệm của mình.
- Tránh cho mọi agent quyền truy vấn tất cả tool — nguyên tắc least privilege.
- Tool phụ trợ `get_customer_history`, `get_policy` có thể được gọi bởi Policy Agent khi cần thêm context.

## 3. A2A protocol

### Message envelope
Mỗi message truyền giữa các agent được gắn `case_id` để correlation. Không truyền dữ liệu thô — chỉ truyền `case_id` + `order_id` + evidence refs đã thu thập.

```python
# Ví dụ message handoff từ Order Agent sang Policy Agent:
{
    "case_id": "L3A_CASE_001",
    "order_id": "abc123",
    "issue": "canceled_order_paid",          # hoặc "no_issue"
    "responsible": "platform",
    "ev": ["ev_AbCdEf..."],                  # evidence refs thực từ MCP
    "order_status": "canceled"
}
```

### Điều kiện handoff
- Coordinator phát `task_assigned` → giao cho 3 Specialist Agents chạy (có thể tuần tự hoặc song song).
- Mỗi Specialist Agent hoàn thành → kết quả được tập hợp tại Policy Agent.
- Policy Agent chỉ khởi động khi **tất cả** Specialist Agents đã trả kết quả.
- Verifier chỉ khởi động sau khi Policy Agent phát `policy_decided`.

### Chống vòng lặp
- Mỗi `case_id` chỉ được xử lý **một lần duy nhất** trong một run.
- Không có handoff ngược chiều (Verifier → Policy Agent → Specialist Agent).
- Nếu một agent fail, workflow kết thúc tại đó với `insufficient_evidence` hoặc `unsupported_claim`.

### Timeout và retry
- Xem **Mục 5 — Failure policy** bên dưới.

## 4. Evidence lifecycle

### Quy tắc bắt buộc (vi phạm = Hard Gate = 0 điểm)

1. **Chỉ dùng evidence_ref thật từ MCP.** Format: `ev_[A-Za-z0-9_-]{20,96}`. Không tự tạo hay sửa ref.
2. **Không dùng evidence chéo case.** Evidence của `L3A_CASE_001` không được xuất hiện trong output của `L3A_CASE_002`.
3. **Mỗi lần gọi tool và dùng kết quả để suy luận → bắt buộc emit `tool_result_consumed`.**
4. **Chỉ trích dẫn evidence thật sự hỗ trợ kết luận.** Không liệt kê evidence thừa/không liên quan.

### Flow xử lý evidence

```
1. Agent gọi tool MCP:
   response = await gateway.call("get_order", case_id=case_id, order_id=order_id)

2. Lấy evidence_ref từ response:
   ev_ref = response["evidence_ref"]   # dạng "ev_AbCdEfGhIj..."
   data   = response["data"]           # dữ liệu thực tế để xử lý nghiệp vụ

3. Emit trace event ngay sau khi dùng evidence:
   trace.emit(
       case_id=case_id,
       event_type="tool_result_consumed",
       actor="order-agent",
       tool_name="get_order",
       evidence_refs=[ev_ref]
   )

4. Đưa ev_ref vào output cuối:
   "evidence_refs": ["ev_AbCdEf...", "ev_XyZwVu..."]  # tập hợp unique
```

### MCP Response envelope (theo `mcp-evidence-response-v1.schema.json`)
```json
{
  "schema_version": "day09-mcp-evidence-v1",
  "evidence_ref": "ev_AbCdEfGhIjKlMnOpQrStUv",
  "result_hash": "sha256:abcdef...64chars...",
  "domain": "order",
  "data": { ... },
  "warnings": []
}
```

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event / decision code |
| --- | --- | --- | --- |
| **MCP timeout** | Có, tối đa 2 lần với backoff | Sau 2 lần fail: `primary_issue = "insufficient_evidence"`, `case_status = "needs_investigation"`, `confidence = 0.3` | `tool_result_consumed` với `evidence_refs = []` |
| **Tool not found / 404** | Không | `primary_issue = "insufficient_evidence"`, ghi vào `data_conflicts[]` nếu có mâu thuẫn | `tool_result_consumed` với `evidence_refs = []` |
| **Source conflict** (dữ liệu mâu thuẫn giữa các tool) | Không | Ghi vào `data_conflicts[]` với `resolution_code`, chọn nguồn đáng tin hơn | `policy_decided` với `decision_code = "DATA_CONFLICT_RESOLVED"` |
| **Invalid specialist result** (thiếu key, schema sai) | Không | Skip specialist đó, Policy Agent dùng kết quả từ các agent còn lại | `verification_completed` với `confidence` thấp hơn |
| **Schema validation fail (output)** | Không | Verifier phải sửa lỗi trước khi emit `case_finalized`. Không xuất output không hợp lệ. | `verification_completed` với attributes ghi lý do fail |

**Nguyên tắc retry:**
- Retry phải idempotent: cùng `case_id` + `tool_name` + `order_id` luôn cho kết quả nhất quán.
- Không retry quá 2 lần để tránh vượt call budget (ảnh hưởng điểm Efficiency).
- Missing evidence **không được** biến thành dữ liệu phỏng đoán.

## 6. Verification invariants

Verifier Agent phải kiểm tra **tất cả** các điều kiện sau trước khi emit `case_finalized`:

### Schema compliance
- [ ] Output khớp 100% với `contracts/schemas/l3a-output-v2.schema.json`.
- [ ] `schema_version == "day09-l3a-output-v2"`.
- [ ] `case_id` đúng định dạng `^[A-Z0-9][A-Z0-9_-]{2,63}$`.
- [ ] Mọi `evidence_ref` đúng định dạng `^ev_[A-Za-z0-9_-]{20,96}$`.

### Status / Refund consistency
- [ ] `case_status == "no_action"` → `recommended_refund_brl == 0` và `refund_lines == []`.
- [ ] `case_status == "action_required"` → `recommended_refund_brl > 0`.
- [ ] `resolution_actions` là mảng `uniqueItems` (không trùng lặp).
- [ ] `case_status == "no_action"` → `resolution_actions` chỉ chứa `"close_case"`.

### Responsibility consistency
- [ ] Nếu `primary_issue == "late_delivery_seller"` → `responsible_parties[].party_type == "seller"`.
- [ ] Nếu `primary_issue == "late_delivery_logistics"` → `responsible_parties[].party_type == "logistics_provider"`.
- [ ] Nếu `primary_issue` là `canceled_order_paid` hoặc `unavailable_order_paid` → `party_type == "platform"` hoặc `"seller"`.
- [ ] Đơn vị vận chuyển không thể là bên chịu trách nhiệm hoàn tiền cho lỗi của seller.

### Evidence ownership
- [ ] Tất cả `evidence_refs` trong output phải xuất phát từ MCP call của **cùng case_id** trong run hiện tại.
- [ ] Không có `evidence_ref` nào rỗng, null hoặc giả mạo.
- [ ] Mọi evidence được trích dẫn phải có `tool_result_consumed` tương ứng trong trace.

### Confidence calibration
- [ ] `confidence ∈ [0.0, 1.0]`.
- [ ] Không đặt `confidence == 1.0` khi có `data_conflicts` hoặc missing evidence.
- [ ] Nếu chỉ có 1 evidence → `confidence ≤ 0.85`.
- [ ] Nếu `primary_issue == "insufficient_evidence"` → `confidence ≤ 0.5`.

### Lifecycle event coverage
- [ ] Trace chứa đủ 7 event bắt buộc theo đúng thứ tự:
  `case_received` → `task_assigned` → `tool_result_consumed` → `handoff` → `policy_decided` → `verification_completed` → `case_finalized`
- [ ] `case_received` phải là event đầu tiên, `case_finalized` phải là event cuối cùng.
- [ ] Ít nhất 1 `tool_result_consumed` event tồn tại trong trace.
- [ ] Ít nhất 1 `handoff` event tồn tại trong trace (A2A collaboration).

## 7. Reproducibility

### Môi trường
```
Python      : 3.13.x (yêu cầu >= 3.11)
OS          : Windows 11 / Ubuntu 22.04+
Virtual env : .venv (created with python -m venv .venv)
```

### Dependencies (pinned theo `pyproject.toml`)
```
httpx2       >= 2, < 3
jsonschema   >= 4.25, < 5  (với format extras)
mcp          >= 2, < 3
python-dotenv>= 1.1, < 2
pytest       >= 8.4, < 9   (dev)
ruff         >= 0.12, < 1  (dev)
```

### Cấu hình môi trường (file `.env` — KHÔNG commit vào git)
```dotenv
COMPETITION_API_URL=https://n7-competition.pages.dev
COMPETITION_TEAM_API_KEY=sk-team-...
MCP_ENDPOINT=https://day09-competition.34-142-201-239.sslip.io/mcp
```

### Lệnh cài đặt và chạy
```bash
# Cài đặt
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux/Mac
python -m pip install -e ".[dev]"

# Kiểm tra tools MCP
day09 mcp-tools

# Validate input
day09 validate-inputs

# Chạy batch
day09 run

# Validate output + trace
day09 validate

# Đóng gói nộp bài (tự động loại .env)
day09 package --output dist/submission.zip
```

### Giới hạn tài nguyên
- **Concurrency:** Xử lý tuần tự từng case trong `case-set.json` (không parallel giữa các case).
- **MCP Call budget:** Tối thiểu hóa số lần gọi tool; retry tối đa 2 lần mỗi tool call.
- **Random seed:** Không dùng random — pipeline hoàn toàn deterministic.
- **Timeout MCP:** Retry tối đa 2 lần, sau đó fallback sang `insufficient_evidence`.

### Ghi chú quan trọng
- API Key **không** được ghi vào bất kỳ file source code nào.
- File `dist/submission.zip` được tạo bởi `day09 package` — lệnh này tự động lọc `.env` ra khỏi zip.
- Score = trung bình arithmetic trên 100 cases (50 public + 50 private).
