"""Phân tích hộp đen sau khi máy Linux bị treo hoặc reset."""

import datetime
import re
from typing import Dict, List, Optional

from healthwatch import util
from healthwatch.recorder import iter_records

_BOOT_PREFIX_RE = re.compile(
    r"^\s*([+-]?\d+)\s+([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\s+(.+?)\s*$"
)
_STAMP_RE = re.compile(r"[A-Za-z]{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?: [A-Za-z0-9+:-]+)?")
_CLEAN_MARKERS = (
    "Reached target Power-Off",
    "Reached target Reboot",
    "Reached target System Reboot",
    "Reached target System Power Off",
    "Reached target Shutdown",
    "systemd-shutdown",
    "Journal stopped",
    # KHÔNG dùng "Stopped target ..." hay "Shutting down": chúng xuất hiện cả khi máy chạy bình thường
    # (rút tai nghe, đăng xuất) và sẽ làm một lần đơ thật bị kết luận nhầm là tắt máy sạch.
)


def _normal_boot_id(value):
    return str(value or "").replace("-", "").lower()


def list_boots(runner=util.run) -> List[Dict]:
    """Đọc danh sách boot từ journalctl, bỏ qua header và dòng lạ."""
    code, output = runner(["journalctl", "--list-boots", "--no-pager"])
    if code != 0:
        return []
    result = []
    for line in output.splitlines():
        match = _BOOT_PREFIX_RE.match(line)
        if not match:
            continue
        try:
            index = int(match.group(1))
        except ValueError:
            continue
        remainder = match.group(3)
        # systemd 245 (Ubuntu 20.04): "Mon 2026-11-23 08:01:02 +07—Mon ..."; bản mới: hai mốc cách nhau dấu cách
        stamps = _STAMP_RE.findall(remainder)
        if len(stamps) >= 2:
            first, last = stamps[0], stamps[1]
        else:
            pieces = re.split(r"—|\s+-\s+", remainder, maxsplit=1)
            if len(pieces) != 2:
                continue
            first = pieces[0].strip()
            last = re.sub(r"\s+\d+\s*$", "", pieces[1]).strip()
        result.append(
            {
                "index": index,
                "boot_id": _normal_boot_id(match.group(2)),
                "first": first,
                "last": last,
            }
        )
    return result


def shutdown_was_clean(journal_text: str) -> Optional[bool]:
    """Suy đoán boot kết thúc sạch từ khoảng 200 dòng journal cuối."""
    lines = [line for line in journal_text.splitlines() if line.strip()][-200:]
    if not lines:
        return None
    tail = "\n".join(lines)
    entry = tail.rfind("PM: suspend entry")
    exit_position = tail.rfind("PM: suspend exit")
    if entry >= 0 and entry > exit_position:
        return False
    return any(marker in tail for marker in _CLEAN_MARKERS)


def load_blackbox(directory, boot_id) -> List[Dict]:
    """Đọc và lọc các JSONL hộp đen; dòng hỏng được bỏ qua."""
    wanted = _normal_boot_id(boot_id)
    if not wanted:
        return []
    records = [record for record in iter_records(directory) if _normal_boot_id(record.get("boot_id")) == wanted]

    def sort_key(item):
        try:
            timestamp = float(item.get("ts", 0) or 0)
        except (TypeError, ValueError):
            timestamp = 0
        return timestamp, str(item.get("type", ""))

    records.sort(key=sort_key)
    return records


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _get(record, *keys):
    value = record
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _last_sample(records):
    samples = [record for record in records if record.get("type") == "sample"]

    def sample_sort_key(item):
        try:
            return float(item.get("ts", 0) or 0)
        except (TypeError, ValueError):
            return 0

    samples.sort(key=sample_sort_key)
    return samples[-1] if samples else None


def _max_value(items, path):
    values = [_number(_get(item, *path)) for item in items]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _disk_peak(items, minimum=False):
    values = []
    for sample in items:
        for disk in sample.get("disks") or []:
            value = _number(disk.get("free_gb")) if minimum else _number(disk.get("used_percent"))
            if value is not None:
                values.append(value)
    if not values:
        return None
    return min(values) if minimum else max(values)


def _thermal_peak(items):
    values = []
    for sample in items:
        for thermal in sample.get("thermal") or []:
            value = _number(thermal.get("temp_c"))
            if value is not None:
                values.append(value)
    return max(values) if values else None


