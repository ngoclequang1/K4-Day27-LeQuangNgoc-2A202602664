# Báo cáo hoàn thiện Lab 27

Đã hoàn thiện 5 bài tập theo README. Các file trong `common/` được giữ nguyên; audit sử dụng SQLite theo phương án zero-setup của đề, chưa triển khai PostgreSQL.

## Chức năng đã triển khai

| Bài | Kết quả |
| --- | --- |
| 1 | LLM trả `PRAnalysis`; graph định tuyến confidence dưới 0.58, từ 0.58 đến dưới 0.73 và từ 0.73 trở lên. |
| 2 | `interrupt()` cho approve/reject/edit; `MemorySaver`; resume cùng thread. |
| 3 | Sinh câu hỏi escalation, thu câu trả lời, tổng hợp lại review và yêu cầu xác nhận cuối. |
| 4 | `AuditEntry` ở các bước xử lý; SQLite checkpoint; khôi phục thread đang chờ; replay và danh sách phiên. |
| 5 | Streamlit có nhập PR, xem diff/reasoning/comments/risks, phê duyệt, chỉnh sửa, trả lời câu hỏi, xem audit và mở lại phiên. |

Edit nhận **toàn bộ nội dung bình luận thay thế**. Reject không đăng bình luận. Sau escalation, việc trả lời câu hỏi chưa đồng nghĩa với chấp thuận: reviewer xem review mới và xác nhận trước khi đăng.

Event đang chờ được ghi ở node riêng trước node interrupt, tránh ghi trùng khi node interrupt chạy lại lúc resume. Thống kê worst_risk dùng thứ tự high > med > low. Thread ID nằm trong session state và URL `?thread=...` để có thể khôi phục sau khi tải lại trang.

Đã thêm truststore trên Windows để dùng kho chứng chỉ hệ thống khi gọi HTTPS, vẫn xác minh TLS. Đã cài dependencies và cập nhật uv.lock.

## Cách chạy trên PowerShell

```powershell
uv sync
uv run streamlit run app.py
```

Nếu uv báo lỗi CA khi tải packages, dùng `uv --system-certs sync`.

Chạy từng bài:

```powershell
uv run python exercises/exercise_1_confidence.py --pr https://github.com/VinUni-AI20k/PR-Demo/pull/1
uv run python exercises/exercise_2_hitl.py --pr https://github.com/VinUni-AI20k/PR-Demo/pull/1
uv run python exercises/exercise_3_escalation.py --pr https://github.com/VinUni-AI20k/PR-Demo/pull/2
uv run python exercises/exercise_4_audit.py --pr https://github.com/VinUni-AI20k/PR-Demo/pull/2
uv run python -m audit.replay --list
```

Khôi phục bài 4 bằng `--thread <thread_id>` cùng URL PR. Xem lịch sử bằng `uv run python -m audit.replay --thread <thread_id>`.

Thêm `GITHUB_USER=<tên đăng nhập GitHub của bạn>` vào `.env` để audit ghi danh tính reviewer; nếu bỏ trống, reviewer_id là null.

## Kiểm chứng ngày 15/10/2026

```powershell
uv run python -X utf8 -m unittest discover -s tests -v
```

4 test methods đều PASS, gồm nhiều trường hợp:

- Các biên confidence 0, 0.579, 0.58, 0.72, 0.729, 0.73, 1.
- Graph bài 1 và interrupt/resume của bài 2–3.
- Nhánh auto, approve, reject, edit, escalation approve/reject và lỗi đăng bình luận.
- Đóng/mở lại SQLite rồi resume; mở thread đã hoàn tất không chạy lại hoặc đăng lại.
- Audit không lặp event approval đang chờ; kiểm tra reviewer và worst_risk.
- Streamlit AppTest: bắt đầu PR → escalation → trình duyệt mới khôi phục bằng thread URL → trả lời → xác nhận → reject, không gọi đăng GitHub.

Các test dùng PR/LLM/GitHub giả lập và database tạm, không đăng bình luận thật.

Live verification with the updated API key: PASS.

| Check | Result |
| --- | --- |
| Exercise 1, PR #1 | Confidence 65%, `human_approval`. |
| Exercise 1, PR #2 | Confidence 55%, `escalate`. |
| Full graph, PR #1 | Confidence 68%; SQLite checkpoint, reopen connection, resume and reject PASS; 6 audit events. |
| Full graph, PR #2 | Confidence 55%; escalation, real LLM synthesis, final human confirmation, resume and reject PASS; 9 audit events. |

Local audit threads:

- PR #1: `live-validation-9ad4f287-a16f-4cf7-ba90-6918f817dbc0`.
- PR #2: `live-validation-b436b8b6-05b0-433b-aa86-9b0ec22299bb`.

PR #1 scored 65% and 68% across separate LLM calls, both within the expected human-approval bucket. The escalation validation explicitly stated that no additional author context was available and asked the model to preserve unresolved risks. The final validation choice was reject, so no test comment was published. Posting behavior was verified with mocks in the offline tests. Database files and `.env` remain local and are excluded from Git.


## Giới hạn

Confidence do LLM tự báo cáo, vì vậy PR demo không được đảm bảo luôn đi vào một nhánh cố định. Nhánh >=0.73 tự đăng bình luận theo yêu cầu đề bài.

SQLite checkpoint không bảo đảm exactly-once cho thao tác GitHub: nếu tiến trình dừng ngay sau khi GitHub nhận comment nhưng trước lúc checkpoint được lưu, retry có thể đăng lặp. Luồng resume bình thường và khôi phục phiên đã hoàn tất đã được kiểm thử.
