"""Đọc báo cáo đóng băng do freeze watchdog (root) ghi ở /var/lib/pc-health-watch."""

import json
import os
import time

REPORT_DIR = "/var/lib/pc-health-watch"
WATCHDOG_STATUS = "/run/pc-health-watch/watchdog-status.json"

OUTCOME_TEXT = {
    "action_poweroff": "watchdog đã TẮT MÁY sau khi đơ quá lâu",
    "action_reboot": "watchdog đã KHỞI ĐỘNG LẠI máy sau khi đơ quá lâu",
    "would_act": "lẽ ra watchdog đã tắt máy (đang ở mode=log nên không làm)",
    "recovered": "máy tự hồi phục",
}


def _norm(boot_id):
    return str(boot_id or "").replace("-", "").lower()


def load_freeze_reports(boot_id=None, directory=REPORT_DIR):
    """Các báo cáo (mới nhất sau cùng); lọc theo boot_id nếu có."""
    out = []
    try:
        names = sorted(n for n in os.listdir(directory) if n.startswith("freeze-") and n.endswith(".json"))
    except OSError:
        return out
    for name in names:
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as stream:
                report = json.load(stream)
        except (OSError, ValueError):
            continue
        if not isinstance(report, dict):
            continue
        if boot_id and _norm(report.get("boot_id")) != _norm(boot_id):
            continue
        report["_file"] = name
        out.append(report)
    return out


def watchdog_status(path=WATCHDOG_STATUS, max_age=30):
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or time.time() - data.get("ts", 0) > max_age:
        return None
    return data


def _peak(samples, key):
    values = [s.get(key) for s in samples if isinstance(s.get(key), (int, float))]
    return max(values) if values else None


def format_freeze(report):
    lines = []
    started = report.get("started_ts")
    when = time.strftime("%H:%M:%S %d/%m", time.localtime(started)) if started else "?"
    lines.append(
        "Đóng băng lúc {} — kéo dài {:.0f} s — {}".format(
            when, report.get("duration_seconds") or 0, OUTCOME_TEXT.get(report.get("outcome"), report.get("outcome"))
        )
    )
    reasons = report.get("reason_text") or report.get("reasons") or []
    if reasons:
        lines.append("  Nguyên nhân: " + "; ".join(reasons))
    samples = report.get("samples") or []
    peaks = []
    for key, label in (("memory_full", "RAM nghẽn"), ("io_full", "ổ đĩa nghẽn")):
        value = _peak(samples, key)
        if value is not None:
            peaks.append("{} tới {:.0f}% thời gian".format(label, value))
    avail = [s.get("available_mb") for s in samples if isinstance(s.get("available_mb"), (int, float))]
    if avail:
        peaks.append("RAM trống thấp nhất {} MB".format(min(avail)))
    if peaks:
        lines.append("  Đỉnh: " + ", ".join(peaks))
    procs = report.get("processes") or {}
    if procs.get("top_memory"):
        lines.append(
            "  Ngốn RAM: " + ", ".join("{} {} MB".format(p.get("name"), p.get("rss_mb")) for p in procs["top_memory"][:3])
        )
    if procs.get("d_state_count"):
        names = ", ".join(sorted({p.get("name") for p in procs.get("d_state") or []}))
        lines.append("  {} tiến trình kẹt chờ I/O (trạng thái D): {}".format(procs["d_state_count"], names))
    for action in report.get("actions") or []:
        if action.get("do") in ("oom_kill", "would_oom_kill"):
            lines.append(
                "  Giây {}: {}".format(
                    action.get("t"), "gọi OOM killer" if action["do"] == "oom_kill" else "lẽ ra gọi OOM killer"
                )
            )
    events = report.get("kernel_events") or []
    for event in events[-5:]:
        lines.append("  Kernel: [{}] {}".format(event.get("category"), event.get("summary")))
    return "\n".join(lines)
