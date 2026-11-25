"""Cấu hình mặc định + nạp từ ~/.config/pc-health-watch/config.json (ghi đè từng phần)."""

import copy
import json
import os

from . import util

# Ngưỡng chọn cho máy để bàn/laptop phổ thông. warning/critical theo đơn vị của từng luật;
# for_seconds = giá trị phải vượt ngưỡng liên tục bấy lâu mới báo (tránh báo vì đỉnh nhọn).
DEFAULTS = {
    "interval_seconds": 5,
    "record_every_seconds": 10,
    "retention_days": 14,
    "rules": {
        # % RAM đã dùng (theo MemAvailable)
        "memory_used": {"enabled": True, "warning": 90, "critical": 96, "for_seconds": 20},
        # % swap đã dùng
        "swap_used": {"enabled": True, "warning": 70, "critical": 90, "for_seconds": 30},
        # PSI memory full avg10 (% thời gian MỌI tác vụ bị nghẽn vì RAM) — dấu hiệu sắp đơ rõ nhất
        "memory_pressure": {"enabled": True, "warning": 10, "critical": 30, "for_seconds": 10},
        # PSI io full avg10 — ổ đĩa nghẽn, giao diện sẽ giật/đứng
        "io_pressure": {"enabled": True, "warning": 30, "critical": 60, "for_seconds": 20},
        # Thời gian chờ trung bình mỗi lệnh I/O (ms) — SSD khỏe thường < 20 ms
        "io_latency": {"enabled": True, "warning": 300, "critical": 1500, "for_seconds": 15},
        # °C của cảm biến nóng nhất
        "temperature": {"enabled": True, "warning": 88, "critical": 97, "for_seconds": 15},
        # GB trống tối thiểu trên mỗi phân vùng (thấp là xấu)
        "disk_free_gb": {"enabled": True, "warning": 5, "critical": 1, "for_seconds": 0},
        # Phân vùng hệ thống bị remount read-only (thường do lỗi đĩa)
        "disk_readonly": {"enabled": True},
        # Lỗi kernel nguy hiểm trong journal (I/O error, OOM, hung task, GPU...)
        "kernel_log": {"enabled": True, "min_severity": "warning"},
        # Sức khỏe SSD qua smartctl (cần sudo không mật khẩu cho smartctl — xem README)
        "smart": {"enabled": True, "every_minutes": 60},
    },
    "notify": {
        "desktop": True,  # notify-send
        "popup": True,  # cửa sổ GTK giữa màn hình cho mức critical
        "sound": True,  # tiếng bíp khi critical
        "repeat_minutes": 10,  # nhắc lại cảnh báo critical còn kéo dài
        "recovered": True,  # báo khi đã trở lại bình thường
    },
    # Hành động cứu máy. TẮT mặc định — bật nếu bạn chấp nhận tool tự đóng ứng dụng.
    "actions": {
        "relieve_memory": {
            "enabled": False,
            "pressure_full_avg10": 40,  # PSI memory full avg10 vượt mức này...
            "for_seconds": 15,  # ...liên tục bấy lâu thì gửi SIGTERM tiến trình ngốn RAM nhất của bạn
            "protect": ["gnome-shell", "Xorg", "Xwayland", "systemd", "pulseaudio", "pipewire", "sshd"],
        }
    },
}


def _merge(base, override):
    """Ghi đè đệ quy; chỉ nhận khóa có trong mặc định và cùng kiểu (số nguyên/thực coi như nhau)."""
    for key, value in override.items():
        if key not in base:
            continue
        current = base[key]
        if isinstance(current, dict):
            if isinstance(value, dict):
                _merge(current, value)
        elif isinstance(current, bool):
            if isinstance(value, bool):
                base[key] = value
        elif isinstance(current, (int, float)):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                base[key] = value
        elif isinstance(current, list):
            if isinstance(value, list) and all(isinstance(x, str) for x in value):
                base[key] = value
        elif isinstance(current, str):
            if isinstance(value, str):
                base[key] = value
    return base


def default_path():
    return os.environ.get("HEALTHWATCH_CONFIG") or os.path.join(util.config_dir(), "config.json")


def load_config(path=None):
    """Trả (config, lỗi_hoặc_None). File thiếu = dùng mặc định, file hỏng = mặc định + báo lỗi."""
    config = copy.deepcopy(DEFAULTS)
    path = path or default_path()
    try:
        with open(path, encoding="utf-8") as stream:
            raw = json.load(stream)
    except FileNotFoundError:
        return config, None
    except (OSError, ValueError) as error:
        return config, "Không đọc được {}: {}".format(path, error)
    if not isinstance(raw, dict):
        return config, "{} phải là một object JSON".format(path)
    return _merge(config, raw), None
