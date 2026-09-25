# Phân công Nhiệm vụ (5 Người - 5 Agent)

Theo bản thiết kế `ARCHITECTURE.md` gốc thì hệ thống có tổng cộng **6 Actors** (Coordinator, Order/Item, Payment, Shipment, Policy, Verifier). 

Để chia cho nhóm **5 người, mỗi người phụ trách chính xác 1 Agent**, tôi đã gộp 2 Agent có nghiệp vụ liên quan chặt chẽ với nhau (Shipment & Policy) lại thành 1 Agent duy nhất để số lượng Agent vừa đúng bằng 5.

Dưới đây là cách chia cụ thể:

## 1. Tài: Coordinator Agent (Người điều phối)
*   **Trách nhiệm chính:** Xây dựng não bộ điều phối của toàn hệ thống. Tiếp nhận `inputs/<case_id>.json`, phân tích ngữ cảnh và quyết định luồng đi (truyền cho Agent nào trước, Agent nào sau).
*   **A2A Protocol:** Thiết kế cấu trúc Message Envelope để các Agent giao tiếp. Xử lý timeout và chống vòng lặp (infinite loop).
*   **MCP Tools:** Không gọi trực tiếp tool nào, chỉ điều phối.

## 2. Dũng: Order & Item Agent (Chuyên gia Đơn hàng)
*   **Trách nhiệm chính:** Phụ trách Agent xử lý mọi thông tin về đơn hàng, giỏ hàng, người mua.
*   **MCP Tools được cấp quyền:** 
    *   `get_order`
    *   `get_order_items`
    *   `get_product_context`
*   **Đầu ra:** Báo cáo xem đơn hàng có thật không, sản phẩm mua là gì, tình trạng gói hàng cơ bản.

## 3. Long: Payment Agent (Chuyên gia Thanh toán)
*   **Trách nhiệm chính:** Phụ trách Agent chuyên điều tra về dòng tiền, thanh toán, và tiến trình hoàn tiền (refund).
*   **MCP Tools được cấp quyền:** 
    *   `get_order_payments`
    *   `get_payment_timeline`
    *   `get_refund_timeline`
*   **Đầu ra:** Phát hiện các lỗi như `duplicate_charge` (trừ tiền 2 lần), `payment_mismatch` (thanh toán lệch), hoặc hoàn tiền chậm.

## 4. Quân: Shipment & Policy Agent (Chuyên gia Vận chuyển & Chính sách)
*   *(Đã gộp Shipment Agent và Policy Agent lại cho 1 người vì thường vi phạm giao hàng sẽ liên kết trực tiếp với chính sách)*
*   **Trách nhiệm chính:** Điều tra xem hàng giao có bị trễ không, lỗi do đơn vị vận chuyển hay do người bán, và đối chiếu với quy định.
*   **MCP Tools được cấp quyền:** 
    *   `get_shipment_summary`
    *   `get_policy`
    *   `get_sellers`
*   **Đầu ra:** Đưa ra Claim Assessment xem lỗi thuộc về ai (ví dụ: `late_delivery_seller`).

## 5. Anh Thắng: Verifier Agent (Thẩm định viên cuối cùng)
*   **Trách nhiệm chính:** Xây dựng Agent đứng ở cuối luồng. Agent này không đi thu thập dữ liệu mà chỉ **nhận kết quả từ 3 Agent kia** để đưa ra phán quyết cuối cùng.
*   **Xử lý Output:** Tổng hợp thông tin, điền vào form kết quả, đảm bảo Output khớp 100% với JSON Schema (`l3a-output-v2.schema.json`).
*   **Bằng chứng & Trace:** Quản lý việc parse `evidence_ref` từ MCP, đảm bảo evidence không bị tái sử dụng giữa các case và emit log/trace chuẩn xác phục vụ lệnh `day09 validate`.
