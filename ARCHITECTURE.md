# L3A Architecture Record: Multi-Agent MCP + A2A

Tài liệu này ghi nhận quyết định thiết kế kiến trúc hệ thống Multi-Agent điều tra khiếu nại thương mại điện tử cho bài thi **K4 L3A — Multi-Agent MCP + A2A**, tuân thủ 100% public contracts tại `contracts/schemas/` và `contracts/scoring/`.

---

## 1. System Overview

Hệ thống được thiết kế theo mô hình **Pipeline Phối Hợp Đa Tác Tử (A2A Directed Acyclic Graph - DAG)**. Dữ liệu từ case đầu vào được điều phối tuần tự và song song qua các Agent chuyên biệt, tương tác có thẩm quyền với **MCP Evidence Gateway**, được thẩm định bởi **Verifier Agent** trước khi tạo output và trace log.

```text
               ┌────────────────────────────────────────┐
               │          Customer Case Input           │
               │        (inputs/<case_id>.json)         │
               └───────────────────┬────────────────────┘
                                   │
                                   ▼ [event: case_received]
               ┌────────────────────────────────────────┐
               │          Coordinator / Router          │
               │   - Trích xuất claim, order_id         │
               │   - Lập kế hoạch phân công tác vụ      │
               └───────────────────┬────────────────────┘
                                   │
                     [event: task_assigned / handoff]
                                   │
         ┌─────────────────────────┼─────────────────────────┐
         ▼                         ▼                         ▼
┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
│ Order/Item Ag.  │       │  Payment Agent  │       │ Shipment Agent  │
│  - get_order    │       │- get_order_     │       │- get_shipment_  │
│  - get_order_   │       │  payments       │       │  summary        │
│    items        │       │- get_payment_   │       │                 │
│  - get_sellers  │       │  timeline       │       │                 │
│  - get_product_ │       │- get_refund_    │       │                 │
│    context      │       │  timeline       │       │                 │
└────────┬────────┘       └────────┬────────┘       └────────┬────────┘
         │                         │                         │
         │   [event: tool_result_consumed]                  │
         │   [MCP Evidence: order, item, seller, payment,    │
         │                  shipment data + evidence_refs]   │
         │                         │                         │
         └─────────────────────────┼─────────────────────────┘
                                   │
                                   ▼ [event: handoff]
               ┌────────────────────────────────────────┐
               │              Policy Agent              │
               │   - MCP: get_policy, get_customer_     │
               │          history                       │
               │   - So khớp điều khoản hoàn tiền       │
               │   - Xác định root cause & party        │
               └───────────────────┬────────────────────┘
                                   │
                                   ▼ [event: policy_decided / handoff]
               ┌────────────────────────────────────────┐
               │             Verifier Agent             │
               │   - Kiểm tra bất biến (Invariants)     │
               │   - Khớp tiền hoàn & refund_lines      │
               │   - Lọc evidence_ref hợp lệ            │
               │   - Kiểm định JSON Schema V2           │
               └───────────────────┬────────────────────┘
                                   │
                                   ▼ [event: verification_completed]
                                   │ [event: case_finalized]
         ┌─────────────────────────┴─────────────────────────┐
         ▼                                                   ▼
┌──────────────────────────────┐            ┌──────────────────────────────┐
│  outputs/<case_id>.json      │            │     traces/trace.jsonl       │
│  (l3a-output-v2.schema.json) │            │ (trace-event-v1.schema.json) │
└──────────────────────────────┘            └──────────────────────────────┘
```

---

## 2. Agent Ownership & Tool Permissions

Nhằm đảm bảo nguyên tắc đặc quyền tối thiểu (Least Privilege) và kiểm soát chặt chẽ audit log của MCP, mỗi Agent chỉ được phép truy xuất đúng tập công cụ thuộc miền dữ liệu của mình:

