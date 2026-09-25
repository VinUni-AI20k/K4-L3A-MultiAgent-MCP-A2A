# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Hệ thống dự kiến sử dụng Python async state-machine. Mỗi agent là một hàm có trách nhiệm, input và output riêng. Coordinator quản lý luồng xử lý trong solve_case(case, gateway, trace).

```text
Input case
    |
    v
Coordinator
    |
    v
Order/Item: xác minh thông tin nền cần thiết
    |
    v
Coordinator giao nhiệm vụ liên quan
    |
    +--------------------+
    v                    v
Payment               Shipment
    |                    |
    +--------------------+
    |
    v
Gom facts và evidence theo case_id
    |
    v
Policy: áp dụng chính sách, tạo output dự thảo
    |
    v
Verifier
    |
    +-- Đạt --> Coordinator --> Output cuối
    |
    +-- Cần sửa/bổ sung --> Coordinator
                              |
                              v
                       Agent phụ trách
                              |
                              v
                       Policy → Verifier
                       (tối đa một vòng bổ sung)
```

### Truy cập bằng chứng

- Order/Item, Payment, Shipment và Policy gọi MCP thông qua EvidenceGateway theo quyền được quy định tại mục 2.
- Các nhiệm vụ chỉ chạy đồng thời khi không phụ thuộc dữ liệu nhau.
- Chỉ giao những nhiệm vụ liên quan đến case.
- Evidence được lưu trong state riêng của từng lần solve_case.
- Bộ thu thập evidence là thành phần dùng chung, không bắt buộc là một agent LLM riêng.

### Output và trace

- solve_case trả về dict tuân thủ l3a-output-v2.schema.json.
- TraceWriter ghi các sự kiện thực tế trong suốt quá trình xử lý.
- Message A2A và state nội bộ không được đưa nguyên vào public output.
- Chỉ finalize sau khi Verifier chấp nhận output cuối cùng.

### Trạng thái triển khai

Đã triển khai:

- Message A2A trong `a2a.py`.
- State riêng theo case và kiểm tra quyền/phạm vi tool trong `state.py`.
- EvidenceCollector với giới hạn concurrency, deadline và retry cho TimeoutError trực tiếp trong `evidence.py`.
- Ghi nhận sử dụng evidence và message handoff trong `observability.py`.
- Order/Item Agent xác minh định danh và trạng thái đơn trong `order_agent.py`.
- Luồng Coordinator giao việc và nhận kết quả Order/Item trong `coordinator.py`.
- `workflow.solve_case()` nối Coordinator với Policy và Verifier, tạo public output
    từ facts đã có evidence và chỉ trả sau khi kiểm tra nội bộ đạt.

Đã kiểm tra bằng Gateway giả lập và TraceWriter thật:

- Coordinator ghi `task_assigned`.
- Order/Item ghi `tool_result_consumed`.
- Kết quả được chuyển về Coordinator bằng `handoff`.
- Ba sự kiện đúng thứ tự và vượt qua public trace schema.

Giới hạn hiện tại:

- Order/Item Agent mới xác minh order status; truy vấn item/seller là phần mở rộng tiếp theo.
- Verifier hiện kiểm tra field set, case/entity scope, evidence linkage và tổng refund;
    CLI tiếp tục là lớp validate JSON Schema cuối cùng.
- Chưa tự động thực hiện vòng rework sau `NEEDS_REWORK`; workflow dừng với lỗi thay vì
    tạo fallback evidence hoặc kết luận không có căn cứ.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case đầu vào | Xác định phạm vi case, chia nhiệm vụ, điều phối và giới hạn vòng xử lý | Nhiệm vụ cho specialist kèm case_id và phạm vi entity |
| Order/item | Nhiệm vụ kiểm tra đơn và sản phẩm | Xác minh trạng thái đơn, item và seller liên quan bằng MCP | Facts đã xác minh và evidence_refs cho Policy |
| Payment | Nhiệm vụ kiểm tra thanh toán | Xác minh giao dịch, thanh toán nhiều phần, thu trùng và hoàn tiền | Facts tài chính và evidence_refs cho Policy |
| Shipment | Nhiệm vụ kiểm tra vận chuyển | Xác minh trạng thái và các mốc giao hàng | Facts vận chuyển và evidence_refs cho Policy |
| Policy | Kết quả specialist và bằng chứng chính sách | Áp dụng chính sách, đề xuất kết luận, trách nhiệm và phương án xử lý | Output dự thảo cho Verifier |
| Verifier | Output dự thảo và bằng chứng đã thu thập | Kiểm tra schema, phạm vi entity, liên kết bằng chứng và tính nhất quán nghiệp vụ | Kết quả kiểm tra cho Coordinator: đạt hoặc yêu cầu bổ sung cụ thể |

