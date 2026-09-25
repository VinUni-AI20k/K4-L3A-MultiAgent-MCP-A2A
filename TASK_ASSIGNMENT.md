# 📋 BẢNG PHÂN CÔNG CÔNG VIỆC DỰ ÁN K4-L3A
**Hệ thống Multi-Agent Điều tra Khiếu nại Thương mại Điện tử (MCP + A2A)**

---

## 🎯 1. TỔNG QUAN & TIÊU CHÍ ĐÁNH GIÁ

Dự án yêu cầu xây dựng hệ sinh thái **Multi-Agent (A2A)** phối hợp điều tra các khiếu nại của khách hàng bằng cách thu thập bằng chứng từ **MCP Evidence Gateway** và sinh kết quả đầu ra chuẩn schema JSON V2.

### Trọng số tính điểm (Scoring Weights)
| Thành phần | Trọng số | Mô tả ngắn |
| :--- | :---: | :--- |
| **Độ đúng nghiệp vụ (`semantic`)** | **45%** | Đúng vấn đề chính (`primary_issue`), phân tích nguyên nhân, số tiền bồi hoàn. |
| **Chất lượng bằng chứng (`evidence`)** | **15%** | F1 coverage nhóm bằng chứng bắt buộc, trích dẫn đúng trọng tâm. |
| **Xác thực MCP Audit (`provenance`)** | **15%** | Mọi `evidence_ref` phải xuất phát từ MCP Server thật của BTC qua audit log. |
| **Tính nhất quán dữ liệu (`consistency`)** | **10%** | Khớp giữa trạng thái case, số tiền refund, bên chịu trách nhiệm và hành động. |
| **Chuẩn JSON Schema (`schema`)** | **5%** | Tuyệt đối tuân thủ `l3a-output-v2.schema.json`. |
| **Độ tự tin hiệu chuẩn (`calibration`)** | **5%** | Mức độ tự tin (`confidence` 0.0 - 1.0) tương quan với độ chính xác. |
| **Quy trình Multi-Agent (`workflow`)** | **5%** | Vòng đời sự kiện trong trace log đầy đủ và đúng thứ tự. |

> ⚠️ **CẢNH BÁO HARD GATES (0 ĐIỂM TOÀN CASE):**
> 1. Sai `case_id`.
> 2. Output không pass JSON Schema.
> 3. Tự chế tác / bịa đặt `evidence_ref` giả.
> 4. Dùng `evidence_ref` chéo giữa các case khác nhau.
> 5. Thiếu bằng chứng bắt buộc.

---

## 🏗️ 2. MÔ HÌNH KIẾN TRÚC & LUỒNG PHỐI HỢP (A2A)

```text
                       [ Input Case ]
                             │
                             ▼
                 ┌───────────────────────┐
                 │    1. COORDINATOR     │ ◄─── Quản lý Trace & Task Dispatch
                 └───────────┬───────────┘
                             │
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│ 2. ORDER AGENT  │ │3. LOGISTICS AGT │ │4. PAYMENT AGENT │
│ (Order/Items)   │ │ (Shipment/SLA)  │ │ (Money/Refund)  │
└────────┬────────┘ └────────┬────────┘ └────────┬────────┘
         │                   │                   │
         └───────────────────┼───────────────────┘
                             ▼
                 ┌───────────────────────┐
                 │   5. VERIFIER AGENT   │ ◄─── Policy, Conflict, Schema & Gates
                 └───────────┬───────────┘
                             │
                             ▼
                    [ Final Output JSON ]
```

---

## 👥 3. PHÂN CÔNG CHI TIẾT 5 THÀNH VIÊN

---

### 👤 THÀNH VIÊN 1: Lead Architect & Coordinator Agent
**Chuyên môn:** Kiến trúc hệ thống, Điều phối luồng A2A, Quản lý Trace & Pipeline vận hành