| Actor | Input | Trách nhiệm chính | Tool được phép gọi | Output / Handoff |
| :--- | :--- | :--- | :--- | :--- |
| **coordinator** | `case` raw JSON từ `inputs/` | Phân tích cấu trúc khiếu nại, trích xuất `claimed_order_id`, phát hành `case_received`, phân công nhiệm vụ cho các Specialist. | *Không gọi MCP* | `InvestigationPlan`: danh sách task & parameters gửi tới các Specialist. |
| **order-agent** | `case_id`, `order_id` | Xác minh thông tin đơn hàng thực tế trên hệ thống: trạng thái đơn, danh mục mặt hàng, người bán liên quan, phân loại sản phẩm. | `get_order`<br>`get_order_items`<br>`get_sellers`<br>`get_product_context` | `OrderContext`: order status, item IDs, seller IDs, giá tiền từng item, danh mục hàng, `evidence_refs`. |
| **payment-agent** | `case_id`, `order_id` | Điều tra lịch sử dòng tiền: tổng số tiền đã thanh toán, phương thức trả (credit card, boleto, voucher...), các đợt chia tiền (split payment), trạng thái refund. | `get_order_payments`<br>`get_payment_timeline`<br>`get_refund_timeline` | `PaymentContext`: danh sách payment references, tổng tiền đã thu, tình trạng giao dịch trùng/lệch, `evidence_refs`. |
| **shipment-agent** | `case_id`, `order_id` | Điều tra vận chuyển: thời gian giao hàng dự kiến (`estimated_delivery`), ngày giao thực tế (`delivered_date`), đơn vị vận chuyển, xác định trễ hạn do bên nào. | `get_shipment_summary` | `ShipmentContext`: shipment IDs, carrier, timeline giao hàng, số ngày trễ (nếu có), `evidence_refs`. |
| **policy-agent** | Context từ 3 Specialist + `customer_request.claims` + `policy_version` | Tra cứu điều khoản bồi thường; đối chiếu khiếu nại của khách với bằng chứng xác thực; xác định lỗi chính (`primary_issue`), nguyên nhân gốc rễ (`root_cause_analysis`), bên chịu trách nhiệm và mức hoàn tiền đề xuất. | `get_policy`<br>`get_customer_history` | `PolicyVerdict`: `assessment`, `claim_assessments`, `root_cause_analysis`, `preliminary_refund`, `resolution_actions`, `evidence_refs`. |
| **verifier** | `PolicyVerdict` + toàn bộ context & `evidence_refs` đã thu thập | Chạy toàn bộ các quy tắc bất biến kiểm tra tính toàn vẹn; đối chiếu số liệu tài chính; lọc bỏ bằng chứng ngoài luồng; kiểm tra JSON schema trước khi chốt case. | *Không gọi MCP* | `FinalCaseOutput`: Payload hoàn chỉnh sẵn sàng ghi vào `outputs/<case_id>.json`. |

---

## 3. A2A (Agent-to-Agent) Protocol

### 3.1. Message Envelope Nội Bộ
Mỗi thông điệp trao đổi giữa các Agent mang cấu trúc thống nhất:
```python
{
    "case_id": str,
    "source_actor": str,
    "target_actor": str,
    "handoff_type": str,  # "task_dispatch" | "context_return" | "verdict_handoff"
    "payload": dict,      # Dữ liệu nghiệp vụ tương ứng
    "evidence_refs": list[str]
}
```

### 3.2. Correlation & Đồng Bộ
* Mọi hành động, envelope, MCP tool call và trace event đều gắn chặt với `case_id`.
* **Song song hóa chuyên gia**: 3 Specialist (`order-agent`, `payment-agent`, `shipment-agent`) được điều phối chạy song song thông qua `asyncio.gather()` sau khi `coordinator` phát sự kiện `task_assigned`.
* **Tránh vòng lặp (Loop Prevention)**: Quy trình là một đồ thị có hướng không chu trình (Strict DAG) đi theo 4 pha:
  `Coordinator` $\rightarrow$ `Specialists (Parallel)` $\rightarrow$ `Policy Agent` $\rightarrow$ `Verifier Agent`.
  Không cho phép Specialist gọi vòng lại Coordinator.
* **Trace Events Phát Hành**: Tuân thủ chuẩn `day09-trace-event-v1`:
  1. `case_received` (bởi coordinator)
  2. `task_assigned` (coordinator $\rightarrow$ specialist)
  3. `tool_result_consumed` (khi specialist nhận được response hợp lệ từ MCP)
  4. `handoff` (khi chuyển giao dữ liệu sang Policy và Verifier)
  5. `policy_decided` (khi Policy Agent đưa ra phán quyết nghiệp vụ)
  6. `verification_completed` (khi Verifier Agent thẩm định thành công)
  7. `case_finalized` (bởi coordinator khi output đã ghi đĩa)

---

## 4. Evidence Lifecycle & Audit Provenance

1. **Khám phá Tool**: Luôn dùng `gateway.list_tools()` khi khởi động để xác định các tool khả dụng, không gọi các tool nằm ngoài discovery.
2. **Gọi Tool**: Mọi lệnh gọi MCP bắt buộc truyền `case_id=case["case_id"]`.
3. **Thẩm định Envelope**: Response trả về phải khớp với schema `mcp-evidence-response-v1.schema.json` (chứa `evidence_ref`, `result_hash`, `domain`, `data`).
4. **Ghi Nhận Tiêu Thụ Bằng Chứng**:
   Ngay sau khi nhận evidence, Agent phải phát sự kiện trace:
   ```python
   trace.emit(
       case_id=case["case_id"],
       event_type="tool_result_consumed",
       actor=actor_name,
       tool_name=tool_name,
       evidence_refs=[evidence["evidence_ref"]],
   )
   ```
5. **Cách ly Tuyệt đối giữa các Case**:
   * Bộ gom `evidence_ref` được tạo mới theo từng phiên xử lý `solve_case()`.
   * Tuyệt đối không lưu cache `evidence_ref` vào biến toàn cục hoặc tái sử dụng giữa các case khác nhau.
   * Danh sách `evidence_refs` trong output cuối cùng chỉ chứa các `evidence_ref` thực tế đã nhận từ gateway trong chính case đó.

---

## 5. Failure Policy & Resilience