### Quyền gọi tool

Danh sách tool đã được xác nhận bằng `day09 mcp-tools`. Phân quyền dưới đây là thiết kế cần thực thi trong workflow.

| Actor | Tool được phép gọi |
| --- | --- |
| Coordinator | Discovery qua gateway.list_tools(); không gọi tool dữ liệu |
| Order/item | get_order, get_order_items, get_sellers, get_product_context |
| Payment | get_order_payments, get_payment_timeline, get_refund_timeline |
| Shipment | get_shipment_summary |
| Policy | get_policy |
| Verifier | Không gọi MCP trực tiếp |

### Quy tắc thực thi

- Kiểm tra allowlist của actor trước mỗi lần gọi tool.
- Được phép gọi không có nghĩa phải gọi tất cả tool; chỉ truy vấn khi nhiệm vụ cần dữ liệu đó.
- Mọi lời gọi dữ liệu phải truyền đúng case_id.
- Arguments phải theo định nghĩa tool thực tế, không suy đoán từ tên.
- Verifier yêu cầu Coordinator giao lại nhiệm vụ khi cần thêm evidence.
- get_customer_history chưa cấp cho agent nào vì thiết kế hiện tại chưa xác định nhu cầu sử dụng lịch sử khách hàng.
- Nếu phát sinh nhu cầu, cập nhật quyền và mục đích sử dụng rõ ràng trước khi triển khai lời gọi đó.

### Arguments đã xác nhận qua MCP discovery

| Tool | Arguments bắt buộc |
| --- | --- |
| get_order | case_id, order_id |
| get_order_items | case_id, order_id |
| get_order_payments | case_id, order_id |
| get_payment_timeline | case_id, order_id |
| get_refund_timeline | case_id, order_id |
| get_shipment_summary | case_id, order_id |
| get_sellers | case_id, order_id |
| get_product_context | case_id, order_id |
| get_policy | case_id, policy_version |
| get_customer_history | case_id, customer_unique_id |

Tất cả arguments trên có kiểu string.

- case_id lấy từ case đang xử lý.
- order_id phải thuộc phạm vi case; không tự tạo hoặc lấy từ case khác.
- policy_version phải lấy từ nguồn cấu hình hoặc input được quy định, không tự đoán và không mặc định dùng "latest".
- customer_unique_id phải có căn cứ và thuộc phạm vi case. get_customer_history hiện chưa được cấp quyền trong thiết kế.
- Metadata hiện xác nhận input arguments; chưa xác nhận cấu trúc data trả về của từng tool.

## 3. A2A protocol

### Message nội bộ

Mỗi lần giao hoặc chuyển nhiệm vụ sử dụng message gồm:

| Field | Ý nghĩa |
| --- | --- |
| case_id | Case đang xử lý |
| sender | Agent gửi |
| recipient | Agent nhận |
| task | Nhiệm vụ cụ thể |
| entity_scope | Phạm vi order, item hoặc entity cần kiểm tra |
| facts | Dữ kiện đã xác minh; để trống khi chưa có |
| evidence_refs | Mã bằng chứng thật hỗ trợ facts |
| status | pending, completed hoặc needs_more_evidence |

Đây là message nội bộ, không thêm các field này vào public output hoặc MCP evidence envelope.

### Luồng handoff

1. Coordinator nhận case, xác định yêu cầu và phạm vi entity.
2. Coordinator giao Order/item xác minh thông tin nền cần thiết.
3. Khi đã có đủ định danh, Coordinator giao Payment và Shipment các nhiệm vụ liên quan. Các nhiệm vụ độc lập có thể chạy đồng thời.
4. Kết quả specialist được gom theo case_id và chuyển cho Policy. Mỗi kết quả phải nêu facts, evidence_refs và dữ liệu còn thiếu.
5. Policy sử dụng facts và bằng chứng chính sách để tạo output dự thảo.
6. Policy chuyển bản dự thảo cho Verifier.
7. Nếu kiểm tra đạt, Coordinator trả output đã được xác minh.
8. Nếu cần bổ sung, Verifier nêu rõ phần thiếu; Coordinator giao lại cho specialist phụ trách, sau đó chạy lại Policy và Verifier.

