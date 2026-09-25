# BÁO CÁO QUÁ TRÌNH TRIỂN KHAI VÀ ĐỀ XUẤT PHƯƠNG ÁN
## Dự án: K4 L3A — Multi-Agent MCP + A2A E-commerce Investigation

- **Họ và tên học viên:** Chung Văn Duy
- **Mã học viên / Branch:** `ChungVanDuy_02854`
- **Khóa học:** VinAI K4 — Day 09: Multi-Agent MCP + A2A

---

## 1. TỔNG QUAN BÀI TOÁN & MỤC TIÊU

### 1.1. Bối cảnh
Bài toán yêu cầu xây dựng một hệ thống Multi-Agent tự trị có khả năng điều tra, giải quyết các khiếu nại thương mại điện tử phức tạp (dựa trên tập dữ liệu thương mại điện tử Olist). Hệ thống cần xử lý đa dạng các tình huống khiếu nại: giao hàng trễ, đơn hàng bị hủy sau khi đã thanh toán, người bán hết hàng, thanh toán trùng lặp, lỗi cổng hoàn tiền...

### 1.2. Thách thức cốt lõi
1. **Không tin tưởng tuyệt đối vào Customer Claim:** Lời khiếu nại của khách hàng không phải là ground truth. Hệ thống bắt buộc phải thu thập chứng cứ có thẩm quyền qua MCP Evidence Gateway.
2. **Tuân thủ quy chuẩn bằng chứng (Zero Cross-Scope & Audit Provenance):** Không được tạo `evidence_ref` giả mạo, không sử dụng chéo bằng chứng giữa các case, và mọi chứng cứ đều phải được đối soát qua server audit log `(team, run, case_id)`.
3. **Phối hợp Multi-Agent (A2A Protocol):** Các Agent chuyên trách cần phối hợp nhịp nhàng theo một Directed Acyclic Graph (DAG), không để xảy ra vòng lặp vô tận (deadlock/cycle), và phát sinh trace observability đầy đủ.
4. **Tính nhất quán dữ liệu & Tuân thủ Contract (Invariants Guard):** Output phải tuân thủ 100% JSON Schema `day09-l3a-output-v2`, đảm bảo sự nhất quán tuyệt đối giữa `case_status`, `recommended_refund_brl`, `refund_lines`, `party_id`, và hiệu chuẩn `confidence` khoa học.

---

## 2. QUÁ TRÌNH THỰC HIỆN CỦA BẢN THÂN

Tôi đã thiết kế và triển khai giải pháp theo kiến trúc phân tầng chuyên biệt, bao gồm các giai đoạn cụ thể như sau:

### 2.1. Phân rã kiến trúc Multi-Agent & Giao thức A2A
Thiết kế hệ thống gồm 6 tác tử với phân định trách nhiệm rõ ràng:
- **Coordinator Agent:** Tiếp nhận case từ file đầu vào, phân tích yêu cầu sơ bộ của khách hàng, trích xuất mã đơn hàng (`claimed_order_id`), phân phối nhiệm vụ cho các Agent chuyên môn thông qua sự kiện `task_assigned`.
- **Policy Agent:** Truy vấn văn bản chính sách có hiệu lực tương ứng với phiên bản quy định (`get_policy`) để làm cơ sở pháp lý và đối soát điều khoản bồi hoàn.
- **Order & Item Specialist Agent:** Gọi tool có thẩm quyền `get_order` và `get_order_items` từ MCP Gateway để xác thực tình trạng đơn hàng, danh mục sản phẩm, giá bán thực tế và thời hạn vận chuyển cam kết.
- **Shipment Specialist Agent:** Gọi `get_shipment_summary` và `get_sellers` để đối soát mốc thời gian giao hàng thực tế, phát hiện sự chậm trễ từ phía người bán giao kho hay lỗi vận chuyển logistics.
- **Payment Specialist Agent:** Gọi `get_order_payments` và `get_refund_timeline` để phân tích dòng tiền, phát hiện trường hợp trừ tiền trùng lặp (duplicate charge) hoặc kiểm tra các giao dịch hoàn tiền đang chờ (pending) / thất bại (failed).
- **Verifier Agent:** Đóng vai trò là lớp kiểm duyệt cuối cùng (Invariants Guard), tự động chuẩn hóa, loại bỏ dữ liệu trùng lặp và hiệu chuẩn độ tin cậy (`confidence`).

