"""Lịch ghi thích ứng và rút gọn snapshot hộp đen."""

import copy


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_elevated(snapshot, cfg) -> bool:
    """Trả True khi snapshot có dấu hiệu máy đang chịu tải bất thường."""
    snapshot = snapshot or {}
    memory = snapshot.get("memory") or {}
    cpu = snapshot.get("cpu") or {}
    pressure = snapshot.get("pressure") or {}
    elevated = (cfg.get("recording") or {}).get("elevated") or {}
    checks = (
        (memory.get("used_percent"), elevated.get("memory_percent", 80)),
        (memory.get("swap_used_percent"), 50),
        ((pressure.get("memory") or {}).get("some_avg10"), 5),
        ((pressure.get("io") or {}).get("some_avg10"), 20),
        (cpu.get("iowait_percent"), 20),
    )
    if any(_number(value) and value >= threshold for value, threshold in checks):
        return True
    temperatures = [item.get("temp_c") for item in snapshot.get("thermal") or []]
    # 85°C: nhiều laptop chạy không tải đã ~80°C ở vài cảm biến, đừng ghi dày cả ngày vì thế
    if any(_number(value) and value >= elevated.get("temp_c", 85) for value in temperatures):
        return True
    # cảnh báo dung lượng có thể kéo dài nhiều ngày — không phải lý do để ghi dày
    lasting = ("disk_free_gb",)
    return any(alert.get("rule") not in lasting for alert in snapshot.get("active_alerts") or [] if isinstance(alert, dict))


def _copy_fields(source, fields):
    return {field: copy.deepcopy(source.get(field)) for field in fields if field in source}


def compact(snapshot, full: bool = False) -> dict:
    """Tạo bản ghi nhỏ nhưng vẫn giữ các trường cần cho postmortem."""
    snapshot = snapshot or {}
    result = {field: copy.deepcopy(snapshot.get(field)) for field in ("ts", "cpu", "memory", "pressure")}
    result["disks"] = [
        _copy_fields(disk, ("mount", "free_gb", "used_percent", "readonly"))
        for disk in snapshot.get("disks") or []
    ]
    io_items = snapshot.get("io") or []
    kept_io = []
    for item in io_items:
        util = item.get("util_percent")
        read = item.get("read_mb_s")
        write = item.get("write_mb_s")
        if (not _number(util) or util != 0) or (not _number(read) or read != 0) or (not _number(write) or write != 0):
            kept_io.append(copy.deepcopy(item))
    result["io"] = kept_io

    thermal = [item for item in snapshot.get("thermal") or [] if _number(item.get("temp_c"))]
    thermal.sort(key=lambda item: item.get("temp_c"), reverse=True)
    result["thermal"] = [
        {"label": "{} {}".format(item.get("chip") or "", item.get("label") or "").strip(), "temp_c": item.get("temp_c")}
        for item in thermal[:3]
    ]

    top = snapshot.get("top") or {}
    memory_top = [item for item in top.get("memory") or [] if isinstance(item, dict)]
    cpu_top = [item for item in top.get("cpu") or [] if isinstance(item, dict)]
    cpu_top = [item for item in cpu_top if not _number(item.get("cpu_percent")) or item.get("cpu_percent") >= 1]
    result["top"] = {
        "memory": copy.deepcopy(memory_top[:5 if full else 3]),
        "cpu": copy.deepcopy(cpu_top[:5 if full else 3]),
    }
    return result


class AdaptiveSchedule:
    def __init__(self, cfg):
        recording = cfg.get("recording") or {}
        self.normal_seconds = recording.get("normal_seconds", 30)
        self.elevated_seconds = max(recording.get("elevated_seconds", 2), cfg.get("interval_seconds", 0))
        self.cooldown_seconds = recording.get("cooldown_seconds", 120)
        self.last_record = float("-inf")
        self.last_elevated = False
        self.cooldown_until = float("-inf")

    def due(self, now, elevated) -> bool:
        changed_to_elevated = bool(elevated) and not self.last_elevated
        if elevated and not self.last_elevated:
            self.cooldown_until = float("inf")
        elif not elevated and self.last_elevated:
            self.cooldown_until = now + self.cooldown_seconds
        self.last_elevated = bool(elevated)
        if elevated or now < self.cooldown_until:
            interval = self.elevated_seconds
        else:
            interval = self.normal_seconds
        if changed_to_elevated or now - self.last_record >= interval:
            self.last_record = now
            if elevated:
                self.cooldown_until = float("inf")
            return True
        return False