| Sự cố (Failure) | Retry? | Chiến lược Fallback | Trace Event / Decision Code |
| :--- | :--- | :--- | :--- |
| **MCP Timeout / Network drop** | Có (tối đa 2 lần, exponential backoff: 1s, 2s). Idempotent call. | Nếu sau 2 lần vẫn timeout, đánh dấu thiếu dữ liệu domain đó; chuyển trạng thái case sang `needs_investigation`. Không bịa dữ liệu. | `decision_code: "MCP_TIMEOUT_FALLBACK"` |
| **Dữ liệu Không Tồn Tại (Not Found / 404)** | Không retry. | Ghi nhận thực thể không tồn tại trên hệ thống. Đánh giá claim liên quan là `unsupported` hoặc `insufficient_evidence`. | `decision_code: "ENTITY_NOT_FOUND"` |
| **Mâu Thuẫn Dữ Liệu (Source Conflict)** | Không retry. | Ghi nhận vào trường `data_conflicts`. Ưu tiên nguồn dữ liệu có thẩm quyền cao hơn: `MCP authoritative data` > `customer claim`. | `decision_code: "RESOLVED_VIA_MCP_TRUTH"` |
| **Verifier Invariant Vi phạm** | Có (thử cân chỉnh 1 lần nội bộ). | Nếu không giải quyết được xung đột nghiệp vụ, hạ `confidence` xuống $\le 0.5$, chuyển `case_status` thành `needs_investigation`, set `recommended_refund_brl: 0.0`. | `decision_code: "VERIFIER_FALLBACK_SAFE_MODE"` |

---

## 6. Verification Invariants (Quy Tắc Bất Biến Bắt Buộc)

Trước khi Verifier phê duyệt xuất dữ liệu ra file `outputs/<case_id>.json`, payload phải vượt qua 7 bất biến kiểm tra:

1. **Schema Compliance**:
   * Kiểm tra thông qua `Contracts.validate_output()`, đảm bảo đạt 100% tiêu chuẩn JSON Schema Draft 2020-12 của `day09-l3a-output-v2`.
2. **Entity Scope**:
   * Các ID trong `affected_entities` (`order_ids`, `item_ids`, `seller_ids`, `payment_references`, `shipment_ids`) phải là các ID thực tế xuất hiện trong dữ liệu trả về từ MCP, loại bỏ các ID giả mạo khách tự xưng nếu không tồn tại.
3. **Evidence Provenance & Ownership**:
   * Tất cả `evidence_ref` xuất hiện trong `evidence_refs`, `claim_assessments[*].evidence_refs` phải nằm trong tập hợp các `evidence_ref` được trả về từ MCP Gateway trong lần chạy của case hiện tại.
4. **Claim Linkage**:
   * Mọi `claim_id` có trong `case.customer_request.claims` đều phải được đánh giá trong `claim_assessments` với một trong 4 verdict chuẩn: `supported`, `unsupported`, `partially_supported`, `insufficient_evidence`.
5. **Financial Arithmetic Consistency**:
   * Tổng số tiền `amount_brl` trong tất cả các dòng của `refund_lines` phải bằng chính xác giá trị `recommended_refund_brl` (sai số làm tròn $\le 0.01$).
   * Nếu `case_status == "no_action"` $\rightarrow$ `recommended_refund_brl` bắt buộc bằng `0.0` và `refund_lines` rỗng.
   * Nếu `recommended_refund_brl > 0` $\rightarrow$ bắt buộc phải có ít nhất 1 dòng trong `refund_lines`.
6. **Responsibility & Action Consistency**:
   * Nếu `primary_issue` là lỗi do người bán (ví dụ `late_delivery_seller`) $\rightarrow$ `responsible_parties` phải chứa bên có `party_type: "seller"`.
   * Nếu `primary_issue` do vận chuyển (`late_delivery_logistics`) $\rightarrow$ `responsible_parties` phải có `logistics_provider`.
   * `resolution_actions` không được chứa phần tử trùng lặp và không vượt quá 8 hành động.
7. **Confidence Calibration**:
   * Giá trị `confidence` thuộc khoảng số thực $[0.0, 1.0]$.
   * Không gán cố định `1.0`. Nếu đầy đủ chứng cứ: $0.85 - 0.95$; nếu phát hiện mâu thuẫn hoặc thiếu dữ liệu: $0.4 - 0.65$.

---

## 7. Reproducibility & Runtime Boundaries

* **Môi trường**: Python 3.11 / 3.12, chạy trên môi trường ảo `.venv`.
* **Dependencies**: Đóng băng theo `pyproject.toml` (`httpx2>=2,<3`, `mcp>=2,<3`, `jsonschema[format]>=4.25,<5`, `python-dotenv>=1.1,<2`).
* **Concurrency Limit**: Chạy tuần tự từng case trong batch 100 cases (đảm bảo không vượt quá budget và audit rate limit của server thi đấu).
* **Lệnh chạy chuẩn**:
  ```bash
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
* **Bảo mật**: Không bao giờ ghi trực tiếp `COMPETITION_TEAM_API_KEY` hoặc các bí mật vào output, trace hay Git repository.
