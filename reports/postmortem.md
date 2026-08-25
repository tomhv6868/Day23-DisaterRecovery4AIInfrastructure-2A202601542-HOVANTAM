# Postmortem — DR drill ngày 25-08-2026

Đây là bài phân tích không đổ lỗi cá nhân. Mục tiêu là tìm chỗ hệ thống và quy trình còn yếu, ko phải tìm người đã chạy lệnh chaos.

## 1. Timeline

| Thời gian UTC | Sự kiện | Bằng chứng |
|---|---|---|
| `2026-08-25T16:39:18.010616Z` | Region A bị `netblock`, bắt đầu đồng hồ RTO | `chaos/chaos-events.jsonl:3` |
| `2026-08-25T16:39:18.037624Z` | Người dùng nhận lỗi đầu tiên | `reports/drill-2-withdr.jsonl:25` |
| `2026-08-25T16:39:30.935533Z` | Runbook xác nhận outage qua 3 probe và mở sự cố | `reports/runbook-run.jsonl:2` |
| `2026-08-25T16:39:33.831475Z` | Health checker nền đánh dấu A `UNHEALTHY` | `reports/health-events.jsonl:2` |
| `2026-08-25T16:39:35.019216Z` | Snapshot dc phục hồi sang B, mất 7 tài liệu mới nhất | `reports/failover-events.jsonl:2` |
| `2026-08-25T16:39:41.107877Z` | Region B đã sẵn sàng và DNS/LB cutover sang B | `reports/failover-events.jsonl:5` |
| `2026-08-25T16:39:47.006001Z` | Request đầu tiên thành công từ B, sự cố được giải quyết | `reports/drill-2-withdr.jsonl:39` |

Runbook tự xác nhận bằng 3 probe sớm hơn event của health checker nền khoảng 2,9 giây. tuy vậy cutover vẫn chờ qua detection gate, nên không có cảnh báo `t_cutover < t_detect`.

## 2. RTO/RPO so với mục tiêu và gap

- RTO mục tiêu: `300s`; đo được: `29.0s`; gap theo nghĩa dư địa còn lại: `271.0s`.
- RPO mục tiêu: `300s`; đo được: `3.5s`, tương ứng 7 tài liệu; gap còn lại: `296.5s`.
- Bước tốn nhiều thời gian nhất là phát hiện outage: `15.8s`, bằng khoảng 54,5% RTO thực đo. Trong đó detection floor cấu hình là `15.0s`.
- GPU pool warm-up mất `6.1s`; DNS/LB cùng nhịp request mất thêm `5.9s`.

RTO và RPO đều đạt mục tiêu. điểm chưa đẹp là hơn nửa RTO nằm ở khâu phát hiện, còn RPO phụ thuộc đúng thời điểm snapshot gần nhất hoàn tất.

## 3. Nguyên nhân gốc — 5 câu hỏi vì sao

1. **Vì sao request lỗi?** Edge vẫn trỏ vào Region A trong lúc tiến trình A bị treo bởi `netblock`, nên request chờ hết timeout.
2. **Vì sao traffic không sang B ngay?** Cần đủ 3 readiness probe lỗi liên tiếp để tránh flapping, sau đó mới cho phép failover.
3. **Vì sao B chưa thể nhận traffic khi outage vừa xảy ra?** B theo mô hình active-passive: pool còn `warm`, thiếu model weight và cơ sở dữ liệu vector.
4. **Vì sao cần thêm thời gian restore và warm-up?** State chỉ dc sao chép theo chu kỳ, còn GPU pool chỉ chuyển sang `full` khi sự cố đã dc xác nhận.
5. **Vì sao vẫn có rủi ro nếu đây là outage thật?** Backend `fs` đặt snapshot trên cùng máy lab. Nếu máy hoặc ổ đĩa mất hoàn toàn thì bước restore có thể k chạy dc, dù code failover đúng.

Nguyên nhân gốc ko phải lệnh chaos. Đó là lựa chọn active-passive kết hợp health check có ngưỡng, snapshot theo chu kỳ và bản sao chưa nằm trên failure domain độc lập.

## 4. Action item — hạng mục cần làm

| # | Hạng mục | Người phụ trách | Hạn hoàn thành | Tác động dự kiến |
|---:|---|---|---|---|
| 1 | Giảm health-check interval từ 5 giây xuống 2 giây, vẫn giữ threshold bằng 3 và chạy thử chống flapping | Nhóm SRE | 01-09-2026 | Detection floor từ `15s` xuống `6s`, giảm tối đa khoảng `9s` RTO |
| 2 | Giữ Region B ở trạng thái pre-warm đầy đủ trong khung giờ quan trọng | Nhóm nền tảng AI | 08-09-2026 | Bỏ gần `6.1s` warm-up, đổi lại tốn thêm tài nguyên GPU |
| 3 | Giảm chu kỳ replication từ 30 giây xuống 10 giây và cảnh báo khi snapshot trễ | Nhóm dữ liệu | 03-09-2026 | Giảm RPO biên trên khoảng `20s`, nhưng tăng I/O |
| 4 | Chuyển snapshot sang object store ở failure domain độc lập và diễn tập restore hằng tháng | Nhóm hạ tầng | 15-09-2026 | Không giảm nhiều RTO đo trong lab, nhưng tránh mất luôn nguồn phục hồi |

Các mức giảm trên là dự kiến, chưa dc xem là số đo cho đến khi chạy lại drill.

## 5. Ba câu hỏi bắt buộc

1. `interval × threshold = 5s × 3 = 15s`. Phần này chiếm `15 / 29 × 100 = 51.7%` RTO; độ trễ phát hiện thực tế là `15.8s`.
2. Nếu interval giảm còn 1 giây, detection floor còn `3s`, lý thuyết giảm `12s`. đổi lại số probe tăng 5 lần, tốn kết nối hơn và dễ tạo false positive; threshold vẫn phải giữ để hạn chế flapping.
3. `docs_lost=7` nghĩa là Region A có 7 tài liệu mới hơn snapshot mà B không nhận dc. Nếu A mất vĩnh viễn trong outage 6 giờ, câu trả lời từ B có thể thiếu tri thức của 7 tài liệu đó; chỉ replay dc khi còn nguồn sự kiện hoặc bản ghi gốc khác.

## 6. Điều đã hoạt động

- Không cutover trước khi B trả readiness thành công.
- Năm bước failover xuất hiện đúng thứ tự và embedding model version dc ghi lại.
- 10 golden request đều thành công, error rate bằng 0 và p95 là `7.7ms` tại `reports/runbook-run.jsonl:6`.
- Kết quả cuối có `valid:true`, không có warning và request phục hồi do B phục vụ tại `reports/measure-drill-2.json:2` đến `reports/measure-drill-2.json:6`.
