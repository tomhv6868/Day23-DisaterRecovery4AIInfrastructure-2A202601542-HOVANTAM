# Runbook một trang — Region chính ngừng phục vụ

Chạy từ thư mục gốc của repo. Sự cố thật dùng chế độ xác nhận thủ công; `--auto` chỉ dành cho drill hoặc CI. Nếu một bước chưa đạt tín hiệu hoàn tất thì ko chuyển sang bước kế tiếp.

| # | Bước | Lệnh có thể sao chép | Biết là xong khi | Người phụ trách |
|---:|---|---|---|---|
| 1 | Xác nhận outage | `for lan in 1 2 3; do curl --max-time 2 --silent --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8001/readyz; sleep 5; done; curl --silent http://127.0.0.1:8002/healthz` | Region A ko trả `200` trong 3 lần liên tiếp; Region B vẫn trả `alive:true` | Kỹ sư trực |
| 2 | Mở sự cố, bấm giờ và xác nhận failover | `python3 dr/runbook.py --primary a --target b --backend fs` | Nhập `y` tại lời nhắc; `reports/runbook-run.jsonl` có bước `thong_bao_incident` và `operator_confirmed:true` | Chỉ huy sự cố |
| 3 | Theo dõi phục hồi snapshot | `rg '"step": "2_restore_snapshot".*"ok": true' reports/failover-events.jsonl` | Có `rpo_seconds`, `docs_lost`, `embed_model_version`; trong lần drill này là `3.5`, `7`, `embed-model=vi-e5-base@v3` | Kỹ sư dữ liệu |
| 4 | Xác nhận GPU pool và Region B sẵn sàng | `curl --fail --silent http://127.0.0.1:8002/readyz` | HTTP `200`, `ready:true`, `pool_state:full`, số vector lớn hơn 0 | Kỹ sư nền tảng AI |
| 5 | Xác nhận DNS/LB cutover | `curl --silent http://127.0.0.1:8080/edge/state` | `active_region:b`; sự kiện `5_dns_cutover` chỉ xuất hiện sau bước Region B sẵn sàng | Kỹ sư mạng |
| 6 | Kiểm tra golden signals | `rg '"name": "verify_golden_signals".*"ok": true' reports/runbook-run.jsonl` | Đủ 10 request thật, `errors:0`, `error_rate:0.0`; p95 dưới `2000ms` | Kỹ sư trực |
| 7 | Đo RTO/RPO và cập nhật postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, `warnings:[]`, `rto_verdict:PASS`, request phục hồi do Region B phục vụ | Chỉ huy sự cố |

## Điều kiện dừng và rollback

Trước cutover, nếu snapshot thiếu, sai phiên bản embedding model, `docs_lost` ko đo dc, hoặc `/readyz` của B không trả `200` trong thời gian chờ thì dừng quy trình. `dr/failover.py` phải giữ nguyên con trỏ traffic ở A; không sửa `edge/active_region` bằng tay.

Sau cutover, chỉ failback khi Region A đáp ứng đủ các điều kiện sau:

- `/readyz` trả `200` trong 3 lần liên tiếp, cách nhau 5 giây.
- State từ Region B đã dc snapshot và phục hồi về A, phiên bản model khớp, vector count lớn hơn 0.
- 10 request kiểm tra trực tiếp vào A có error rate bằng 0 và p95 dưới `2000ms`.
- Region B vẫn khỏe trong suốt lúc chuẩn bị, để còn đường lùi nếu A lại lỗi.

Lệnh failback trong bare mode của drill:

```bash
python3 chaos/kill_region.py restore --region a --backend bare
python3 state/snapshot.py put --region b --backend fs
python3 dr/failover.py --target a --backend fs
```

Chỉ huy sự cố là người có quyền quyết định failback; kỹ sư trực thi hành và ghi timestamp. Không trả traffic về A chỉ vì một probe vừa xanh, vì làm vậy dễ khiến hai Region flap qua lại.