* **File/Thư mục sở hữu chính:**
  * `src/student_agent/workflow.py` (Hàm `solve_case`)
  * `src/student_agent/agents/coordinator.py`
  * `ARCHITECTURE.md` (Phần 1, 3, 5, 7)
* **Trách nhiệm chính:**
  1. **Khung xương luồng (Orchestrator):** Tiếp nhận `case`, khởi tạo Case Context, bóc tách `claimed_order_id`, gọi tuần tự hoặc song song các Specialist Agents.
  2. **Vòng đời Trace Event:** Đảm bảo trace ghi đủ và đúng trình tự các sự kiện bắt buộc:
     * `case_received` (bắt đầu)
     * `task_assigned` (giao việc cho các agent chuyên môn)
     * `handoff` (chuyển giao dữ liệu giữa các agent)
     * `case_finalized` (hoàn tất case)
  3. **Xử lý sự cố (Failure Policy):** Xử lý timeout MCP Gateway, cơ chế retry có giới hạn, không để script bị văng exception làm gián đoạn batch 100 cases.
  4. **Vận hành & Đóng gói:** Thiết lập môi trường, chạy kiểm thử batch (`day09 run`), đóng gói submission (`day09 package`).
* **Định nghĩa hoàn thành (DoD):**
  * `workflow.py` chạy xuyên suốt không crash.
  * File `traces/trace.jsonl` vượt qua `Contracts.validate_trace()` 100%.

---

### 👤 THÀNH VIÊN 2: Order & Customer Claims Specialist Agent
**Chuyên môn:** Bóc tách khiếu nại khách hàng, Điều tra Đơn hàng, Mặt hàng & Lịch sử khách hàng

* **File/Thư mục sở hữu chính:**
  * `src/student_agent/agents/order_agent.py`
  * Actor name trong Trace: `order-agent`
  * Khối Output: `affected_entities.order_ids`, `affected_entities.item_ids`, `claim_assessments` (Order claims)
* **MCP Tools thực tế phụ trách (4 tools):**
  1. `get_order`: Lấy thông tin tổng quan đơn hàng (status, timestamps, customer_id).
  2. `get_order_items`: Lấy chi tiết danh sách sản phẩm, giá, phí ship, mã seller của từng item.
  3. `get_product_context`: Lấy thông tin ngữ cảnh sản phẩm, danh mục, thông số kỹ thuật.
  4. `get_customer_history`: Lấy lịch sử mua hàng và độ tin cậy của khách hàng.
* **Trách nhiệm chính:**
  1. **Phân tích yêu cầu:** Bóc tách `customer_request.claims`, trích xuất `claim_id` và chủ đề khiếu nại.
  2. **Thu thập bằng chứng Đơn hàng:** Gọi 4 tool MCP tương ứng, lưu lại `evidence_ref`. Emit event `tool_result_consumed` với `actor="order-agent"`.
  3. **Đánh giá các vấn đề nghiệp vụ:**
     * `canceled_order_paid`: Khách đã thanh toán nhưng trạng thái đơn hàng bị hủy trên hệ thống.
     * `unavailable_order_paid`: Đơn hàng bị hết hàng / nhà cung cấp hủy bỏ do không có sẵn sản phẩm.
     * `unsupported_claim`: Khiếu nại của khách không đúng với thực tế dữ liệu đơn hàng.
  4. **Xuất kết quả nhánh:** Sinh danh sách `claim_assessments` (gồm `claim_id`, `verdict`, `confidence`, `evidence_refs`) cho nhánh Order.
* **Định nghĩa hoàn thành (DoD):**
  * Gom đầy đủ các ID vào `order_ids` và `item_ids`.
  * Không đoán mò dữ liệu; tất cả nhận định về trạng thái đơn hàng đều có `evidence_ref` từ 4 tool trên.

---

### 👤 THÀNH VIÊN 3: Shipment & Seller Specialist Agent
**Chuyên môn:** Hành trình Vận chuyển, SLA Người bán & Phân tích Nguyên nhân gốc rễ

