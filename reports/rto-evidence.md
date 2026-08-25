# Bằng chứng RTO/RPO — bài thực hành 23

Các số dưới đây lấy từ lần chạy ngày 25-08-2026. Mình giữ nguyên timestamp trong log, ko nội suy bằng cảm giác.

## 1. Drill 1 — chưa có DR

| Chỉ số | Giá trị | Cách đo | Bằng chứng |
|---|---:|---|---|
| Thời điểm outage | `2026-08-25T16:38:45` | Sự kiện `kill`, chế độ `netblock` | `chaos/chaos-events.jsonl:1` |
| Request lỗi đầu tiên | `+0.5s` | Dòng `ok:false` đầu tiên sau outage | `reports/drill-1-nodr.jsonl:12` |
| Tổng request lỗi sau outage | `7` | Kết quả của công cụ đo | `reports/measure-drill-1.json:28` |
| Request thành công sau đó | Không có | `recovered_by_region` là `null` | `reports/measure-drill-1.json:9` |
| RTO | `NO_RECOVERY` | Không có request nào phục hồi trong cửa sổ tải | `reports/measure-drill-1.json:25` |

Hai cảnh báo ở baseline là đúng dự kiến vì health checker và failover chưa chạy. quan trọng là outage có tác động thật, 7 request lỗi và Region A ko tự hồi phục.

## 2. Drill 2 — có DR

| Mốc | Thời gian từ outage | Cách đo | Bằng chứng |
|---|---:|---|---|
| Outage bắt đầu | `0.0s` | Sự kiện `kill` Region A | `chaos/chaos-events.jsonl:3` |
| Người dùng thấy lỗi đầu tiên | `+0.0s` | Request đầu tiên sau outage có `ok:false` | `reports/drill-2-withdr.jsonl:25` |
| Health checker phát hiện | `+15.8s` | Region A chuyển sang `UNHEALTHY` sau 3 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| Snapshot dc phục hồi | `+17.0s` | Bước `2_restore_snapshot` hoàn tất | `reports/failover-events.jsonl:2` |
| Region B sẵn sàng | `+23.1s` | `/readyz` đã qua, pool đầy và có 260 vector | `reports/failover-events.jsonl:4` |
| DNS/LB cutover | `+23.1s` | Con trỏ traffic đổi từ A sang B | `reports/failover-events.jsonl:5` |
| Request thành công đầu tiên từ B | `+29.0s` | Request đầu tiên sau chuỗi lỗi có `served_by:"b"` | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu | Kết luận | Bằng chứng |
|---|---:|---:|---|---|
| RTO — API suy luận | `29.0s` | `300s` | Đạt | `reports/measure-drill-2.json:20` |
| RPO — cơ sở dữ liệu vector | `3.5s` / `7` tài liệu | `300s` | Đạt | `reports/failover-events.jsonl:2` |

Kết quả đo có `valid:true`, danh sách cảnh báo rỗng và request phục hồi do Region B phục vụ. số liệu này nằm tại `reports/measure-drill-2.json:2`, `reports/measure-drill-2.json:4` và `reports/measure-drill-2.json:6`.

## 3. RTO gồm những gì

Cấu hình health check là `interval=5s`, `threshold=3`, nên detection floor là `15.0s`; cấu hình thật nằm trong `reports/health-events.jsonl:2`. Bảng dưới dùng timestamp thực đo để bốn phần cộng đúng RTO.

| Thành phần | Thời gian | Cách tính | Cách giảm |
|---|---:|---|---|
| Phát hiện outage | `15.8s` | `t_detect - t_outage`; floor cấu hình là `15.0s` | Giảm interval nhưng vẫn giữ threshold để hạn chế flapping |
| Điều phối và phục hồi snapshot | `1.2s` | `t_restore - t_detect`; gồm chuyển giao từ alert, kiểm tra target và restore | Tự động chuẩn bị artifact, kiểm tra định kỳ khả năng restore |
| GPU pool warm-up | `6.1s` | `t_ready - t_restore`; log ghi `waited_s=6.088` | Giữ pool dự phòng ở trạng thái đầy hoặc pre-warm |
| DNS/LB và nhịp request | `5.9s` | `t_recovered - t_cutover` | Hạ TTL hợp lý và dùng global load balancer chủ động |
| **Tổng** | **`29.0s`** | `15.8 + 1.2 + 6.1 + 5.9` | Khớp RTO đo từ phía người dùng |

Mốc phát hiện đối chiếu `chaos/chaos-events.jsonl:3` với `reports/health-events.jsonl:2`; restore và warm-up đối chiếu `reports/failover-events.jsonl:2` cùng `reports/failover-events.jsonl:4`; phần DNS/LB đối chiếu `reports/failover-events.jsonl:5` với `reports/drill-2-withdr.jsonl:39`.

## 4. RPO và tính toàn vẹn state

Snapshot gần nhất dc tạo ở `reports/replication.jsonl:2`. Khi restore, Region A có thêm 7 tài liệu mới hơn bản snapshot, tương ứng `3.5s` dữ liệu chưa kịp sao chép. Phiên bản embedding model là `embed-model=vi-e5-base@v3`, nên vector và model weight vẫn tương thích; bằng chứng nằm cùng sự kiện restore tại `reports/failover-events.jsonl:2`.