def _io_peak(items):
    values = []
    for sample in items:
        for device in sample.get("io") or []:
            value = _number(device.get("util_percent"))
            if value is not None:
                values.append(value)
    return max(values) if values else None


def _fmt_time(timestamp):
    try:
        return datetime.datetime.fromtimestamp(float(timestamp)).strftime("%H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def analyze(samples_and_records, kernel_events, clean) -> Dict:
    """Tính đỉnh chỉ số và đưa ra các nguyên nhân khả dĩ bằng tiếng Việt."""
    records = list(samples_and_records or [])
    samples = [record for record in records if record.get("type") == "sample"]

    def sample_sort_key(item):
        try:
            return float(item.get("ts", 0) or 0)
        except (TypeError, ValueError):
            return 0

    samples.sort(key=sample_sort_key)
    last = samples[-1] if samples else None
    last_ts = _number(last.get("ts")) if last else None
    recent = [
        sample
        for sample in samples
        if last_ts is not None and _number(sample.get("ts")) is not None and last_ts - float(sample.get("ts")) <= 600
    ]
    peaks = {
        "memory.used_percent": _max_value(recent, ("memory", "used_percent")),
        "memory.swap_used_percent": _max_value(recent, ("memory", "swap_used_percent")),
        "pressure.memory.full_avg10": _max_value(recent, ("pressure", "memory", "full_avg10")),
        "pressure.io.full_avg10": _max_value(recent, ("pressure", "io", "full_avg10")),
        "pressure.cpu.some_avg10": _max_value(recent, ("pressure", "cpu", "some_avg10")),
        "cpu.percent": _max_value(recent, ("cpu", "percent")),
        "cpu.iowait_percent": _max_value(recent, ("cpu", "iowait_percent")),
        "thermal.max_c": _thermal_peak(recent),
        "io.util_percent": _io_peak(recent),
        "disks.min_free_gb": _disk_peak(recent, minimum=True),
    }
    findings = []
    ram = peaks["memory.used_percent"] is not None and peaks["memory.used_percent"] >= 95
    ram = ram or (peaks["pressure.memory.full_avg10"] is not None and peaks["pressure.memory.full_avg10"] >= 10)
    ram = ram or (peaks["memory.swap_used_percent"] is not None and peaks["memory.swap_used_percent"] >= 80)
    if ram:
        text = "Hết RAM / swap: máy bị thrashing, có thể dẫn tới đơ."
        top = sorted(
            _get(last or {}, "top", "memory") or [], key=lambda item: _number(item.get("rss_mb")) or 0, reverse=True
        )
        if top:
            process = top[0]
            text += " Tiến trình dùng RAM nhiều nhất: %s (%s MB)." % (
                process.get("name", "?"),
                process.get("rss_mb", "?"),
            )
        findings.append({"severity": "critical", "text": text})
    io_bad = peaks["pressure.io.full_avg10"] is not None and peaks["pressure.io.full_avg10"] >= 20
    io_bad = io_bad or (peaks["cpu.iowait_percent"] is not None and peaks["cpu.iowait_percent"] >= 30)
    io_bad = io_bad or (peaks["io.util_percent"] is not None and peaks["io.util_percent"] >= 95)
    if io_bad:
        findings.append({"severity": "warning", "text": "Nghi SSD/ổ đĩa chậm, treo I/O hoặc hàng đợi I/O quá dài."})
    if peaks["thermal.max_c"] is not None and peaks["thermal.max_c"] >= 90:
        findings.append({"severity": "critical", "text": "Quá nhiệt: nhiệt độ cao nhất đạt từ 90°C."})
    # phân vùng nhỏ (/boot) luôn ít GB trống: chỉ coi là đầy khi đã dùng >= 90% và còn < 1 GB, hoặc >= 98%
    disk_full = False
    readonly_mounts = set()
    for sample in recent:
        for disk in sample.get("disks") or []:
            used, free = _number(disk.get("used_percent")), _number(disk.get("free_gb"))
            if used is not None and (used >= 98 or (used >= 90 and free is not None and free < 1)):
                disk_full = True
            if disk.get("readonly") is True and disk.get("mount") in ("/", "/home"):
                readonly_mounts.add(disk.get("mount"))
    if readonly_mounts:
        findings.append(
            {
                "severity": "critical",
                "text": "Phân vùng %s bị remount read-only (kernel phát hiện lỗi đĩa/filesystem)."
                % ", ".join(sorted(readonly_mounts)),
            }
        )
    if disk_full:
        findings.append({"severity": "warning", "text": "Đĩa gần đầy, có thể làm hệ thống hoạt động bất thường."})
    by_category = {}
    for event in kernel_events or []:
        by_category.setdefault(str(event.get("category", "")).lower(), []).append(event)

    def examples(*names):
        found = []
        for name in names:
            for event in by_category.get(name, []):
                summary = event.get("summary") or ""
                if summary and summary not in found:
                    found.append(summary)
        return (" Ví dụ: " + "; ".join(found[:3]) + ".") if found else ""

    kernel_rules = (
        (
            ("disk", "filesystem"),
            "critical",
            "Kernel báo lỗi ổ đĩa / filesystem — nghi SSD, cáp/khe cắm hoặc firmware.",
        ),
        (("hang",), "critical", "Kernel báo hung task / soft lockup — hệ thống bị treo ở mức kernel."),
        (("gpu",), "critical", "Driver GPU lỗi (thường gặp với NVIDIA/AMD trên Ubuntu 20.04)."),
        (("memory",), "critical", "Kernel phải chạy OOM killer — đã hết RAM."),
        (("thermal",), "critical", "Kernel ghi nhận quá nhiệt / CPU bị hạ xung."),
        (("power",), "warning", "Kernel ghi nhận lỗi phần cứng (PCIe/MCE) — nghi nguồn, RAM hoặc mainboard."),
        (("smart",), "critical", "SMART báo SSD có dấu hiệu hỏng."),
    )
    for names, severity, text in kernel_rules:
        if any(name in by_category for name in names):
            findings.append({"severity": severity, "text": text + examples(*names)})
    if not findings and clean is False:
        findings.append(
            {
                "severity": "warning",
                "text": "Máy đơ đột ngột khi tải bình thường: nghi driver đồ họa / phần cứng (RAM, nguồn) / firmware SSD; xem docs/TROUBLESHOOTING.md",
            }
        )
    gap_note = ""
    if clean is False and last_ts is not None:
        gap_note = "Bản ghi cuối lúc %s" % _fmt_time(last_ts)
    return {
        "clean": clean,
        "last_sample_ts": last_ts,
        "gap_note": gap_note,
        "peaks": {key: value for key, value in peaks.items() if value is not None},
        "findings": findings,
        "last_samples": samples[-3:],
        "alerts": [record for record in records if record.get("type") == "alert"][-10:],
        "kernel_events": list(kernel_events or [])[-20:],
    }


def kernel_events_for_boot(index, runner=util.run, classify=None) -> List[Dict]:
    """Đọc và phân loại các dòng kernel của một boot."""
    if classify is None:
        try:
            from healthwatch.collectors.kernel_log import classify as imported_classify

            classify = imported_classify
        except ImportError:
            return []
    code, output = runner(["journalctl", "-k", "-b", str(index), "--no-pager", "-o", "short-iso"])
    if code != 0:
        return []
    events = []
    for line in output.splitlines():
        try:
            event = classify(line)
        except Exception:
            event = None
        if event:
            events.append(event)
    return events


def _journal_last_time(journal):
    """Lấy timestamp ISO ở đầu dòng journal cuối cùng nếu có."""
    for line in reversed(journal.splitlines()):
        match = re.match(r"^(\S+)", line.strip())
        if match and "T" in match.group(1):
            return match.group(1)
    return None


def build_report(boot_index=-1, state_directory=None, runner=util.run, classify=None) -> Dict:
    """Ghép journal, hộp đen và sự kiện kernel thành báo cáo."""
    code, journal = runner(["journalctl", "-b", str(boot_index), "--no-pager", "-o", "short-iso", "-n", "300"])
    if code != 0:
        journal = ""
    boots = list_boots(runner)
    selected = next((boot for boot in boots if boot["index"] == boot_index), None)
    boot_id = selected.get("boot_id") if selected else ""
    records = load_blackbox(state_directory or util.state_dir(), boot_id) if boot_id else []
    events = kernel_events_for_boot(boot_index, runner, classify)
    # sự kiện daemon đã ghi (gồm cả SMART) — bổ sung khi journal không đọc được
    seen = {(e.get("category"), e.get("summary")) for e in events}
    for record in records:
        if record.get("type") == "event" and (record.get("category"), record.get("summary")) not in seen:
            events.append(record)
            seen.add((record.get("category"), record.get("summary")))
    current = boot_index == 0 or (selected is not None and boot_id == _normal_boot_id(util.boot_id()))
    # boot đang chạy thì chưa "tắt" — không được kết luận là đơ
    report = analyze(records, events, None if current else shutdown_was_clean(journal))
    journal_time = _journal_last_time(journal)
    if report.get("gap_note") and journal_time:
        report["gap_note"] += ", journal cuối lúc %s" % journal_time
    report["current"] = current
    if report.get("clean") is None and records and not current:
        # journal không đọc được: daemon ghi "stop" khi dừng bình thường (tắt máy) → dùng làm dấu hiệu
        report["clean"] = any(record.get("type") == "stop" for record in records[-50:])
    report["boot_index"] = boot_index
    report["boot_id"] = boot_id
    report["boot"] = selected
    report["blackbox_records"] = len(records)
    try:
        from .freeze_reports import load_freeze_reports

        report["freezes"] = load_freeze_reports(boot_id) if boot_id else []
    except Exception:
        report["freezes"] = []
    for freeze in report["freezes"]:
        reasons = "; ".join(freeze.get("reason_text") or [])
        text = "Freeze watchdog xác nhận máy ĐƠ {:.0f} s ({}).".format(freeze.get("duration_seconds") or 0, reasons)
        report.setdefault("findings", []).insert(0, {"severity": "critical", "text": text})
    if report["freezes"] and report.get("clean") is not True:
        report["clean"] = False
    return report


def format_report(report: Dict, color=False) -> str:
    """Định dạng báo cáo ngắn gọn, phù hợp terminal."""
    boot = report.get("boot") or {}
    lines = ["BÁO CÁO LẦN KHỞI ĐỘNG %s" % report.get("boot_index", "?"), "=" * 40]
    if boot:
        lines.append("Boot %s: %s → %s" % (boot.get("boot_id", "")[:12], boot.get("first", "?"), boot.get("last", "?")))
    clean = report.get("clean")
    if report.get("current"):
        lines.append("Kết luận: đây là lần khởi động hiện tại (máy đang chạy)")
    else:
        lines.append(
            "Kết luận: %s"
            % ("tắt máy sạch" if clean is True else "không thấy tắt máy sạch" if clean is False else "không đủ dữ liệu")
        )
    if report.get("gap_note"):
        lines.append(report["gap_note"])
    if not report.get("blackbox_records"):
        lines.append("(Không có dữ liệu hộp đen cho boot này — daemon chưa chạy lúc đó; chỉ dựa vào journal.)")
    if report.get("freezes"):
        from .freeze_reports import format_freeze

        lines.append("\nBáo cáo của freeze watchdog:")
        for freeze in report["freezes"]:
            lines.append(format_freeze(freeze))
    lines.append("\nNguyên nhân khả dĩ:")
    symbols = {"critical": "[!!]", "warning": "[! ]", "info": "[i]"}
    if not report.get("findings"):
        lines.append("[i] Không thấy dấu hiệu bất thường.")
    for finding in report.get("findings", []):
        lines.append("%s %s" % (symbols.get(finding.get("severity"), "[i]"), finding.get("text", "")))
    if report.get("peaks"):
        lines.append("\nĐỉnh 10 phút cuối:")
    for key, value in report.get("peaks", {}).items():
        lines.append("  %-32s %s" % (key, value))
    if report.get("last_samples"):
        lines.append("\n3 sample cuối:")
    for sample in report.get("last_samples", []):
        lines.append(
            "  %s  RAM=%s%% CPU=%s%%"
            % (_fmt_time(sample.get("ts")), _get(sample, "memory", "used_percent"), _get(sample, "cpu", "percent"))
        )
        for process in (_get(sample, "top", "memory") or [])[:1]:
            lines.append("    top RAM: %s (%s MB)" % (process.get("name", "?"), process.get("rss_mb", "?")))
    if report.get("kernel_events"):
        lines.append("\nSự kiện kernel:")
    for event in report.get("kernel_events", []):
        lines.append("  [%s] %s" % (event.get("category", "?"), event.get("summary", event.get("line", ""))))
    if report.get("alerts"):
        lines.append("\nCảnh báo cuối:")
        for alert in report["alerts"]:
            lines.append("  %s: %s" % (alert.get("severity", "?"), alert.get("title", alert.get("rule", ""))))
    result = "\n".join(lines)
    if color:
        result = result.replace("[!!]", "\033[31m[!!]\033[0m").replace("[! ]", "\033[33m[! ]\033[0m")
    return result
