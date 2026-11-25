"""Bộ luật cảnh báo: ngưỡng warning/critical + thời gian kéo dài + hysteresis + nhắc lại.

Mỗi luật rút từ snapshot ra một hoặc nhiều "phép đo" (key, value, ngữ cảnh), ví dụ một phép đo
cho mỗi phân vùng. Mỗi key có trạng thái riêng nên phân vùng này đầy không che phân vùng khác.
"""

LEVELS = {"ok": 0, "warning": 1, "critical": 2}


class Alert(object):
    __slots__ = ("rule", "key", "severity", "title", "body", "value", "recovered")

    def __init__(self, rule, key, severity, title, body, value=None, recovered=False):
        self.rule, self.key, self.severity = rule, key, severity
        self.title, self.body, self.value, self.recovered = title, body, value, recovered

    def to_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}

    def __repr__(self):
        return "Alert({}/{} {} {!r})".format(self.rule, self.key, self.severity, self.title)


class _KeyState(object):
    __slots__ = ("level", "candidate", "since", "last_alert")

    def __init__(self):
        self.level = "ok"  # mức đã báo
        self.candidate = "ok"  # mức đang quan sát, chờ đủ for_seconds
        self.since = None
        self.last_alert = None


class ThresholdRule(object):
    """Luật ngưỡng chung.

    measure(snapshot) -> list[(key, value, ctx)]; describe(level, value, ctx) -> (title, body).
    higher_is_worse=False cho các đại lượng "càng thấp càng tệ" (dung lượng trống).
    """

    def __init__(self, name, measure, describe, higher_is_worse=True, hysteresis=0.0):
        self.name = name
        self.measure = measure
        self.describe = describe
        self.higher_is_worse = higher_is_worse
        self.hysteresis = hysteresis
        self.states = {}

    def _level_of(self, value, warning, critical, current):
        # hysteresis: đang ở một mức thì phải tốt hơn ngưỡng một khoảng mới hạ mức (chống nhấp nháy)
        def worse(threshold, margin):
            if self.higher_is_worse:
                return value >= threshold - margin
            return value <= threshold + margin

        crit_margin = self.hysteresis if LEVELS[current] >= 2 else 0
        warn_margin = self.hysteresis if LEVELS[current] >= 1 else 0
        if worse(critical, crit_margin):
            return "critical"
        if worse(warning, warn_margin):
            return "warning"
        return "ok"

    def evaluate(self, snapshot, settings, now, repeat_seconds, report_recovery=True):
        if not settings.get("enabled", True):
            self.states.clear()
            return []
        warning, critical = settings["warning"], settings["critical"]
        hold = settings.get("for_seconds", 0)
        alerts = []
        seen = set()
        for key, value, ctx in self.measure(snapshot) or []:
            if value is None:
                continue
            seen.add(key)
            state = self.states.setdefault(key, _KeyState())
            level = self._level_of(value, warning, critical, state.level)
            if state.since is None or level != state.candidate:
                state.candidate, state.since = level, now
            held = now - state.since >= hold
            if level == state.level:
                # đang giữ mức critical: nhắc lại định kỳ
                if (
                    level == "critical"
                    and repeat_seconds
                    and state.last_alert is not None
                    and now - state.last_alert >= repeat_seconds
                ):
                    alerts.append(self._alert(key, level, value, ctx))
                    state.last_alert = now
                continue
            if LEVELS[level] > LEVELS[state.level]:
                if held:
                    state.level, state.last_alert = level, now
                    alerts.append(self._alert(key, level, value, ctx))
            else:
                # hạ mức: cần ổn định đủ lâu như khi tăng, để không báo "đã ổn" rồi lại báo
                if held:
                    was = state.level
                    state.level = level
                    if level == "ok" and report_recovery and was != "ok":
                        title, body = self.describe("ok", value, ctx)
                        alerts.append(Alert(self.name, key, "info", title, body, value, recovered=True))
        for key in list(self.states):
            if key not in seen:
                del self.states[key]
        return alerts

    def _alert(self, key, level, value, ctx):
        title, body = self.describe(level, value, ctx)
        return Alert(self.name, key, level, title, body, value)