### Điều kiện và giới hạn

- Mọi message phải giữ nguyên case_id của case đang xử lý.
- Agent chỉ xử lý entity trong phạm vi được giao.
- Không chuyển lời khách hàng khai thành facts khi chưa xác minh.
- Specialist thiếu bằng chứng trả status = needs_more_evidence; không tự tạo dữ kiện hoặc evidence_ref.
- Coordinator chờ các nhiệm vụ cần thiết hoàn thành hoặc hết hạn trước khi chuyển kết quả tổng hợp cho Policy.
- Cho phép tối đa một vòng bổ sung sau lần kiểm tra đầu tiên.
- Deadline dự kiến cho toàn bộ solve_case là 180 giây; mỗi lần gọi MCP tối đa 30 giây và không vượt thời gian còn lại.
- Khi hết giới hạn, chỉ trả kết luận về việc thiếu bằng chứng nếu output đó vượt qua kiểm tra schema và nghiệp vụ; nếu không, báo lỗi xử lý case thay vì tạo kết quả giả.

### Trace cho handoff

- task_assigned: Coordinator giao nhiệm vụ.
- handoff: agent chuyển kết quả hoặc yêu cầu cho agent khác.
- Ghi actor, target và case_id đúng với hành động thực tế.
- Chỉ ghi sự kiện quan sát được và decision code; không ghi suy luận riêng.

### Trách nhiệm ghi trace

- CLI ghi `case_received` trước khi gọi `solve_case()`.
- Coordinator ghi `task_assigned` khi giao nhiệm vụ.
- Agent gửi ghi `handoff` khi chuyển kết quả hoặc yêu cầu.
- Agent sử dụng bằng chứng ghi `tool_result_consumed`.
- Policy ghi `policy_decided` khi hoàn thành quyết định dự thảo.
- Verifier ghi `verification_completed` khi hoàn thành kiểm tra.
- CLI ghi `case_finalized` sau khi `solve_case()` trả kết quả, output vượt qua kiểm tra của CLI và được ghi ra file.
- Workflow không ghi trùng `case_received` hoặc `case_finalized`.
- `solve_case()` chỉ trả output cuối cùng sau khi Verifier chấp nhận.
- Khi không thể tạo output hợp lệ và có căn cứ, workflow báo lỗi; không tạo sự kiện `case_finalized` giả.

CLI đã ghi hai sự kiện đầu/cuối; phần phân công trong workflow là thiết kế dự kiến, cần được triển khai.

### Ánh xạ input đã quan sát

Với cấu trúc của L3A_CASE_001:

- case_id lấy từ case["case_id"].
- Đơn cần bắt đầu xác minh lấy từ case["customer_request"]["claimed_order_id"].
- Claims lấy từ case["customer_request"]["claims"].
- policy_version lấy từ case["policy_version"].
- opened_at là thời điểm mở case, không phải thời điểm hiện tại.

Coordinator kiểm tra sự tồn tại và kiểu dữ liệu của các trường trước khi giao nhiệm vụ. Cấu trúc này mới được quan sát trên một case, cần kiểm tra các input còn lại trước khi coi là quy tắc chung.

claimed_order_id là định danh khách hàng cung cấp để truy vấn; Order/item phải xác minh bằng MCP trước khi dùng dữ liệu đơn làm căn cứ kết luận.

topic trong claims không tự động trở thành primary_issue. Yêu cầu requested_full_refund không tự động dẫn đến hoàn toàn bộ tiền. Kết luận phải dựa trên evidence thanh toán, trạng thái đơn và policy. Giữ nguyên claim_id khi tạo claim_assessments.

## 4. Evidence lifecycle

### 1. Thu thập

- Specialist gọi MCP thông qua EvidenceGateway, luôn truyền case_id.
- Chỉ gọi tool đã được discovery và được phép dùng theo vai trò.
- EvidenceGateway kiểm tra envelope theo mcp-evidence-response-v1.schema.json trước khi trả kết quả.
- Specialist kiểm tra thêm domain, cấu trúc data cần dùng và phạm vi entity. Envelope hợp lệ chưa bảo đảm đúng nghiệp vụ.

