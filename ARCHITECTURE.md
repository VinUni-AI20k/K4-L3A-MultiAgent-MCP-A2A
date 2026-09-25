# L3A Architecture Record

Hồ sơ kiến trúc hệ thống Multi-Agent điều tra khiếu nại thương mại điện tử (Day09 L3A Multi-Agent MCP + A2A).

---

## 👥 THÀNH VIÊN DỰ ÁN

| STT | Họ và Tên | Mã Học Viên | Vai Trò Chính | Tác Tử Phụ Trách |
| :---: | :--- | :---: | :--- | :--- |
| 1 | **Chử Trần Phương Nam** | `2A202602675` | **Team Lead & Workflow Orchestrator** | `CoordinatorAgent` (`agents/coordinator.py`, `workflow.py`) |
| 2 | **Ngụy Khắc Phi Long** | `2A202602532` | **Order & Logistics Specialist** | `OrderLogisticsAgent` (`agents/order_logistics.py`) |
| 3 | **Nguyễn Đức Phát** | `2A202602753` | **Payment & Resolution Specialist** | `PaymentResolutionAgent` (`agents/payment_resolution.py`) |
| 4 | **Đỗ Thành Đạt** | `2A202602874` | **QA Engineer & Safety Gatekeeper** | `Verifier` (`agents/verifier.py`, Testing & Release) |

---

## 1. System overview

Hệ thống được thiết kế theo mô hình điều phối tuần tự 4 tác tử chuyên biệt (Sequential Multi-Agent Architecture giữa 4 thành viên), loại bỏ xung đột dữ liệu chéo, bảo đảm tính xác thực của chứng cứ và tuân thủ các điều kiện kiểm định của ban tổ chức:

```text
[inputs/<case_id>.json]
         │
         ▼
 ┌──────────────────────────────┐  task_assigned   ┌─────────────────────────────┐  Tool Calls
 │         Coordinator          │ ───────────────► │     OrderLogisticsAgent     │ ──────────► MCP Evidence Gateway
 │ (Chử Trần Phương Nam)        │ ◄─────────────── │    (Ngụy Khắc Phi Long)     │ ◄────────── (order, items, shipment)
 └──────────────┬───────────────┘     handoff      └─────────────────────────────┘
                │
                │          task_assigned   ┌─────────────────────────────┐  Tool Calls
                ├────────────────────────► │   PaymentResolutionAgent    │ ──────────► MCP Evidence Gateway
                │ ◄─────────────────────── │     (Nguyễn Đức Phát)       │ ◄────────── (payments, timeline, refund)
                │             handoff      └─────────────────────────────┘
                │
                ▼
 ┌──────────────────────────────┐
 │           Verifier           │ ────► Thẩm định độc lập: Schema (100%), Invariants, Deduplication & Quality Gate
 │        (Đỗ Thành Đạt)        │
 └──────────────┬───────────────┘
                │
                ├───► outputs/<case_id>.json  (JSON Output chuẩn format l3a-output-v2)
                └───► traces/trace.jsonl      (Observable Life-cycle Events)
```

---

## 2. Phân vai và trách nhiệm cụ thể của 4 thành viên (Agent Ownership)

Mỗi thành viên phụ trách một tác tử với ranh giới trách nhiệm và ma trận phân quyền công cụ MCP chặt chẽ (Least Privilege Principle):

