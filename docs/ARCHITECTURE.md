# Kiến trúc pc-health-watch

Mục tiêu: theo dõi liên tục sức khỏe máy Linux (nhắm Ubuntu 20.04, chạy được trên
distro mới hơn), cảnh báo **trước** khi máy đơ, và ghi "hộp đen" xuống đĩa để sau
khi phải bấm reset vẫn biết lúc đơ máy đang ra sao.

## Ràng buộc chung

- **Python 3.8+**, chỉ dùng thư viện chuẩn (Ubuntu 20.04 có sẵn Python 3.8).
  Không dùng `match`, `X | Y` trong type hint, `dict[str, int]` ở runtime,
  `str.removeprefix`, `tomllib`, `functools.cache`, `zoneinfo`.
- Không cần root cho phần lõi. SMART cần root → tùy chọn (`sudo -n smartctl`).
- Mọi hàm đọc `/proc`, `/sys` nhận tham số `root="/"` để test bằng cây thư mục giả.
- Lỗi đọc file/thiếu lệnh không được làm sập daemon: trả `None` / danh sách rỗng.
- Test: `python3 -m unittest discover -s tests` (unittest thuần, không pytest).

## Cấu trúc

```
healthwatch/
  util.py            read_text(), run(), proc_path()
  config.py          DEFAULTS + load_config()
  collectors/
    system.py        SystemCollector: CPU, RAM, swap, PSI, đĩa, I/O, nhiệt độ, top tiến trình
    smart.py         read_smart(): sức khỏe SSD qua smartctl -j (tùy chọn)
    kernel_log.py    classify() + KernelLogFollower: lỗi kernel nguy hiểm
  rules.py           Rule engine: ngưỡng + thời gian kéo dài + hysteresis + cooldown
  recorder.py        Hộp đen JSONL (fsync), xoay vòng theo ngày
  postmortem.py      `healthwatch report`: phân tích lần boot trước bị đơ
  notify.py          notify-send + popup GTK3 + âm thanh
  daemon.py          Vòng lặp chính
  cli.py             daemon | status | report | test-alert | selftest
```

## Snapshot (đầu ra của `SystemCollector.sample()`)

Một `dict` JSON-serializable. Trường không đọc được thì để `None`.

```json
{
  "ts": 1732500000.0,
  "cpu": {"percent": 12.5, "iowait_percent": 0.8, "count": 8,
          "load1": 0.9, "load5": 0.7, "load15": 0.5},
  "memory": {"total_mb": 15890, "available_mb": 9000, "used_percent": 43.4,
             "swap_total_mb": 2048, "swap_used_mb": 10, "swap_used_percent": 0.5},
  "pressure": {
    "cpu":    {"some_avg10": 0.0, "some_avg60": 0.0},
    "memory": {"some_avg10": 0.0, "some_avg60": 0.0, "full_avg10": 0.0, "full_avg60": 0.0},
    "io":     {"some_avg10": 0.0, "some_avg60": 0.0, "full_avg10": 0.0, "full_avg60": 0.0}
  },
  "disks": [{"mount": "/", "device": "/dev/nvme0n1p2", "fstype": "ext4",
             "used_percent": 71.2, "free_gb": 40.1, "readonly": false}],
  "io": [{"device": "nvme0n1", "util_percent": 3.1, "read_mb_s": 0.2,
          "write_mb_s": 1.4, "await_ms": 0.6}],
  "thermal": [{"chip": "coretemp", "label": "Package id 0", "temp_c": 52.0,
               "high_c": 80.0, "crit_c": 100.0}],
  "top": {"memory": [{"pid": 1234, "name": "chrome", "rss_mb": 812.3}],
          "cpu":    [{"pid": 1234, "name": "chrome", "cpu_percent": 35.0}]}
}
```

- `pressure` = `null` nếu kernel không có `/proc/pressure` (Ubuntu 20.04 kernel 5.4+ có,
  nhưng có thể bị tắt).
- `cpu.percent`, `iowait_percent`, `io[*]` và `top.cpu` cần 2 lần đo → lần `sample()`
  đầu trả `None` / rỗng cho các trường này.

## Sự kiện kernel (đầu ra của `kernel_log.classify(line)`)

```python
{"category": "disk", "severity": "critical", "summary": "Lỗi I/O trên nvme0n1", "line": "<dòng gốc>"}
```

`category` ∈ `disk | filesystem | memory | hang | gpu | thermal | power | other`;
`severity` ∈ `warning | critical`.

## Bản ghi hộp đen (recorder)

`~/.local/state/pc-health-watch/blackbox-YYYY-MM-DD.jsonl`, mỗi dòng một object:

- `{"type": "sample", "boot_id": "...", ...snapshot}`
- `{"type": "event", "boot_id": "...", "ts": ..., ...kernel event}`
- `{"type": "alert", "boot_id": "...", "ts": ..., "rule": "...", "severity": "...", "title": "...", "body": "..."}`
- `{"type": "start" | "stop", "boot_id": "...", "ts": ...}`

`boot_id` lấy từ `/proc/sys/kernel/random/boot_id`. Một boot có `start` mà không có
`stop` + journal không có dấu hiệu tắt máy bình thường ⇒ nghi bị đơ/reset cứng.
