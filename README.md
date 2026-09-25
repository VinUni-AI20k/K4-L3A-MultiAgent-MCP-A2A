# K4 L3A — Multi-Agent MCP + A2A

## Mục tiêu

Xây dựng hệ thống multi-agent điều tra khiếu nại thương mại điện tử.

Agent phải:

- đọc yêu cầu của khách hàng;
- lấy dữ liệu có thẩm quyền qua MCP Evidence Gateway;
- phối hợp giữa các agent để đưa ra kết luận;
- tạo output và trace đúng public contract.

Customer message không phải ground truth. Không được tự đoán dữ liệu hoặc tạo `evidence_ref` giả.

## Dữ liệu

Tham khảo dữ liệu tại: https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce

## Quy tắc đặt tên

Làm nhóm hoặc cá nhân, khi fork về các bạn giữ nguyên tên gốc repo, không đổi tên

## 1. Cài đặt

Yêu cầu Python 3.11 trở lên.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Nếu PowerShell chặn chạy script khi kích hoạt môi trường ảo, chạy lệnh sau trong cửa sổ hiện tại rồi kích hoạt lại:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Kiểm tra:

```powershell
pytest -q
python -m student_agent.cli --help
```

Các lệnh bên dưới gọi CLI qua Python của môi trường đang kích hoạt (`python -m student_agent.cli`), nên không phụ thuộc PowerShell có tìm thấy `day09.exe` trong `PATH` hay không. Hãy kích hoạt `.venv` của repo trước khi chạy.

## 2. Đăng ký team

1. Mở `/register` trên Competition Workspace.
2. Điền tên team, mã học viên và các thành viên.
3. Nhập registration code của lớp.
4. Lưu Team API Key dạng `sk-team-...` được hiển thị sau khi đăng ký.

Điền thông tin thật vào `.env`:

```dotenv
COMPETITION_API_URL=http://127.0.0.1:8081
COMPETITION_TEAM_API_KEY=sk-team-your_key
MCP_ENDPOINT=http://127.0.0.1:8001/mcp
```

## 3. Tải input

Tải ZIP input **L3A** từ GitHub Release và giải nén vào root repo:

```powershell
Expand-Archive -LiteralPath .\l3a-inputs-<version>.zip -DestinationPath .
python -m student_agent.cli validate-inputs
```

Cấu trúc đúng:

```text
case-set.json
inputs/
├── L3A_CASE_001.json
├── ...
└── L3A_CASE_100.json
```

## 4. Sử dụng MCP

MCP Gateway cung cấp evidence về order, item, payment, shipment, seller và policy. Mọi call sẽ được server audit nên mọi người lưu ý config đúng để đảm bảo quyền lợi

Xem các tool hiện có:

```powershell
python -m student_agent.cli mcp-tools
```

Ví dụ gọi tool trong `workflow.py`:

```python
evidence = await gateway.call(
    "get_order",
    case_id=case["case_id"],
    order_id=order_id,
)

evidence_ref = evidence["evidence_ref"]
order_data = evidence["data"]
```

Khi dùng evidence để đưa ra kết luận, ghi lại trong trace:

```python
trace.emit(
    case_id=case["case_id"],
    event_type="tool_result_consumed",
    actor="order-agent",
    tool_name="get_order",
    evidence_refs=[evidence_ref],
)
```

Quy tắc quan trọng:

- luôn truyền đúng `case_id`;
- dùng tool discovery, không đoán tên tool;
- không sửa hoặc tự tạo `evidence_ref`;
- không dùng evidence chéo case;
- chỉ trích dẫn evidence thật sự hỗ trợ kết luận.

## 5. Xây dựng multi-agent workflow

Triển khai tại:

```text
src/student_agent/workflow.py
```

Hàm chính:

```python
async def solve_case(case, gateway, trace) -> dict:
    ...
```

Gợi ý có thể tổ chức các vai trò:

- coordinator;
- order/item agent;
- payment agent;
- shipment agent;
- policy agent;
- verifier.

Competition không chấm tên framework hay số lượng class. Scorer đánh giá kết quả, evidence và sự phối hợp thể hiện trong trace.

Hoàn thiện mô tả thiết kế trong `ARCHITECTURE.md`.

## 6. Chạy và kiểm tra

```powershell
python -m student_agent.cli run
python -m student_agent.cli validate
```

Kết quả được tạo tại:

```text
outputs/<case_id>.json
traces/trace.jsonl
```

Nếu output pass schema nhưng điểm thấp, cần kiểm tra lại semantic, evidence, consistency, confidence và workflow — schema chỉ là một phần nhỏ của điểm.

## 7. Đóng gói và nộp bài

```powershell
python -m student_agent.cli package --output dist/submission.zip
```

ZIP chỉ được chứa:

```text
manifest.json
trace.jsonl
outputs/<case_id>.json
```

Không đưa source, input, `.env`, API key hoặc debug log vào ZIP. Sau đó upload `dist/submission.zip` tại workspace `/l3a`

## Tiêu chí chấm điểm công khai

| Thành phần                                     | Trọng số |
| ---------------------------------------------- | -------: |
| Độ đúng nghiệp vụ (`semantic`)                 |      45% |
| Chất lượng bằng chứng (`evidence`)             |      15% |
| Evidence đúng MCP audit (`provenance`)         |      15% |
| Tính nhất quán giữa các field (`consistency`)  |      10% |
| Đúng JSON Schema (`schema`)                    |       5% |
| Confidence hợp lý (`calibration`)              |       5% |
| Quy trình multi-agent trong trace (`workflow`) |       5% |
| Hiệu quả gọi tool (`efficiency`)               |       0% |

L3A không cộng điểm efficiency trực tiếp, nhưng MCP calls vẫn được audit để kiểm tra tính hợp lệ.

Case có thể nhận 0 điểm nếu:

- sai `case_id` hoặc output không thể chấm theo schema;
- thiếu evidence bắt buộc;
- evidence ref không tồn tại;
- evidence thuộc team, run hoặc case khác.