### 2. Lưu theo case

- Mỗi lần chạy solve_case có kho evidence nội bộ riêng.
- Lưu nguyên envelope nhận từ MCP.
- Lưu riêng ngữ cảnh gọi: case_id, actor, tool_name và arguments.
- Không thêm ngữ cảnh nội bộ vào envelope.
- Không chỉnh sửa hoặc tự tạo evidence_ref và result_hash.
- Không tái sử dụng evidence giữa các case.

### 3. Liên kết với kết luận

- Mỗi fact dùng để kết luận phải liên kết với evidence hỗ trợ nó.
- Policy nhận facts cùng evidence_refs từ các specialist.
- Chỉ đưa vào output các evidence_refs thực sự hỗ trợ kết luận.
- Nếu sử dụng claim_assessments, liên kết evidence với từng claim.
- Thiếu bằng chứng phải được ghi nhận rõ; không suy đoán thành fact.

### 4. Ghi trace khi sử dụng

- Emit tool_result_consumed khi agent thực sự dùng kết quả MCP để xác minh fact hoặc đưa ra quyết định.
- Ghi đúng case_id, actor, tool_name và evidence_refs đã sử dụng.
- Việc nhận được response chưa tự động có nghĩa đã sử dụng evidence.
- Mỗi event chứa tối đa 20 evidence_refs theo trace schema.

### 5. Kiểm tra trước khi trả output

- Verifier kiểm tra mọi evidence_ref được trích dẫn đều có trong kho evidence của case hiện tại.
- Kiểm tra ngữ cảnh gọi và entity scope phù hợp với case.
- Kiểm tra nội dung bằng chứng thực sự hỗ trợ kết luận được gắn.
- Kiểm tra danh sách evidence_refs trong output không trùng lặp và không vượt quá giới hạn 30 phần tử.
- Kiểm tra cục bộ không thay thế việc đối chiếu provenance với MCP audit của hệ thống chấm.

Đây là thiết kế dự kiến; EvidenceGateway đã có kiểm tra schema envelope, các bước quản lý và kiểm tra nghiệp vụ cần triển khai thêm.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout hoặc lỗi tạm thời đã xác định | Tối đa 2 lần gọi lại, trong deadline còn lại | Ghi nhận thiếu bằng chứng nếu vẫn thất bại | task_assigned / MCP_TEMPORARY_RETRY khi giao lại nhiệm vụ |
| Không tìm thấy dữ liệu | Không lặp lại cùng truy vấn; chỉ truy vấn khác khi có căn cứ | Báo needs_more_evidence cho Coordinator | handoff / EVIDENCE_NOT_FOUND |
| Sai quyền hoặc arguments không hợp lệ | Không retry nguyên trạng | Báo lỗi cấu hình hoặc yêu cầu sửa tham số | handoff / MCP_REQUEST_REJECTED |
| MCP envelope sai schema | Không sử dụng response làm bằng chứng | Báo lỗi contract cho Coordinator | handoff / INVALID_EVIDENCE_ENVELOPE |
| Nguồn dữ liệu mâu thuẫn | Không gọi lại cùng truy vấn để mong có kết quả khác | Áp dụng quy tắc ưu tiên nguồn nếu có; nếu chưa giải quyết được thì giữ trạng thái chưa chắc chắn | handoff / SOURCE_CONFLICT |
| Kết quả specialist không hợp lệ | Tối đa một vòng sửa hoặc bổ sung do Coordinator điều phối | Xử lý theo giới hạn toàn workflow nếu vẫn không đạt | task_assigned / SPECIALIST_REWORK khi giao lại |

### Giới hạn retry

- Một yêu cầu MCP có tối đa 3 lần gọi: lần đầu và 2 lần retry.
- Chờ 1 giây trước retry thứ nhất, 2 giây trước retry thứ hai.
- Mỗi lần gọi tối đa 30 giây và không vượt thời gian còn lại.
- Tổng thời gian solve_case không vượt deadline 180 giây đã chọn.
- Không bắt đầu retry nếu deadline đã hết.
- Chỉ tự động retry thao tác đọc có thể lặp an toàn.
- Chỉ retry lỗi được xác định là tạm thời; không retry mọi exception.
- Giữ nguyên case_id và phạm vi truy vấn khi retry cùng yêu cầu.
- Retry MCP không đặt lại bộ đếm vòng bổ sung của workflow.
- Việc sửa kết quả specialist dùng chung giới hạn một vòng bổ sung đã quy định trong A2A protocol.

