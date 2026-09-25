# L3A Architecture Record

Hồ sơ kiến trúc hệ thống Multi-Agent phục vụ giải quyết khiếu nại thương mại điện tử (Olist E-commerce Dataset) cho cuộc thi Day09 L3A Multi-Agent MCP + A2A.

## 1. System overview

Luồng xử lý từ input đầu vào đến output hoàn tất và nhật ký trace có thể quan sát:

```text
Input (inputs/<case_id>.json)
  │
  ▼
Coordinator Agent ─────────────► Trace (case_received, task_assigned)
  │
  ├──► Specialist Agents
  │      ├── Order & Items Agent ────► MCP Gateway (get_order, get_order_items)
  │      ├── Shipment Agent ─────────► MCP Gateway (get_shipment_summary, get_sellers)
  │      └── Payment Agent ──────────► MCP Gateway (get_order_payments, get_refund_timeline)
  │      └── Trace: tool_result_consumed, handoff
  │
  ▼
Policy Agent ────────────────────────► MCP Gateway (get_policy)
  │                                    Trace (policy_decided, handoff)
  ▼
Verifier Agent ──────────────────────► Trace (verification_completed)
  │                                    Invariants Guard & Calibration
  ▼
Output (outputs/<case_id>.json) ─────► Trace (case_finalized)
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff | Quyền truy cập Tools |
| --- | --- | --- | --- | --- |
| **Coordinator** | `inputs/<case_id>.json` | Tiếp nhận case, phân tích claim khách hàng, điều phối công việc cho các specialist | Giao việc cho các Specialist Agents | `list_tools` |
| **Order/item** | `case_id`, `claimed_order_id` | Xác minh trạng thái đơn hàng, thông tin sản phẩm, giá bán, hạn xuất kho | Handoff hồ sơ đơn hàng & sản phẩm | `get_order`, `get_order_items` |
| **Shipment** | `case_id`, `claimed_order_id` | Đối soát timeline giao hàng, hạn chót người bán bàn giao kho, thông tin seller | Handoff hồ sơ vận chuyển & seller | `get_shipment_summary`, `get_sellers` |
| **Payment** | `case_id`, `claimed_order_id` | Phân tích dòng tiền, phát hiện duplicate charge, kiểm tra hoàn tiền pending/failed | Handoff hồ sơ thanh toán & hoàn tiền | `get_order_payments`, `get_refund_timeline` |
| **Policy** | Hồ sơ chứng cứ tổng hợp, `policy_version` | Thẩm định theo quy tắc chính sách, xác định `primary_issue`, tính tiền bồi hoàn | Phán quyết sơ bộ & cấu trúc tài chính | `get_policy` |
| **Verifier** | Phán quyết sơ bộ, danh sách evidence | Kiểm tra tính bất biến (invariants), phát hiện mâu thuẫn dữ liệu, hiệu chuẩn confidence | Output chuẩn `day09-l3a-output-v2` | Không gọi tool (Internal Logic Only) |

## 3. A2A protocol

- **Message Envelope:** Trao đổi dữ liệu nội bộ qua dictionary có cấu trúc chặt chẽ, gắn chặt với correlation key duy nhất là `case_id`.
- **Handoff Condition:** Specialist chỉ handoff khi đã truy xuất xong các evidence liên quan hoặc nhận phản hồi an toàn từ MCP Gateway.
- **Tránh vòng lặp (Acyclic Pipeline):** Quy trình điều phối là một Directed Acyclic Graph (DAG) đi thẳng từ Coordinator qua Specialists, sang Policy, tới Verifier và Finalize; không có chu trình quay lui lặp vô tận.
- **Trace Observability:** Chỉ phát sinh các sự kiện trạng thái quan sát được (`task_assigned`, `tool_result_consumed`, `handoff`, `policy_decided`, `verification_completed`), tuyệt đối không đưa prompt bí mật hoặc chain-of-thought vào trace.

## 4. Evidence lifecycle

- **Thẩm quyền (Authority):** Mọi bằng chứng phải được truy vấn qua MCP Gateway gắn liền với `case_id` tương ứng. Server audit log sẽ đối soát `(team, run, case_id)`.
- **Validation:** Toàn bộ phản hồi tool phải tuân thủ schema `day09-mcp-evidence-v1`.
- **Audit & Provenance:** Sau mỗi lần tiêu thụ dữ liệu có thẩm quyền, Agent phát sinh sự kiện `tool_result_consumed` ghi nhận `evidence_ref`.
- **Zero Cross-Scope:** Bằng chứng của case nào chỉ được sử dụng cho case đó; tuyệt đối không tái sử dụng `evidence_ref` giữa các case khác nhau.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event / code |
| --- | --- | --- | --- |
| **MCP timeout / ConnectError** | Có (3-5 lần, exponential backoff) | Tự động tái lập session stream mới | Bắt lỗi cấp transport, reconnect êm xuôi |
| **Not found (ví dụ: chưa có refund)** | Không (0 lần, idempotent) | Coi như danh sách rỗng (`allow_error=True`) | Tiếp tục quy trình bình thường |
| **Source conflict (Khách claim sai)** | Không | Ưu tiên dữ liệu MCP authoritative | Ghi nhận vào `data_conflicts` |
| **Invalid specialist result** | Không | Verifier tự động chuẩn hóa và khử trùng lặp | `verification_completed` |

## 6. Verification invariants

Trước khi hoàn tất ghi file `outputs/<case_id>.json`, Verifier kiểm tra các luật bất biến:
1. **Schema Invariant:** Khớp 100% với JSON Schema `day09-l3a-output-v2`.
2. **Status - Financial Consistency:** Nếu `case_status == "no_action"` thì `recommended_refund_brl == 0.0` và `refund_lines == []`.
3. **Seller Party Invariant:** Nếu bên chịu trách nhiệm là `seller` thì `party_id` bắt buộc phải là chuỗi định danh người bán thật, không được `null`.
4. **Deduplication:** `resolution_actions`, `evidence_refs`, và các tập ID thực thể phải có thuộc tính `uniqueItems: true`.
5. **Confidence Bounds:** Giá trị `confidence` được kẹp chặt chẽ trong khoảng $[0.50, 0.98]$ dựa trên chất lượng và độ đầy đủ của evidence.

## 7. Reproducibility

- **Môi trường:** Python 3.11.0, thư viện quản lý qua `.venv`.
- **Dependencies Pinning:** `httpx2>=2,<3`, `mcp>=2,<3`, `jsonschema>=4.25,<5`, `python-dotenv>=1.1,<2`.
- **Lệnh thực thi chuẩn:**
  - Chạy toàn bộ case: `python -m student_agent.cli run`
  - Kiểm tra hợp đồng: `python -m student_agent.cli validate`
  - Đóng gói submission: `python -m student_agent.cli package --output dist/submission.zip`
- **Giới hạn tài nguyên:** Tối đa 1-2 concurrent connections, retry giới hạn 5 lần, dung lượng file ZIP $\le 12$ MB. Không chứa API key trong bất kỳ file output hay trace nào.