* **File/Thư mục sở hữu chính:**
  * `src/student_agent/agents/shipment_agent.py` *(lưu ý: đặt tên file là shipment_agent.py)*
  * Actor name trong Trace: `shipment-agent`
  * Khối Output: `affected_entities.seller_ids`, `affected_entities.shipment_ids`, `root_cause_analysis`
* **MCP Tools thực tế phụ trách (2 tools):**
  1. `get_shipment_summary`: Lấy thông tin vận chuyển, tracking timeline, ngày bàn giao bưu cục, hạn giao dự kiến, ngày nhận hàng thực tế.
  2. `get_sellers`: Lấy thông tin người bán, địa chỉ, SLA cam kết của seller.
* **Trách nhiệm chính:**
  1. **Điều tra Vận chuyển:** Gọi `get_shipment_summary` và `get_sellers` để đối chiếu mốc thời gian giao hàng thực tế vs SLA cam kết.
  2. **Phân định trách nhiệm giao hàng trễ:**
     * `late_delivery_seller`: Người bán bàn giao hàng cho bưu cục muộn hơn hạn quy định (SLA breached by seller).
     * `late_delivery_logistics`: Người bán giao hàng đúng hạn, nhưng đơn vị vận chuyển giao trễ cho khách.
  3. **Xây dựng `root_cause_analysis`:**
     * `ranked_causes`: Xếp hạng nguyên nhân gốc từ 1 đến 5 (ví dụ: `LOGISTICS_DELAY`, `SELLER_DISPATCH_TIMEOUT`, `CARRIER_LOST_PARCEL`).
     * `responsible_parties`: Xác định chính xác bên chịu trách nhiệm (`seller`, `logistics_provider`, `platform`, `customer`).
* **Định nghĩa hoàn thành (DoD):**
  * Phân biệt chính xác giữa lỗi vận chuyển và lỗi nhà bán hàng dựa trên mốc thời gian thực tế từ MCP.
  * `root_cause_analysis` thỏa mãn chặt chẽ cấu trúc schema (có `cause_code`, `rank`, `party_type`, `party_id`).

---

### 👤 THÀNH VIÊN 4: Payment & Financial Resolution Specialist Agent
**Chuyên môn:** Đối soát Giao dịch, Cổng thanh toán & Tính toán Đền bù Tài chính

* **File/Thư mục sở hữu chính:**
  * `src/student_agent/agents/payment_agent.py`
  * Actor name trong Trace: `payment-agent`
  * Khối Output: `affected_entities.payment_references`, `financial_resolution`
* **MCP Tools thực tế phụ trách (3 tools):**
  1. `get_order_payments`: Lấy chi tiết các khoản thanh toán của đơn (loại thẻ, số lần trả góp, số tiền từng phần).
  2. `get_payment_timeline`: Lấy dòng thời gian xử lý giao dịch thanh toán, thời điểm duyệt/từ chối.
  3. `get_refund_timeline`: Lấy tiến trình xử lý hoàn tiền, trạng thái gateway đã hoàn hay chưa.
* **Trách nhiệm chính:**
  1. **Điều tra Thanh toán:** Gọi 3 tool MCP trên để đối chiếu dòng tiền giữa khách hàng và sàn.
  2. **Xác định các sự cố thanh toán:**
     * `valid_split_payment`: Khách thanh toán nhiều phương thức/nhiều lần hợp lệ (không phải lỗi gian lận).
     * `payment_mismatch`: Sai lệch số tiền giữa cổng thanh toán và giá trị đơn hàng.
     * `duplicate_charge`: Trừ tiền trùng lặp cho cùng một mã đơn.
     * `refund_pending` / `refund_failed`: Tiền hoàn đang treo hoặc bị lỗi cổng thanh toán.
  3. **Tính toán `financial_resolution`:**
     * Luôn đặt `"currency": "BRL"`.
     * Tính toán chính xác `recommended_refund_brl` (xử lý làm tròn số thập phân, không gây sai số floating-point).
     * Chi tiết các dòng hoàn tiền `refund_lines`: Mỗi dòng phải có `reason_code`, `amount_brl`, và `entity_id`.