# ---------------------------------------------------------------- các luật cụ thể


def _get(snapshot, *path):
    node = snapshot
    for part in path:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _top_memory(snapshot, count=3):
    items = _get(snapshot, "top", "memory") or []
    parts = ["{} ({:.0f} MB)".format(p.get("name"), p.get("rss_mb") or 0) for p in items[:count]]
    return ", ".join(parts)


def _top_cpu(snapshot, count=3):
    items = _get(snapshot, "top", "cpu") or []
    return ", ".join("{} ({:.0f}%)".format(p.get("name"), p.get("cpu_percent") or 0) for p in items[:count])


def _ok_text(label):
    return "{} đã trở lại bình thường".format(label), "Không cần làm gì thêm."


def _memory_used(snapshot):
    return [("ram", _get(snapshot, "memory", "used_percent"), snapshot)]


def _describe_memory(level, value, snapshot):
    if level == "ok":
        return _ok_text("RAM")
    avail = _get(snapshot, "memory", "available_mb")
    body = "Còn trống {} MB. Ngốn nhiều nhất: {}.".format(
        int(avail) if avail is not None else "?", _top_memory(snapshot) or "?"
    )
    if level == "critical":
        body += " Đóng bớt ứng dụng ngay, máy sắp đơ."
    return "RAM đã dùng {:.0f}%".format(value), body


def _swap_used(snapshot):
    if not _get(snapshot, "memory", "swap_total_mb"):
        return []
    return [("swap", _get(snapshot, "memory", "swap_used_percent"), snapshot)]


def _describe_swap(level, value, snapshot):
    if level == "ok":
        return _ok_text("Swap")
    return (
        "Swap đã dùng {:.0f}%".format(value),
        "Hệ thống đang đẩy RAM xuống đĩa, máy sẽ chậm dần. Ngốn RAM nhất: {}.".format(_top_memory(snapshot) or "?"),
    )


def _memory_pressure(snapshot):
    return [("psi-mem", _get(snapshot, "pressure", "memory", "full_avg10"), snapshot)]


def _describe_memory_pressure(level, value, snapshot):
    if level == "ok":
        return _ok_text("Áp lực bộ nhớ")
    return (
        "Nghẽn bộ nhớ {:.0f}% thời gian".format(value),
        "Các tiến trình đang phải chờ RAM (thrashing), giao diện sắp đứng. Ngốn RAM nhất: {}.".format(
            _top_memory(snapshot) or "?"
        ),
    )


def _io_pressure(snapshot):
    return [("psi-io", _get(snapshot, "pressure", "io", "full_avg10"), snapshot)]


def _describe_io_pressure(level, value, snapshot):
    if level == "ok":
        return _ok_text("Áp lực ổ đĩa")
    busiest = max(_get(snapshot, "io") or [{}], key=lambda d: d.get("util_percent") or 0)
    return (
        "Nghẽn ổ đĩa {:.0f}% thời gian".format(value),
        "Ổ {} bận {}%. Có thể do swap, chép file lớn hoặc SSD có vấn đề.".format(
            busiest.get("device", "?"), busiest.get("util_percent", "?")
        ),
    )


def _io_latency(snapshot):
    return [
        ("await-" + str(d.get("device")), d.get("await_ms"), d)
        for d in _get(snapshot, "io") or []
        # bỏ qua khi gần như không có I/O: 1 lệnh chậm lẻ không có ý nghĩa
        if (d.get("util_percent") or 0) >= 5
    ]


def _describe_io_latency(level, value, disk):
    if level == "ok":
        return _ok_text("Độ trễ ổ " + str(disk.get("device")))
    return (
        "Ổ {} phản hồi chậm ({:.0f} ms/lệnh)".format(disk.get("device"), value),
        "SSD khỏe thường dưới 20 ms. Trễ cao kéo dài: nghi SSD lỗi, firmware/APST hoặc ổ quá đầy.",
    )