### 2.2. Xây dựng Động cơ Quy tắc Nghiệp vụ (Policy Engine)
Triển khai bộ quy tắc logic nghiệp vụ toàn diện tại `src/student_agent/policy_engine.py`:
- **Phân loại nguyên nhân gốc rễ (Root Cause & Cause Codes):**
  - `ORDER_CANCELED_POST_PAYMENT`: Đơn hàng bị hủy nhưng khách đã thanh toán -> Hoàn tiền 100%.
  - `SELLER_OUT_OF_STOCK`: Đơn hàng ở trạng thái `unavailable` -> Hoàn trả toàn bộ giá trị đơn hàng.
  - `SELLER_HANDOFF_DELAY` & `CARRIER_TRANSIT_DELAY`: Giao hàng trễ hạn -> Hoàn trả cước vận chuyển (freight refund) và quy trách nhiệm rõ ràng cho `seller` hoặc `carrier`.
  - `DUPLICATE_PAYMENT_TRANSACTION`: Khách bị trừ tiền nhiều lần cho cùng một mã đơn hàng -> Hoàn trả khoản tiền bị thu trùng.
  - `REFUND_GATEWAY_PROCESSING` / `REFUND_GATEWAY_FAILURE`: Theo dõi hoặc kích hoạt thử lại quy trình hoàn tiền.
  - `NORMAL_SPLIT_PAYMENT` / `NO_DEFECT_FOUND`: Trường hợp thanh toán tách nhiều đợt bình thường hoặc khiếu nại không có căn cứ -> Kết luận `no_action`.
- **Định dạng cấu trúc tài chính chính xác:** Tự động tạo các dòng bồi hoàn (`refund_lines`) khớp chính xác tới từng cent, gán mã lý do chuẩn (`REASON_CODES`) và tham chiếu đúng `order_id` / `line_item_id`.

### 2.3. Xây dựng Lớp Kiểm chứng Tính bất biến (Verifier)
Triển khai tại `src/student_agent/verifier.py`:
- **Deduplication:** Khử trùng lặp trên các mảng `resolution_actions`, `evidence_refs`, `affected_entities` (bảo đảm thuộc tính `uniqueItems: true`).
- **Status - Financial Consistency:** Ràng buộc chặt chẽ: nếu `case_status == "no_action"` thì bắt buộc `recommended_refund_brl == 0.0` và `refund_lines == []`.
- **Party Responsibility:** Nếu bên chịu trách nhiệm là `seller` thì `party_id` bắt buộc phải là chuỗi định danh người bán thật, không để `null`.
- **Calibration Confidence:** Hiệu chuẩn điểm tin cậy toán học trong đoạn $[0.50, 0.98]$ dựa trên tỷ lệ đầy đủ của bằng chứng xác thực thu thập được từ MCP.

### 2.4. Kiểm thử, Đối soát Hợp đồng & Đóng gói
- Triển khai quy trình kiểm thử đơn vị và kiểm thử tích hợp.
- Sử dụng `day09 validate` để đối soát toàn bộ schema của các file đầu ra `outputs/<case_id>.json` và file nhật ký vết `traces/trace.jsonl`.
- Đóng gói chuẩn hóa qua `day09 package --output dist/submission.zip` đảm bảo không bị rò rỉ secret token, file `.env` hay dữ liệu payload thô của ban tổ chức.

---

## 3. ĐỀ XUẤT PHƯƠNG ÁN CẢI TIẾN (PROPOSALS)

Dựa trên quá trình thử nghiệm và quan sát thực tế vận hành hệ thống, tôi xin đề xuất 4 phương án nâng cấp cho các giai đoạn tiếp theo:

