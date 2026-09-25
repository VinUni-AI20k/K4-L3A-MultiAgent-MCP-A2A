# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Mô hình Multi-Agent được thiết kế theo dạng Orchestrator-Workers. Coordinator nhận input, quyết định thứ tự gọi các Specialist Agents. Các Specialist lấy dữ liệu từ MCP Gateway. Sau khi thu thập đủ, Verifier đối chiếu và xuất kết quả cuối cùng.

```text
Input → Coordinator → Specialists → Verifier → Output
                         │              │
                         └── MCP ───────┴── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff | Quyền gọi Tool (MCP) |
| --- | --- | --- | --- | --- |
| **Coordinator** | `inputs/<case_id>.json` | Phân tích ngữ cảnh ban đầu, định tuyến tới các specialist phù hợp, quản lý workflow state. | Handoff payload cho Specialist tương ứng, chuyển cho Verifier khi xong. | *Không có quyền gọi tool* |
| **Order/item** | Thông tin khách khiếu nại (từ Coordinator) | Xác minh sự tồn tại của đơn hàng, thông tin sản phẩm và lịch sử mua hàng. | Báo cáo trạng thái đơn, giá trị đơn, tính hợp lệ của item. | `get_order`, `get_order_items`, `get_product_context` |
| **Payment** | Thông tin đơn hàng | Xác minh dòng tiền, trạng thái thanh toán, tiền hoàn trả, đối soát số dư. | Trạng thái thanh toán (đã thanh toán, nợ, hoàn trả). | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline`, `get_customer_history` |
| **Shipment** | Tracking ID / Mã đơn | Kiểm tra tiến trình vận chuyển, tình trạng giao hàng, thời gian chốt. | Báo cáo tình trạng giao hàng (trễ, thất lạc, thành công). | `get_shipment_summary` |
| **Policy** | Tình huống vi phạm (từ các agent khác) | Đối chiếu điều khoản bảo hành, quy định của platform và lịch sử người bán. | Đánh giá trách nhiệm (Claim Assessment) dựa trên luật. | `get_policy`, `get_sellers` |
| **Verifier** | Báo cáo tổng hợp từ tất cả Specialist | Kiểm tra logic chéo (ví dụ: tiền trả có khớp tiền đơn), map evidence, chốt schema chuẩn. | JSON Output cuối cùng đúng format `l3a-output-v2.schema.json`. | *Không có quyền gọi tool* |

## 3. A2A protocol

*   **Message Envelope**: Mọi giao tiếp giữa các Agent phải bọc trong chuẩn envelope:
    ```json
    {
      "case_id": "L3A_CASE_001",
      "sender": "coordinator",
      "receiver": "payment_agent",
      "payload": { "order_id": "12345" },
      "evidence_refs_collected": ["evidence_8f2a"]
    }
    ```
*   **Correlation**: Mọi thông điệp và trace log phải đính kèm `case_id`.
*   **Điều kiện handoff**: Một Specialist chỉ trả kết quả (handoff) về cho Coordinator khi đã truy vấn đủ dữ liệu MCP hoặc gặp lỗi không thể phục hồi.
*   **Tránh vòng lặp**: Đặt `max_turns = 3` cho mỗi Specialist. Vượt quá 3 lần gọi tool mà chưa xong sẽ ép ngắt (timeout) và báo lỗi về Coordinator.
*   **Trace**: Hệ thống chỉ emit các Trace Observable như `agent_started`, `tool_called`, `handoff_completed`.

## 4. Evidence lifecycle

1. Khi Specialist gọi MCP tool, tool trả về Data + `evidence_ref` (ID bằng chứng).
2. Hệ thống tự động đẩy `evidence_ref` này vào danh sách `evidence_refs_collected` của Envelope.
3. Đồng thời emit log `tool_result_consumed` với `case_id` và `evidence_ref`.
4. Bằng chứng được scope 100% theo `case_id`. Không được phép sử dụng `evidence_ref` của `case_A` để làm căn cứ phán quyết cho `case_B`.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| **MCP timeout / 5xx** | Có (Max 3 lần, Exponential backoff) | Bỏ qua tool, báo cáo `data_unavailable` cho Coordinator | `mcp_retry_limit_exceeded` |
| **Not found (404)** | Không | Specialist kết luận thực thể không tồn tại | `resource_not_found` |
| **Source conflict** | Không | Ghi nhận Data Conflict, đẩy cho Verifier xử lý (ghi vào mảng `data_conflicts`) | `data_conflict_detected` |
| **Invalid specialist result** | Có (Max 1 lần) | Đóng case với status `needs_investigation` | `specialist_output_invalid` |

*Lưu ý: Tuyệt đối không phỏng đoán dữ liệu (hallucinate) nếu MCP trả về Not Found hoặc Timeout.*

## 6. Verification invariants

Trước khi Verifier xuất JSON cuối cùng, các biến bất biến (invariants) sau phải được check code:
*   **Schema validation:** Output phải parse thành công Pydantic model của `l3a-output-v2.schema.json`.
*   **Evidence linkage:** Bất kỳ ID bằng chứng nào nằm trong mảng `evidence_refs` của output đều phải tồn tại trong mảng `evidence_refs_collected` ở Envelope.
*   **Financial consistency:** Tiền khách trả = Tiền hàng + Tiền ship - Khuyến mãi.
*   **Confidence bounds:** `confidence` score luôn phải nằm trong khoảng [0.0, 1.0].