def _temperature(snapshot):
    sensors = [t for t in _get(snapshot, "thermal") or [] if t.get("temp_c") is not None]
    if not sensors:
        return []
    hottest = max(sensors, key=lambda t: t["temp_c"])
    return [("temp", hottest["temp_c"], dict(hottest, snapshot=snapshot))]


def _describe_temperature(level, value, sensor):
    if level == "ok":
        return _ok_text("Nhiệt độ")
    name = "{} {}".format(sensor.get("chip") or "", sensor.get("label") or "").strip()
    return (
        "Nhiệt độ cao: {:.0f}°C".format(value),
        "Cảm biến {}. Ngốn CPU nhất: {}. Kiểm tra quạt/khe tản nhiệt.".format(
            name or "?", _top_cpu(sensor.get("snapshot") or {}) or "?"
        ),
    )


def _disk_free(snapshot):
    return [("free-" + str(d.get("mount")), d.get("free_gb"), d) for d in _get(snapshot, "disks") or []]


def _describe_disk_free(level, value, disk):
    if level == "ok":
        return _ok_text("Dung lượng " + str(disk.get("mount")))
    return (
        "Phân vùng {} sắp đầy (còn {:.1f} GB)".format(disk.get("mount"), value),
        "Đã dùng {}%. Ổ đầy làm hệ thống treo khi ghi. Dọn: `sudo apt clean`, `journalctl --vacuum-size=200M`.".format(
            disk.get("used_percent", "?")
        ),
    )


def _disk_readonly(snapshot):
    return [
        ("ro-" + str(d.get("mount")), 1 if d.get("readonly") else 0, d)
        for d in _get(snapshot, "disks") or []
        if d.get("mount") in ("/", "/home", "/var", "/boot", "/boot/efi") or str(d.get("mount", "")).startswith("/home")
    ]


def _describe_readonly(level, value, disk):
    if level == "ok":
        return _ok_text("Phân vùng " + str(disk.get("mount")))
    return (
        "Phân vùng {} bị chuyển sang chỉ-đọc".format(disk.get("mount")),
        "Kernel thường làm vậy khi phát hiện lỗi đĩa/filesystem. Sao lưu dữ liệu, xem `journalctl -k`,"
        " chạy fsck từ live USB.",
    )


def build_rules():
    """Trả dict tên luật -> ThresholdRule. Tên khớp khóa trong config['rules']."""
    return {
        "memory_used": ThresholdRule("memory_used", _memory_used, _describe_memory, hysteresis=3),
        "swap_used": ThresholdRule("swap_used", _swap_used, _describe_swap, hysteresis=5),
        "memory_pressure": ThresholdRule(
            "memory_pressure", _memory_pressure, _describe_memory_pressure, hysteresis=5
        ),
        "io_pressure": ThresholdRule("io_pressure", _io_pressure, _describe_io_pressure, hysteresis=10),
        "io_latency": ThresholdRule("io_latency", _io_latency, _describe_io_latency, hysteresis=100),
        "temperature": ThresholdRule("temperature", _temperature, _describe_temperature, hysteresis=4),
        "disk_free_gb": ThresholdRule(
            "disk_free_gb", _disk_free, _describe_disk_free, higher_is_worse=False, hysteresis=0.5
        ),
        "disk_readonly": ThresholdRule("disk_readonly", _disk_readonly, _describe_readonly),
    }


# disk_readonly không có ngưỡng trong config: 1 = critical ngay
FIXED_SETTINGS = {"disk_readonly": {"warning": 1, "critical": 1, "for_seconds": 0}}


class RuleEngine(object):
    def __init__(self, config):
        self.rules = build_rules()
        self.config = config

    def evaluate(self, snapshot, now):
        notify = self.config["notify"]
        repeat = notify.get("repeat_minutes", 10) * 60
        alerts = []
        for name, rule in self.rules.items():
            settings = dict(self.config["rules"].get(name, {}))
            settings.update(FIXED_SETTINGS.get(name, {}))
            if "warning" not in settings:
                continue
            alerts.extend(rule.evaluate(snapshot, settings, now, repeat, notify.get("recovered", True)))
        return alerts