* **Định nghĩa hoàn thành (DoD):**
  * `sum(line.amount_brl for line in refund_lines) == recommended_refund_brl`.
  * Không đề xuất hoàn tiền nếu giao dịch hợp lệ và đơn hàng hoàn tất bình thường.

---

### 👤 THÀNH VIÊN 5: Policy, Data Conflict & Verifier / QA Agent
**Chuyên môn:** Đối soát Chính sách, Xử lý Xung đột dữ liệu, Kiểm soát Hard Gates & Testing

* **File/Thư mục sở hữu chính:**
  * `src/student_agent/agents/verifier_agent.py`
  * `src/student_agent/agents/policy_agent.py`
  * Actor name trong Trace: `policy-agent`, `verifier`
  * Thư mục `tests/` (Unit test, integration test, mock data)
  * Khối Output: `data_conflicts`, `assessment.confidence`, `resolution_actions`, kiểm định Schema toàn diện.
* **MCP Tools thực tế phụ trách (1 tool):**
  1. `get_policy`: Lấy chi tiết điều khoản sàn theo `policy_version` (quy định hoàn tiền, SLA giao hàng, chính sách đền bù).
* **Trách nhiệm chính:**
  1. **Chính sách sàn (`get_policy`):** Truy vấn điều khoản sàn để làm căn cứ pháp lý cho hành động giải quyết và mức bồi hoàn.
  2. **Phát hiện xung đột dữ liệu (`data_conflicts`):** Phát hiện mâu thuẫn giữa lời khai của khách hàng và dữ liệu thật từ MCP (điền `field`, `sources`, `selected_source`, `resolution_code`).
  3. **Đề xuất hành động (`resolution_actions`):** Danh sách hành động cụ thể (tối đa 8 actions, unique strings, độ dài 1-80 ký tự).
  4. **Người gác cổng Hard Gates (Gatekeeper):**
     * Kiểm tra khớp `case_id`.
     * Kiểm tra tính nhất quán (`consistency`): Nếu `no_action` thì refund = 0; nếu lỗi do seller thì `responsible_parties` phải có seller.
     * Lọc bỏ `evidence_ref` trùng lặp hoặc không thuộc case hiện tại.
     * Hiệu chuẩn độ tự tin (`confidence` 0.0 - 1.0) theo mức độ tin cậy của bằng chứng.
     * Emit event: `policy_decided`, `verification_completed`.
  5. **Bộ Test Suite (`tests/`):** Viết unit tests giả lập MCP gateway để test các trường hợp biên (edge cases).
* **Định nghĩa hoàn thành (DoD):**
  * Chạy `day09 validate` đạt 100/100 cases hợp lệ.
  * Bộ test trong thư mục `tests/` chạy `pytest` pass 100%.

---

## 📊 BẢNG TỔNG KẾT PHÂN BỔ 10 MCP TOOLS

| STT | Tên Tool thật từ MCP Server | Specialist phụ trách | Actor name | Mục đích chính |
| :---: | :--- | :---: | :---: | :--- |
| 1 | `get_order` | **TV2 (Order)** | `order-agent` | Kiểm tra trạng thái đơn, thời điểm tạo/hủy |
| 2 | `get_order_items` | **TV2 (Order)** | `order-agent` | Danh sách item, giá trị từng món, mã seller |
| 3 | `get_product_context` | **TV2 (Order)** | `order-agent` | Thông tin danh mục, sản phẩm |
| 4 | `get_customer_history` | **TV2 (Order)** | `order-agent` | Lịch sử mua hàng và profile khách |
| 5 | `get_shipment_summary` | **TV3 (Shipment)** | `shipment-agent` | Tracking bưu kiện, đối chiếu hạn giao vs thực tế |
| 6 | `get_sellers` | **TV3 (Shipment)** | `shipment-agent` | Thông tin seller, cam kết SLA đóng gói |
| 7 | `get_order_payments` | **TV4 (Payment)** | `payment-agent` | Chi tiết các khoản thanh toán, split payment |
| 8 | `get_payment_timeline` | **TV4 (Payment)** | `payment-agent` | Lịch sử xử lý thanh toán, duplicate charge |
| 9 | `get_refund_timeline` | **TV4 (Payment)** | `payment-agent` | Tiến độ hoàn tiền, refund pending/failed |
| 10 | `get_policy` | **TV5 (Policy)** | `policy-agent` | Quy định sàn, chính sách bồi hoàn |

