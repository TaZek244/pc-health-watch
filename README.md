# 🩺 pc-health-watch

Theo dõi sức khỏe máy Linux liên tục, **cảnh báo trước khi máy đơ**, và ghi **hộp đen** xuống
đĩa để sau khi phải bấm reset vẫn biết lúc đơ máy đang ra sao.

Viết cho trường hợp: Ubuntu 20.04 cài trên một SSD riêng, thỉnh thoảng đơ cứng / đứng màn hình.
Chỉ cần Python 3.8 + thư viện chuẩn (có sẵn trên Ubuntu 20.04), không cần root.

Tách và nâng cấp từ `alert-watch.py` của repo `custom_desktop`. Bản cũ chỉ theo dõi pin, Wi-Fi và
Internet; bản này theo dõi những thứ thực sự làm máy đơ.

## Theo dõi những gì

| Nhóm | Nguồn | Cảnh báo khi (mặc định) |
|---|---|---|
| RAM | `/proc/meminfo` | dùng ≥ 90% (warning) / ≥ 96% (critical) liên tục 20 s |
| Swap | `/proc/meminfo` | dùng ≥ 70% / ≥ 90% |
| **Áp lực bộ nhớ (PSI)** | `/proc/pressure/memory` | `full avg10` ≥ 10% / ≥ 30%: dấu hiệu thrashing, báo sớm nhất trước khi đơ |
| Áp lực ổ đĩa (PSI) | `/proc/pressure/io` | `full avg10` ≥ 30% / ≥ 60% |
| Độ trễ I/O | `/proc/diskstats` | trễ ≥ 300 ms / ≥ 1500 ms mỗi lệnh (SSD khỏe < 20 ms) |
| Nhiệt độ | `/sys/class/hwmon` | ≥ 88°C / ≥ 97°C |
| Dung lượng | `statvfs` | phân vùng dùng ≥ 90% và còn < 5 GB / < 1 GB |
| Read-only | `/proc/mounts` | `/` hoặc `/home` bị kernel remount chỉ-đọc (lỗi đĩa) |
| **Lỗi kernel** | `journalctl -k -f` | I/O error, NVMe timeout, ATA reset, EXT4 error, OOM, hung task, soft lockup, GPU Xid/hang, MCE… |
| **SMART** | `smartctl -j` | SSD báo lỗi, sector hỏng, media error, mòn ≥ 90% (mỗi 60 phút) |

Cảnh báo có **thời gian giữ** (không báo vì đỉnh nhọn), **hysteresis** (không nhấp nháy),
**nhắc lại** mỗi 10 phút nếu còn critical, và báo khi **đã trở lại bình thường**.

Mức critical hiện **popup GTK giữa màn hình** + tiếng bíp; warning chỉ hiện thông báo desktop.

## Cài đặt

```bash
git clone <repo> ~/pc-health-watch && cd ~/pc-health-watch
./install.sh                 # chép vào ~/.local/share, tạo lệnh `healthwatch`, bật systemd --user
healthwatch check            # kiểm tra môi trường, in lệnh sửa cho từng mục thiếu
```

Khuyên chạy thêm (cần sudo, mỗi bước đều hỏi trước):

```bash
./install.sh --setup-system  # journal lưu bền + Magic SysRq + earlyoom + nhóm adm + smartmontools
./install.sh --smart-sudoers # cho phép daemon đọc SMART mà không cần mật khẩu (chỉ đúng lệnh smartctl -j -a)
```

Gói hệ thống dùng tới (đa số có sẵn trên Ubuntu desktop):
`python3 python3-gi gir1.2-gtk-3.0 libnotify-bin` và tùy chọn `smartmontools earlyoom`.

## Dùng

```bash
healthwatch status           # tình trạng hiện tại (đọc từ daemon, hoặc đo trực tiếp)
healthwatch history -m 30    # bảng số liệu hộp đen 30 phút gần nhất
healthwatch report           # ★ sau khi máy đơ phải reset: phân tích lần boot trước
healthwatch report --boot -2 # lần trước nữa
healthwatch test-alert --critical
healthwatch config           # cấu hình đang dùng
journalctl --user -u pc-health-watch -f   # log của daemon
```

### Sau một lần đơ máy

1. Bật máy lên, chạy `healthwatch report`.
2. Báo cáo cho biết lần boot trước có tắt máy sạch không, thời điểm cuối cùng có số liệu,
   giá trị đỉnh trong 10 phút cuối (RAM, swap, PSI, nhiệt, I/O), tiến trình ngốn RAM nhất,
   lỗi kernel và **nguyên nhân khả dĩ**.
3. Tra bảng "Thấy gì → nghi gì" trong [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

Nếu số liệu cuối cùng hoàn toàn bình thường và không có lỗi kernel nào thì máy chết đột ngột.
Nghi phần cứng: driver GPU, RAM, nguồn, firmware/APST của SSD. Xem mục 1.2, 1.3, 1.8 và 4 của tài liệu.

## Cấu hình

`~/.config/pc-health-watch/config.json`. Chỉ cần ghi khóa muốn đổi; daemon tự nạp lại khi file thay đổi.
Xem toàn bộ khóa: `healthwatch config --default`.

```json
{
  "rules": {
    "memory_used": {"warning": 85, "critical": 95, "for_seconds": 15},
    "temperature": {"warning": 85}
  },
  "notify": {"sound": false, "repeat_minutes": 5},
  "actions": {"relieve_memory": {"enabled": true}}
}
```

`actions.relieve_memory` (**tắt mặc định**): khi PSI memory full ≥ 40% kéo dài 15 giây, gửi
SIGTERM cho tiến trình ngốn RAM nhất **của bạn** (bỏ qua danh sách `protect`). Đây là lưới an toàn
cuối cùng; nên cài thêm `earlyoom` vì nó phản ứng nhanh hơn.

## Hộp đen

`~/.local/state/pc-health-watch/blackbox-YYYY-MM-DD.jsonl`: mỗi 10 giây một dòng JSON, ghi kèm
`fsync` để dữ liệu thực sự nằm trên SSD trước khi máy đơ. Giữ 14 ngày, khoảng vài MB mỗi ngày.

## Phát triển

```bash
python3 -m unittest discover -s tests     # 45 test, không cần root
python3 -m healthwatch status             # chạy thẳng từ repo
python3 -m healthwatch.collectors.system  # in 2 snapshot thô
```

Kiến trúc và định dạng dữ liệu: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
