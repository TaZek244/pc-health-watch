"""Giao diện dòng lệnh: healthwatch <lệnh>."""

import argparse
import json
import os
import shutil
import sys
import time

from . import __version__, util
from . import config as config_mod

USAGE = """healthwatch {daemon,status,history,report,check,test-alert,config}

  daemon        chạy theo dõi liên tục (thường do systemd --user chạy)
  status        tình trạng máy hiện tại
  history       số liệu hộp đen N phút gần nhất
  report        phân tích lần khởi động trước (sau khi máy đơ phải reset)
  check         kiểm tra môi trường: journal, quyền, SysRq, earlyoom, smartctl...
  test-alert    gửi thử cảnh báo
  config        in cấu hình đang dùng / mặc định
"""


def _fmt(value, unit="", digits=0):
    if value is None:
        return "—"
    return "{:.{d}f}{}".format(value, unit, d=digits)


def _bar(percent, width=20):
    if percent is None:
        return " " * width
    filled = int(round(max(0, min(100, percent)) / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def _daemon_status():
    from .daemon import status_path

    try:
        with open(status_path(), encoding="utf-8") as stream:
            data = json.load(stream)
        os.kill(int(data["pid"]), 0)
        if time.time() - (data.get("snapshot") or {}).get("ts", 0) > 120:
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def format_snapshot(snap, active=None):
    lines = []
    cpu, mem = snap.get("cpu") or {}, snap.get("memory") or {}
    psi = snap.get("pressure") or {}
    lines.append(
        "CPU   {} {:>6}  iowait {}  load {} / {} / {}  ({} lõi)".format(
            _bar(cpu.get("percent")),
            _fmt(cpu.get("percent"), "%", 1),
            _fmt(cpu.get("iowait_percent"), "%", 1),
            _fmt(cpu.get("load1"), "", 2),
            _fmt(cpu.get("load5"), "", 2),
            _fmt(cpu.get("load15"), "", 2),
            cpu.get("count") or "?",
        )
    )
    lines.append(
        "RAM   {} {:>6}  còn trống {} MB / {} MB".format(
            _bar(mem.get("used_percent")),
            _fmt(mem.get("used_percent"), "%", 1),
            _fmt(mem.get("available_mb")),
            _fmt(mem.get("total_mb")),
        )
    )
    if mem.get("swap_total_mb"):
        lines.append(
            "Swap  {} {:>6}  {} / {} MB".format(
                _bar(mem.get("swap_used_percent")),
                _fmt(mem.get("swap_used_percent"), "%", 1),
                _fmt(mem.get("swap_used_mb")),
                _fmt(mem.get("swap_total_mb")),
            )
        )
    else:
        lines.append("Swap  không có swap — hết RAM là đơ ngay, nên tạo swap/zram")
    if psi:
        lines.append(
            "PSI   cpu some {}  mem some {} full {}  io some {} full {}  (avg10, % thời gian bị nghẽn)".format(
                _fmt((psi.get("cpu") or {}).get("some_avg10"), "%", 1),
                _fmt((psi.get("memory") or {}).get("some_avg10"), "%", 1),
                _fmt((psi.get("memory") or {}).get("full_avg10"), "%", 1),
                _fmt((psi.get("io") or {}).get("some_avg10"), "%", 1),
                _fmt((psi.get("io") or {}).get("full_avg10"), "%", 1),
            )
        )
    else:
        lines.append("PSI   kernel không hỗ trợ /proc/pressure (thêm psi=1 vào tham số kernel)")
    for disk in snap.get("disks") or []:
        lines.append(
            "Đĩa  {:<14} {} {:>6}  trống {} GB  {}{}".format(
                str(disk.get("mount"))[:14],
                _bar(disk.get("used_percent"), 12),
                _fmt(disk.get("used_percent"), "%", 1),
                _fmt(disk.get("free_gb"), "", 1),
                disk.get("device") or "",
                "  [CHỈ-ĐỌC!]" if disk.get("readonly") else "",
            )
        )
    for io in snap.get("io") or []:
        lines.append(
            "I/O  {:<14} bận {:>6}  đọc {} MB/s  ghi {} MB/s  trễ {} ms".format(
                io.get("device"),
                _fmt(io.get("util_percent"), "%", 1),
                _fmt(io.get("read_mb_s"), "", 1),
                _fmt(io.get("write_mb_s"), "", 1),
                _fmt(io.get("await_ms"), "", 1),
            )
        )
    temps = sorted((t for t in snap.get("thermal") or [] if t.get("temp_c") is not None), key=lambda t: -t["temp_c"])[
        :4
    ]
    if temps:
        lines.append(
            "Nhiệt "
            + "  ".join(
                "{} {}°C".format(("{} {}".format(t.get("chip") or "", t.get("label") or "")).strip(), _fmt(t["temp_c"]))
                for t in temps
            )
        )
    top = snap.get("top") or {}
    if top.get("memory"):
        lines.append(
            "Top RAM  " + ", ".join("{} {} MB".format(p.get("name"), _fmt(p.get("rss_mb"))) for p in top["memory"][:5])
        )
    if top.get("cpu"):
        lines.append(
            "Top CPU  " + ", ".join("{} {}%".format(p.get("name"), _fmt(p.get("cpu_percent"))) for p in top["cpu"][:5])
        )
    if active is not None:
        lines.append("")
        if active:
            lines.append("Cảnh báo đang hiệu lực:")
            for alert in active:
                lines.append("  [{}] {} — {}".format(alert.get("severity"), alert.get("title"), alert.get("body")))
        else:
            lines.append("Không có cảnh báo nào. ✔")
    return "\n".join(lines)


def cmd_status(args):
    data = _daemon_status()
    if data:
        snap, active, source = data["snapshot"], data.get("active") or [], "daemon"
    else:
        from .collectors.system import SystemCollector

        collector = SystemCollector()
        collector.sample()
        time.sleep(1)
        snap, active, source = collector.sample(), None, "đo trực tiếp (daemon không chạy)"
    if args.json:
        print(json.dumps({"source": source, "snapshot": snap, "active": active}, ensure_ascii=False, indent=2))
        return 0
    print(f"PC Health Watch {__version__} — nguồn: {source}\n")
    print(format_snapshot(snap, active))
    return 0


def cmd_history(args):
    from .recorder import read_recent

    records = read_recent(seconds=args.minutes * 60)
    if args.json:
        print(json.dumps(records, ensure_ascii=False))
        return 0
    if not records:
        print(f"Chưa có dữ liệu hộp đen trong {args.minutes} phút qua (daemon có chạy không?)")
        return 1
    print(
        "{:<8} {:>6} {:>6} {:>6} {:>8} {:>7} {:>6}  {}".format(
            "Giờ", "CPU%", "RAM%", "Swap%", "PSImem", "PSIio", "°C", "ghi chú"
        )
    )
    for rec in records:
        when = time.strftime("%H:%M:%S", time.localtime(rec.get("ts", 0)))
        kind = rec.get("type")
        if kind == "sample":
            psi = rec.get("pressure") or {}
            temps = [t.get("temp_c") for t in rec.get("thermal") or [] if t.get("temp_c") is not None]
            print(
                "{:<8} {:>6} {:>6} {:>6} {:>8} {:>7} {:>6}".format(
                    when,
                    _fmt((rec.get("cpu") or {}).get("percent")),
                    _fmt((rec.get("memory") or {}).get("used_percent")),
                    _fmt((rec.get("memory") or {}).get("swap_used_percent")),
                    _fmt((psi.get("memory") or {}).get("full_avg10"), "", 1),
                    _fmt((psi.get("io") or {}).get("full_avg10"), "", 1),
                    _fmt(max(temps) if temps else None),
                )
            )
        elif kind in ("alert", "event"):
            print("{:<8} {}".format(when, "⚠ " + str(rec.get("title") or rec.get("summary"))))
        elif kind in ("start", "stop"):
            print("{:<8} — daemon {} —".format(when, "bắt đầu" if kind == "start" else "dừng"))
    return 0


def cmd_report(args):
    try:
        from . import postmortem
    except ImportError as error:
        print(f"Thiếu module postmortem: {error}", file=sys.stderr)
        return 1
    report = postmortem.build_report(boot_index=args.boot)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(postmortem.format_report(report, color=sys.stdout.isatty()))
    return 0


def _check_rows():
    rows = []  # (ok: True/False/None, tên, chi tiết, gợi ý)

    def add(ok, name, detail, hint=""):
        rows.append((ok, name, detail, hint))

    add(sys.version_info >= (3, 8), "Python", sys.version.split()[0], "cần Python 3.8+")
    psi = os.path.exists("/proc/pressure/memory")
    add(psi, "PSI (/proc/pressure)", "có" if psi else "không có", "thêm `psi=1` vào GRUB_CMDLINE_LINUX_DEFAULT")
    persistent = os.path.isdir("/var/log/journal")
    add(
        persistent,
        "Journal lưu bền",
        "/var/log/journal" if persistent else "chỉ trong RAM — reset là mất log",
        "sudo mkdir -p /var/log/journal && sudo systemd-tmpfiles --create --prefix /var/log/journal"
        " && sudo systemctl restart systemd-journald",
    )
    code, out = util.run(["journalctl", "-k", "-n", "1", "--no-pager", "-q"], timeout=5)
    add(
        code == 0 and bool(out.strip()),
        "Đọc kernel log",
        "được" if out.strip() else "không",
        "sudo usermod -aG adm $USER (đăng xuất/vào lại)",
    )
    sysrq = util.read_text("/proc/sys/kernel/sysrq")
    add(
        sysrq in ("1",) or (sysrq.isdigit() and int(sysrq) & 0x80 and int(sysrq) & 0x40 and int(sysrq) & 0x10),
        "Magic SysRq (cứu máy khi đơ)",
        "kernel.sysrq = " + (sysrq or "?"),
        "echo 'kernel.sysrq=1' | sudo tee /etc/sysctl.d/99-sysrq.conf && sudo sysctl --system",
    )
    oom = (
        shutil.which("earlyoom")
        or os.path.exists("/usr/lib/systemd/systemd-oomd")
        or os.path.exists("/lib/systemd/systemd-oomd")
    )
    code_e, _ = util.run(["systemctl", "is-active", "--quiet", "earlyoom"], timeout=3)
    code_o, _ = util.run(["systemctl", "is-active", "--quiet", "systemd-oomd"], timeout=3)
    add(
        code_e == 0 or code_o == 0,
        "OOM killer sớm (earlyoom/systemd-oomd)",
        "đang chạy" if (code_e == 0 or code_o == 0) else ("đã cài nhưng chưa chạy" if oom else "chưa có"),
        "sudo apt install earlyoom (Ubuntu 20.04) — tránh đơ cứng khi hết RAM",
    )
    swaps = util.read_text("/proc/swaps").splitlines()[1:]
    add(bool(swaps), "Swap", f"{len(swaps)} vùng swap" if swaps else "không có", "tạo swapfile hoặc cài zram-config")
    smartctl = shutil.which("smartctl")
    if smartctl:
        code_s, _ = util.run(["sudo", "-n", "smartctl", "--version"], timeout=5)
        add(
            os.geteuid() == 0 or code_s == 0,
            "SMART (sức khỏe SSD)",
            "đọc được" if (os.geteuid() == 0 or code_s == 0) else "cần sudo không mật khẩu cho smartctl",
            "chạy ./install.sh --smart-sudoers",
        )
    else:
        add(False, "SMART (sức khỏe SSD)", "chưa cài smartctl", "sudo apt install smartmontools")
    add(
        bool(shutil.which("notify-send")),
        "notify-send",
        "có" if shutil.which("notify-send") else "thiếu",
        "sudo apt install libnotify-bin",
    )
    try:
        import gi  # noqa: F401

        add(True, "python3-gi (popup)", "có")
    except ImportError:
        add(False, "python3-gi (popup)", "thiếu", "sudo apt install python3-gi gir1.2-gtk-3.0")
    code_d, out_d = util.run(["systemctl", "--user", "is-active", "pc-health-watch"], timeout=3)
    add(
        code_d == 0,
        "Daemon pc-health-watch",
        out_d.strip() or "không chạy",
        "systemctl --user enable --now pc-health-watch",
    )
    return rows


def cmd_check(args):
    rows = _check_rows()
    bad = 0
    for ok, name, detail, hint in rows:
        mark = "✔" if ok else "✘"
        print(f"{mark} {name:<40} {detail}")
        if not ok:
            bad += 1
            if hint:
                print("    → " + hint)
    print(f"\n{bad} mục cần xử lý." if bad else "\nMọi thứ sẵn sàng.")
    return 0 if not bad else 2


def cmd_test_alert(args):
    from .notify import Notifier

    cfg, _ = config_mod.load_config()
    severity = "critical" if args.critical else "warning"
    Notifier(cfg).send(severity, f"Thử cảnh báo ({severity})", "Nếu bạn thấy thông báo này thì cảnh báo hoạt động.")
    print(f"Đã gửi cảnh báo thử mức {severity}.")
    return 0


def cmd_config(args):
    if args.default:
        print(json.dumps(config_mod.DEFAULTS, ensure_ascii=False, indent=2))
        return 0
    cfg, error = config_mod.load_config()
    if error:
        print(error, file=sys.stderr)
    print("# " + config_mod.default_path())
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="healthwatch", usage=USAGE)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("daemon")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("history")
    p.add_argument("-m", "--minutes", type=int, default=15)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("report")
    p.add_argument("--boot", type=int, default=-1, help="chỉ số boot của journalctl (-1 = lần trước)")
    p.add_argument("--json", action="store_true")
    sub.add_parser("check")
    p = sub.add_parser("test-alert")
    p.add_argument("--critical", action="store_true")
    p = sub.add_parser("config")
    p.add_argument("--default", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "daemon":
        from .daemon import run_daemon

        return run_daemon()
    handlers = {
        "status": cmd_status,
        "history": cmd_history,
        "report": cmd_report,
        "check": cmd_check,
        "test-alert": cmd_test_alert,
        "config": cmd_config,
    }
    if args.command not in handlers:
        print(USAGE)
        return 1
    return handlers[args.command](args)