| Thành viên / Actor | Phạm vi Input | Trách nhiệm cốt lõi | Output / Bàn giao (Handoff) | Danh mục MCP Tools được cấp quyền |
| :--- | :--- | :--- | :--- | :--- |
| **Chử Trần Phương Nam**<br>(`Coordinator`) | `inputs/<case_id>.json` | **Điều phối hệ thống & Phân tích nguyên nhân gốc rễ**: Tiếp nhận case, quản lý vòng đời A2A, kích hoạt tuần tự các specialist agent, tổng hợp nhận định, xác định `primary_issue`, xây dựng `root_cause_analysis` và chỉ định `responsible_parties`. | Phán quyết sơ bộ tổng thể, phân công task cho TV2 (Long) và TV3 (Phát). | *Không gọi trực tiếp công cụ dữ liệu* |
| **Ngụy Khắc Phi Long**<br>(`OrderLogisticsAgent`) | `case_id`, `claimed_order_id` | **Điều tra Đơn hàng, Sản phẩm & Giao vận**: Gọi MCP tra cứu trạng thái đơn, bóc tách `item_ids`, `seller_ids`, tính toán giá trị hàng hóa và cước vận chuyển, phân tích các mốc thời gian giao nhận để phân định lỗi trễ hạn (`late_delivery_seller` vs `late_delivery_logistics`). | `OrderLogisticsResult`: `order_status`, `items`, `sellers`, `order_total_brl`, `delay_party`, bằng chứng vận chuyển. | `get_order`<br>`get_order_items`<br>`get_shipment_summary` |
| **Nguyễn Đức Phát**<br>(`PaymentResolutionAgent`) | `order_id`, `OrderLogisticsResult`, `claims` | **Đối soát Tài chính & Tính toán Bồi hoàn**: Gọi MCP kiểm tra thanh toán, dòng thời gian giao dịch và tiến trình hoàn tiền; phát hiện `canceled_order_paid`, `unavailable_order_paid`, `duplicate_charge`, `refund_pending`, `refund_failed`, `payment_mismatch`, `valid_split_payment`; tính toán số tiền `recommended_refund_brl`, lập danh sách `refund_lines` và đánh giá từng `claim`. | `PaymentResolutionResult`: `total_paid_brl`, `suggested_issue`, `recommended_refund_brl`, `refund_lines`, `claim_assessments`. | `get_order_payments`<br>`get_payment_timeline`<br>`get_refund_timeline`<br>`get_policy` |
| **Đỗ Thành Đạt**<br>(`Verifier`) | Raw Output Dictionary từ Coordinator | **Thẩm định An toàn, Kiểm thử & Đóng gói (QA / Safety Gatekeeper)**: Hoạt động độc lập như một kiểm định viên trước khi xuất kết quả: thẩm định 100% JSON Schema, kiểm tra tính nhất quán tài chính (Consistency Invariants), kiểm tra không trùng lặp (Deduplication), quản lý quá trình test tự động (`day09 validate`) và đóng gói (`day09 package`). | Verified Final JSON Output sẵn sàng ghi file `outputs/<case_id>.json`. | *Không gọi MCP Tool (sử dụng Schema Validator cục bộ)* |

---

## 3. Giao thức A2A giữa 4 thành viên (A2A Protocol)

1. **Correlation Key**: Toàn bộ luồng giao tiếp giữa 4 thành viên đều đồng bộ theo `case_id` duy nhất, không dùng dữ liệu chéo giữa các phiên.
2. **Chuỗi sự kiện quan sát được (Observable Trace Events)**:
   * `case_received` (Actor: `coordinator` - Chử Trần Phương Nam): Tiếp nhận case từ input.
   * `task_assigned` (Actor: `coordinator` $\rightarrow$ Target: `order-logistics-agent` - Ngụy Khắc Phi Long): Giao nhiệm vụ điều tra đơn hàng và vận chuyển.
   * `tool_result_consumed` (Actor: `order-logistics-agent` - Ngụy Khắc Phi Long): Thu thập bằng chứng từ MCP.
   * `handoff` (Actor: `order-logistics-agent` $\rightarrow$ Target: `coordinator`): Bàn giao kết quả đơn hàng.
   * `task_assigned` (Actor: `coordinator` $\rightarrow$ Target: `payment-resolution-agent` - Nguyễn Đức Phát): Giao nhiệm vụ đối soát tiền kèm dữ liệu đơn từ Long.
   * `tool_result_consumed` (Actor: `payment-resolution-agent` - Nguyễn Đức Phát): Thu thập bằng chứng thanh toán và hoàn tiền.
   * `handoff` (Actor: `payment-resolution-agent` $\rightarrow$ Target: `coordinator`): Bàn giao phương án tài chính.
   * `verification_completed` (Actor: `verifier` - Đỗ Thành Đạt): Đạt hoàn tất thẩm định tính nhất quán và schema.
   * `case_finalized` (Actor: `coordinator` - Chử Trần Phương Nam): Xuất file output chính thức.
3. **Bảo mật tuyệt đối**: Không trace chuỗi suy luận riêng (Chain-of-Thought) hay thông tin bí mật (API Key) vào trace log.

---

## 4. Evidence lifecycle