## 7. Reproducibility

*   **Model**: Payment agent gọi Qwen `qwen/qwen3-8b` qua OpenRouter (`llm.py`, cấu hình `OPENROUTER_*` hoặc `API_KEY`/`BASE_URL`/`MODEL_NAME`) chỉ khi luật tất định không kết luận được. Order, Shipment/Policy và Verifier hoàn toàn tất định, không dùng LLM; kết luận cuối do Verifier chốt từ evidence.
*   **Config**: `temperature = 0.0` cho toàn bộ Agents để đảm bảo tính tất định (deterministic).
*   **Concurrency**: `day09 run` chạy song song tối đa 10 case (`MAX_CONCURRENT_CASES` trong `cli.py`).
*   **Một lượt chạy = một phiên MCP**: evidence được audit theo team/run/case, nên không trộn evidence của hai phiên. Nếu phiên MCP hỏng giữa chừng, gateway đánh dấu `session_lost`, CLI bỏ toàn bộ kết quả và chạy lại từ đầu trong phiên mới (tối đa 3 lần).
*   **Lọc bản ghi dùng chung**: `scope_facts`, `detect_payment_issue`, `detect_late_delivery` trong `verifier.py` được Payment, Shipment/Policy và Verifier dùng chung để mọi agent suy luận trên cùng tập bản ghi hợp lệ.
*   **Dependencies**: Pin cứng phiên bản trong `pyproject.toml` và `uv.lock`.

## 8. Verifier contract (Thành viên 5)

Code: `src/student_agent/evidence.py`, `src/student_agent/verifier.py`, test: `tests/test_verifier.py`.

**Tích hợp trong `workflow.py` (Coordinator):**

```text
case_received (CLI)
→ task_assigned ×3 (coordinator → order-agent, payment_agent, shipment_policy_agent)
→ với từng specialist: handoff coordinator→agent, agent gọi MCP (tool_result_consumed),
  handoff agent→coordinator (kèm evidence_refs mới)
→ task_assigned + handoff coordinator→verifier
→ policy_decided, verification_completed, handoff verifier→coordinator
→ case_finalized (CLI)
```

- Specialist nhận `RecordingGateway` thay cho gateway gốc: mọi response MCP thành công được ghi vào
  `EvidenceLedger` (một ledger cho cả lượt chạy) với `actor` là agent đang chạy. Agent không cần tự ghi evidence.
- Verifier dựng `SpecialistReport` từ ledger theo actor; `proposed_issue` lấy từ `payment_analysis.detected_issue`
  và từ shipment report (chỉ khi là `late_delivery_*`).
- Specialist lỗi không làm hỏng case: coordinator ghi `handoff` với `decision_code=SPECIALIST_FAILED` và
  Verifier kết luận trên evidence còn lại.
- Agent mới có thể dùng `collect_evidence(...)` trong `evidence.py` để gọi tool + emit `tool_result_consumed`.

**Verifier làm gì:**

1. Chỉ nhận evidence có trong `EvidenceLedger` và thuộc đúng `case_id`; evidence lạ bị loại (đếm vào trace).
2. Lọc bản ghi nhiễu theo vòng đời đơn hàng: item có `shipping_limit_date` trong `[purchase, estimated_delivery]`,
   capture trong `[purchase, approved + 1 ngày]`, refund/payment event trong `[purchase, opened_at]`,
   shipment event trong `[purchase, max(opened_at, delivered) + 1 ngày]`; bỏ bản ghi trùng lặp y hệt.
3. Tự kiểm chứng `primary_issue` theo thứ tự ưu tiên: refund failed → refund pending → order canceled/unavailable
   đã capture → reconciliation mismatch mở → capture lặp vượt tổng đơn (duplicate) / tổng capture = tổng đơn (split)
   → giao trễ theo timestamp của order row (seller nếu bàn giao carrier sau `shipping_limit_date`, ngược lại logistics)
   → `unsupported_claim`. Thiếu order → `insufficient_evidence`. Lời khai của khách không phải ground truth.
4. So với `proposed_issue` của specialist: bất đồng thì giảm confidence; chỉ nhận đề xuất của specialist khi
   Verifier không tìm thấy tín hiệu nào (`unsupported_claim`).
5. Áp policy: `case_status`, `recommended_action`, bên chịu trách nhiệm. `party_id` seller trong policy là ví dụ
   của order khác nên được thay bằng seller thực của case. Số tiền hoàn tính từ dữ liệu (hoàn phí ship bị chặn
   bởi số tiền đã capture), `refund_pending` không hoàn thêm.
6. Chỉ trích evidence liên quan tới issue (bảng `CITATIONS`), không trích `get_customer_history`/`get_product_context`.
   `payment_references`/`shipment_ids` chỉ điền khi dữ liệu có id thật, không tự tạo.
7. Kiểm invariant (ref thuộc case, `no_action` ⇒ refund 0, tổng `refund_lines` = refund, refund ≤ số đã capture,
   seller chịu trách nhiệm ∈ `affected_entities.seller_ids`, confidence ∈ [0,1]), sau đó validate JSON Schema.
8. Emit trace: `policy_decided` → `verification_completed` (kèm toàn bộ evidence_refs của output) →
   `handoff` verifier → coordinator. Coordinator vẫn phải emit `task_assigned` và `handoff` sang specialist/verifier.