---

## 🔄 4. GIAO THỨC CHUYỂN GIAO DỮ LIỆU NỘI BỘ (A2A State)

Để các Agent làm việc độc lập mà không bị giẫm chân lên nhau, các thành viên thống nhất dùng một kiểu dữ liệu State chung:

```python
# Gợi ý cấu trúc State chuyển giao giữa các Agent
class CaseState:
    case_id: str
    opened_at: str
    customer_request: dict
    policy_version: str
    
    # Bằng chứng tích lũy từ MCP
    evidence_refs: list[str]
    
    # Kết quả từng chuyên gia
    order_data: dict | None
    shipment_data: dict | None
    payment_data: dict | None
    policy_data: dict | None
    
    # Kết luận sơ bộ
    claims_assessed: list[dict]
    conflicts_detected: list[dict]
    primary_issue_candidate: str | None
    financial_resolution_candidate: dict | None
```

---

## 📅 5. LỘ TRÌNH VÀ TIẾN ĐỘ THỰC HIỆN

| Giai đoạn | Thời gian dự kiến | Mục tiêu chính | Người phụ trách chính |
| :--- | :---: | :--- | :--- |
| **P1: Setup & Khởi động** | Ngày 1 | Cài đặt môi trường, kết nối MCP server, chạy thử `day09 mcp-tools`, viết khung sườn các class Agent. | **Tất cả (TV1 lead)** |
| **P2: Cài đặt Specialist** | Ngày 2 - 3 | Hoàn thành logic riêng của Order, Shipment, Payment, Policy. Thu thập evidence chuẩn. | **TV2, TV3, TV4, TV5** |
| **P3: Ghép nối & Verifier** | Ngày 4 | Kết nối luồng qua Coordinator, chạy thử 10 case đầu tiên, hoàn thiện bộ lọc Verifier và xử lý Consistency. | **TV1 & TV5** |
| **P4: Batch 100 & Tối ưu** | Ngày 5 | Chạy full 100 cases, rà soát F1 evidence, tinh chỉnh Calibration confidence, cập nhật `ARCHITECTURE.md`. | **Tất cả** |
| **P5: Package & Nộp bài** | Ngày 6 | Đóng gói `day09 package`, kiểm tra tính hợp lệ của `submission.zip` và nộp bài. | **TV1 & TV5** |

---

## 🛠️ 6. NGUYÊN TẮC LÀM VIỆC NHÓM (GIT & CODE REVIEW)

1. **Branching Strategy:**
   * `main`: Nhánh ổn định, chỉ merge khi code đã chạy qua `pytest` và `day09 validate`.
   * `feat/tv1-coordinator-trace`
   * `feat/tv2-order-specialist`
   * `feat/tv3-shipment-specialist`
   * `feat/tv4-payment-specialist`
   * `feat/tv5-verifier-policy`
2. **Quy định Commit & PR:**
   * Tuyệt đối không commit file `.env`, file log rác hoặc file tạm.
   * Mỗi Pull Request phải có ít nhất 1 thành viên khác review trước khi merge vào `main`.
3. **Audit Evidence:**
   * Mọi cuộc gọi `gateway.call()` đều phải có ý nghĩa phục vụ cho kết luận; không gọi tool vô tội vạ để đảm bảo độ tin cậy của audit log.