1. **Thẩm định Envelope**: Mỗi phản hồi từ MCP được kiểm định cấu trúc qua `mcp-evidence-response-v1.schema.json`.
2. **Khai thác `evidence_ref`**: Trích xuất mã băm bằng chứng định dạng `^ev_[A-Za-z0-9_-]{20,96}$`.
3. **Gắn vết Audit tức thì**: Kích hoạt sự kiện `tool_result_consumed` ngay sau khi nhận kết quả để bảo đảm tiêu chí chấm `provenance` (15%).
4. **Khử trùng lặp và liên kết**: Toàn bộ bằng chứng được gom về mảng `evidence_refs` của case và gắn tương ứng vào từng mục trong `claim_assessments`. Không sử dụng `evidence_ref` giả hoặc tái sử dụng ref giữa các case.

---

## 5. Failure policy

Hệ thống thiết lập cơ chế xử lý ngoại lệ theo nguyên tắc Idempotent và Graceful Degradation:

| Tình huống lỗi | Cơ chế Retry? | Phương án Fallback | Ghi nhận Trace / Log | Thành viên Phụ trách |
| :--- | :---: | :--- | :--- | :--- |
| **MCP Network Timeout** | Có (1 lần, backoff) | Tiếp tục phân tích với các bằng chứng đã thu thập được; không chặn đứng toàn bộ pipeline. | `logger.warning` | Ngụy Khắc Phi Long / Nguyễn Đức Phát |
| **Tool Execution Error (500/NotFound)** | Không | Bắt ngoại lệ qua khối `try/except`, gán giá trị mặc định an toàn (`empty dict/list`). | `logger.info` | Ngụy Khắc Phi Long / Nguyễn Đức Phát |
| **Thiếu dữ liệu / Missing Evidence** | Không | Tuyệt đối không suy đoán hoặc tạo ref giả; đánh giá claim là `unsupported` hoặc `insufficient_evidence`. | `verdict: "unsupported"` | Nguyễn Đức Phát |
| **Lệch cấu trúc Schema** | Không | Verifier tự động điều chỉnh tổng tiền hoặc chặn lại để xử lý. | Báo lỗi tại `validate_output` | Đỗ Thành Đạt |

---

## 6. Verification invariants (Chốt chặn của Đỗ Thành Đạt)

Trước khi chấp thuận ghi dữ liệu vào `outputs/<case_id>.json`, Đỗ Thành Đạt (`Verifier`) kiểm tra 6 ràng buộc bất biến:
1. **Tính nhất quán tài chính**: `recommended_refund_brl == sum(line["amount_brl"] for line in refund_lines)`.
2. **Ràng buộc trạng thái `no_action`**: Nếu `case_status == "no_action"`, bắt buộc `recommended_refund_brl == 0.0` và `refund_lines == []`.
3. **Ràng buộc bên chịu trách nhiệm**:
   * Nếu `primary_issue == "late_delivery_seller"`, `responsible_parties` phải chứa `party_type: "seller"`.
   * Nếu `primary_issue == "late_delivery_logistics"`, `responsible_parties` phải chứa `party_type: "logistics_provider"`.
   * Nếu `primary_issue == "duplicate_charge"` hoặc `"refund_failed"`, `responsible_parties` phải chứa `party_type: "payment_provider"`.
4. **Tính độc bản (Uniqueness)**: Các mảng `evidence_refs`, `payment_references`, `resolution_actions` không được chứa phần tử trùng lặp (`uniqueItems: true`).
5. **Tiền tệ chuẩn mực**: Định dạng tiền tệ duy nhất là `"currency": "BRL"`.
6. **Tuân thủ Schema tuyệt đối**: Pass 100% kiểm định của `day09-l3a-output-v2.schema.json`.

---

## 7. Reproducibility

Môi trường và quy trình để tái lập hoàn toàn kết quả:
* **Phiên bản Python**: `Python 3.11.16` (quản lý qua môi trường Conda `vin`).
* **Dependencies chính thức**: `httpx2>=2,<3`, `mcp>=2,<3`, `jsonschema[format]>=4.25,<5`, `python-dotenv>=1.1,<2`.
* **Cấu hình mạng HTTP**: Timeout 600s, connect 60s, read 120s, keepalive expiry 120s nhằm triệt tiêu lỗi ngắt kết nối giữa chừng.
* **Lệnh thực thi chuẩn**:
  ```bash
  # 1. Kích hoạt môi trường
  conda activate vin

  # 2. Chạy toàn bộ 100 cases
  day09 run

  # 3. Thẩm định output và trace (Đỗ Thành Đạt nghiệm thu)
  day09 validate

  # 4. Đóng gói nộp bài
  day09 package --output dist/submission.zip
  ```