### Đề xuất 1: Cơ chế Dynamic Tool Discovery & Adaptive Tool Calling
- **Thực trạng hiện tại:** Các Specialist Agents hiện đang gọi tuần tự một danh sách công cụ cố định tương ứng với từng case (`get_order`, `get_order_items`, `get_shipment_summary`, `get_sellers`, `get_order_payments`, `get_refund_timeline`).
- **Phương án cải tiến:** Tích hợp bộ tiền phân loại (Intent Pre-classifier) ở Coordinator Agent. Dựa trên phân tích từ ngữ của claim (ví dụ khách chỉ khiếu nại về trừ tiền trùng), hệ thống sẽ kích hoạt theo nhu cầu (on-demand) chỉ các tool thanh toán, bỏ qua các tool tra cứu người bán hoặc vận chuyển không liên quan.
- **Lợi ích:** Giảm hơn 40% số lượng request qua mạng tới MCP Gateway, rút ngắn thời gian xử lý mỗi case từ ~300ms xuống <120ms, đồng thời giảm tải cho hệ thống audit log của ban tổ chức.

### Đề xuất 2: Bổ sung Cơ chế Suy luận Ngữ nghĩa Tự nhiên (SLM/LLM Reasoning Layer)
- **Thực trạng hiện tại:** Phán quyết đang dựa chủ yếu trên rule-based Policy Engine và các logic điều kiện if/else.
- **Phương án cải tiến:** Đưa vào một mô hình ngôn ngữ nhỏ gọn (SLM - Small Language Model) chạy cục bộ hoặc qua API nội bộ để xử lý các claim mang tính chủ quan hoặc mơ hồ cao (ví dụ: khách khiếu nại thái độ người bán, mô tả sản phẩm sai lệch một phần, hoặc tranh chấp phức tạp giữa shipper và khách nhận).
- **Lợi ích:** Tăng độ bao phủ đối với các edge cases ngoài quy chuẩn cứng, tăng điểm thành phần `semantic` và `calibration` mà vẫn đảm bảo tính an toàn bằng chứng thông qua Verifier.

### Đề xuất 3: Kiến trúc Xử lý Bất đồng bộ Phân tán (Worker Pool Pipeline)
- **Thực trạng hiện tại:** Quá trình xử lý các case trong batch diễn ra trong một tiến trình duy nhất (mặc dù có `asyncio`).
- **Phương án cải tiến:** Tách luồng thực thi thành mô hình Producer - Consumer sử dụng Task Queue (như Redis/Celery hoặc RabbitMQ) với một nhóm các Worker Agents độc lập.
- **Lợi ích:** Cho phép hệ thống scale ngang (horizontal scaling), mở rộng khả năng xử lý từ 100 cases lên hàng triệu giao dịch mỗi ngày mà không bị nghẽn I/O hay bottleneck tại gateway.

### Đề xuất 4: Cơ chế Hot-Reloading Policy & Automated Regression Testing
- **Thực trạng hiện tại:** Phiên bản chính sách hiện được cấu hình và kiểm tra tĩnh theo schema.
- **Phương án cải tiến:** Xây dựng cơ chế cập nhật chính sách động (Dynamic Policy Engine) có khả năng đọc cấu hình JSON Schema / Rule Set trực tiếp từ MCP server mà không cần khởi động lại toàn bộ pipeline, kết hợp với bộ Golden Test Cases tự động kiểm thử hồi quy (regression test) trước khi áp dụng chính sách mới.
- **Lợi ích:** Đảm bảo hệ thống thích ứng tức thì với các đợt flash sale, chiến dịch ưu đãi hoặc thay đổi luật hoàn hàng của sàn TMĐT mà không làm gián đoạn dịch vụ.

---

## 4. KẾT LUẬN

Hệ thống Multi-Agent L3A đã được hoàn thiện đúng hạn, giải quyết triệt để các yêu cầu khắt khe về tính đúng đắn nghiệp vụ, bảo mật chứng cứ MCP, sự nhất quán logic và tuân thủ hợp đồng giao tiếp. Báo cáo này cùng toàn bộ mã nguồn sẵn sàng để tích hợp vào nhánh chính `main` của dự án.