### Khi hết giới hạn

- Không tạo evidence_ref hoặc dữ kiện thay thế.
- Nếu bằng chứng chưa đủ, cân nhắc insufficient_evidence và needs_investigation theo đúng tình trạng thực tế.
- Output vẫn phải qua Verifier trước khi trả về.
- Nếu không thể tạo output hợp lệ và trung thực, báo lỗi xử lý case.

### Quy tắc ghi trace

- Các decision_code trong bảng là quy ước nội bộ.
- Chỉ dùng event_type có trong public trace schema.
- Chỉ ghi task_assigned khi thực sự giao hoặc giao lại nhiệm vụ.
- Chỉ ghi handoff khi thực sự chuyển kết quả hoặc báo cáo lỗi.
- Retry kỹ thuật bên trong một nhiệm vụ không tự động tạo thành một sự kiện task_assigned.
- Có thể ghi số lần thử bằng attributes với giá trị đơn.
- Không ghi API key, header xác thực hoặc nguyên văn lỗi chứa bí mật.

EvidenceCollector đã triển khai retry cho TimeoutError trực tiếp: tối đa 3 lần gọi, backoff 1 và 2 giây, tuân theo deadline case. Lỗi tool chung không được tự động retry.

Chưa xử lý phân loại timeout nằm trong ExceptionGroup hoặc các loại lỗi tạm thời khác. Phần điều phối fallback và vòng bổ sung vẫn cần triển khai.

### Lỗi thực thi tool không rõ nguyên nhân

Đã quan sát get_refund_timeline trả lỗi: "Error executing tool get_refund_timeline".

- Phân loại là lỗi thực thi chưa rõ nguyên nhân.
- Không chuyển lỗi thành kết quả rỗng hoặc kết luận chưa hoàn tiền.
- Không tạo evidence_ref cho lần gọi thất bại.
- Không emit tool_result_consumed cho phản hồi lỗi này.
- Payment báo needs_more_evidence cho Coordinator.
- Khi workflow thực sự chuyển báo cáo lỗi, dùng handoff với decision_code = MCP_TOOL_EXECUTION_FAILED.
- Chỉ retry tự động sau khi xác định lỗi thuộc nhóm tạm thời.

## 6. Verification invariants

Verifier kiểm tra các điều kiện sau trước khi cho phép finalize.

### 1. Public output contract

- Output vượt qua Contracts.validate_output().
- schema_version đúng với variant L3A.
- Có đầy đủ field bắt buộc, không có field ngoài schema.
- Các enum, kiểu dữ liệu và giới hạn số phần tử đúng schema.

### 2. Case và entity scope

- case_id của output khớp chính xác case đầu vào.
- Entity được kết luận là bị ảnh hưởng phải thuộc phạm vi case và có căn cứ từ dữ liệu đã xác minh.
- Không đưa entity từ case khác vào output.

### 3. Evidence và claim linkage

- Mọi evidence_ref được trích dẫn, kể cả trong claim_assessments, đều tồn tại trong kho evidence của case hiện tại.
- Evidence hỗ trợ đúng claim hoặc kết luận được gắn.
- Khi có claim_assessments, claim_id phải khớp claim đầu vào.
- Không coi lời khách hàng khai là bằng chứng MCP.
- Không xem việc evidence_ref đúng định dạng là đủ để xác nhận
  provenance hợp lệ.

### 4. Financial consistency

- Tính toán tiền nội bộ bằng Decimal, làm tròn theo policy áp dụng.
- currency là BRL.
- Các khoản hoàn tiền không âm.
- recommended_refund_brl bằng tổng amount_brl trong refund_lines.
- Không tính hoàn trùng một khoản.
- Mỗi khoản hoàn có căn cứ từ bằng chứng và chính sách.
- Khi tạo output, chuyển giá trị tiền thành JSON number phù hợp; không xuất Decimal dưới dạng chuỗi.

### 5. Business consistency

- primary_issue, case_status và resolution_actions không mâu thuẫn.
- ranked_causes có căn cứ và thứ hạng không trùng nhau.
- responsible_parties phù hợp bằng chứng về trách nhiệm.
- Không quy trách nhiệm cho một bên khi chưa có đủ căn cứ.
- Nếu chọn selected_source trong data_conflicts, nguồn đó phải nằm trong sources và việc lựa chọn phải có căn cứ.
- Nếu chưa giải quyết được xung đột, không giả vờ đã xác định được nguồn đúng.

