# Chạy bài L3A cá nhân

## Cài đặt

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt -e '.[dev]'
```

Nếu đã có `.venv` và `.env` thì giữ cấu hình hiện có. Team API key không được
commit hoặc gửi trong chat. `.env.example` chứa endpoint công khai và placeholder.

## Kiểm tra và tạo bài nộp

```bash
pytest -q
ruff check src tests
day09 validate-inputs
day09 run
day09 validate
day09 package --output dist/submission.zip
```

`day09 run` tự tạo/gia hạn run L3A trên workspace, kiểm tra case-set version,
discovery tool rồi chạy 100 case. Chương trình giới hạn 2 case đồng thời và in
tiến độ. Output mới chỉ thay bản cũ sau khi tất cả case pass local checks; bản cũ
được sao lưu trong `dist/previous-runs/`.

Nếu tất cả MCP tool báo lỗi trong khi `mcp-tools` vẫn chạy, kiểm tra thời hạn run.
Nếu tạo run trả 401/403, kiểm tra key/quyền team; nếu version/endpoint không khớp,
dùng thông tin được workspace cung cấp. Lỗi server không được coi là order không
tồn tại hoặc bằng chứng cho `no_action`.

Khi thành công, upload `dist/submission.zip` vào `/l3a`. CLI không tự upload vì
workspace có giới hạn số lượt nộp. Tránh tạo run mới trước khi nộp artifacts của
run hiện tại; server kiểm tra evidence theo team/run/case.

## Hiểu kết quả kiểm tra

- Unit test dùng fixtures tổng hợp, không phải đáp án của bộ đề.
- `day09 validate` kiểm schema, lifecycle, trace linkage và scope cục bộ.
- Điểm semantic, evidence coverage và calibration phải xem trên scorer.
- L3A đặt trọng số efficiency bằng 0 theo public policy; ưu tiên correctness và
  evidence liên quan. Call budget vẫn được audit.
- Không sửa public contracts, không dùng claim topic làm đáp án và không dùng
  evidence giả để lấp case thất bại.

Kiến trúc đang triển khai được mô tả trong `ARCHITECTURE.md`. `team-guide.md` là
tài liệu phân công cũ, không còn là kế hoạch thực hiện hiện tại.