### 6. Confidence

- Mọi confidence nằm trong khoảng từ 0 đến 1.
- Confidence phản ánh mức hỗ trợ của bằng chứng đối với kết luận.
- Không tự động đặt confidence cao chỉ vì output pass schema.

### 7. Kết quả verification

- Nếu đạt: emit verification_completed với decision_code = PASS.
- Nếu không đạt: emit verification_completed với decision_code = NEEDS_REWORK và trả danh sách lỗi cụ thể qua message nội bộ cho Coordinator.
- Coordinator chỉ giao bổ sung nếu còn ngân sách vòng và thời gian.
- Sau mọi chỉnh sửa, output phải được kiểm tra lại.
- CLI chỉ emit `case_finalized` sau khi output đã vượt qua verification, kiểm tra của CLI và được ghi ra file.

Workflow đã triển khai các invariant nghiệp vụ cục bộ nêu trên. `Contracts.validate_output()`
ở CLI vẫn được giữ làm cổng kiểm tra public contract cuối cùng; không sửa schema để hợp thức
hóa output.

## 7. Reproducibility

### Nền tảng triển khai

- Python 3.11 trở lên theo yêu cầu của starter repo.
- Framework điều phối dự kiến: Python async state-machine.
- Điểm vào: `src/student_agent/workflow.py`, hàm `solve_case()`.
- Model LLM: chưa chốt. Khi sử dụng, ghi rõ model ID và các tham số thực tế; nếu không sử dụng thì ghi rõ không dùng LLM.
- Không ghi API key hoặc thông tin xác thực trong tài liệu và trace.

### Cấu hình chạy dự kiến

| Tham số | Giá trị |
| --- | --- |
| Số case xử lý đồng thời | 1 |
| Số MCP call đồng thời trong mỗi case | Tối đa 2 |
| Deadline toàn bộ solve_case | 180 giây |
| Timeout mỗi lần gọi MCP | Tối đa 30 giây, không vượt deadline còn lại |
| Retry cho lỗi MCP tạm thời | Tối đa 2 lần ngoài lần gọi đầu |
| Backoff trước các lần retry | 1 giây, 2 giây |
| Số vòng sửa hoặc bổ sung A2A | Tối đa 1 |

Đây là cấu hình mục tiêu, cần được triển khai và kiểm tra trong code.
Timeout của workflow được áp dụng bên ngoài `gateway.call()`;
không mặc định coi timeout HTTP có sẵn là timeout của workflow.

### Dependency và phiên bản

- Khi hoàn thiện triển khai, ghi phiên bản Python thực tế.
- Ghi phiên bản dependency thực tế và lưu bằng cơ chế pin/lock mà dự án sử dụng.
- Ghi commit source và `case_set_version` của lần chạy.
- Giữ nguyên public schemas đã phát hành.
- Nếu có sử dụng ngẫu nhiên, ghi seed và nơi áp dụng.
- Không cam kết kết quả LLM giống tuyệt đối giữa các lần chạy.

### Lệnh kiểm tra và chạy

Thực hiện tại root repo, sau khi đã cài đặt môi trường và cấu hình.

Kiểm tra input và discovery tool:

```powershell
day09 validate-inputs
day09 mcp-tools
```

Sau khi workflow đã được triển khai, chạy, kiểm tra kết quả và đóng gói:

```powershell
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Không đưa `.env`, API key, source hoặc input vào ZIP nộp bài.

### Thông tin đã xác nhận

- `day09 validate-inputs` đã thành công với 100 case L3A.
- `case_set_version` hiện tại là `l3a-competition-v1`.
- Tên và arguments của 10 tool đã được xác nhận qua MCP discovery, xem mục 2.
- Việc xác nhận metadata chưa đồng nghĩa đã triển khai phân quyền tool hoặc kiểm tra cấu trúc dữ liệu trả về.

### Thông tin cần bổ sung sau khi triển khai

- Phiên bản Python và dependency thực tế.
- Model/config thực tế nếu sử dụng LLM.
- Commit source và xác nhận lại `case_set_version` của lần chạy nộp bài.
- Kết quả chạy validation và các giới hạn còn tồn tại.
