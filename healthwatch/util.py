"""Tiện ích chung: đọc file an toàn, chạy lệnh có timeout."""

import os
import subprocess


def proc_path(root, *parts):
    """Ghép đường dẫn dưới `root` (để test bằng cây /proc, /sys giả)."""
    return os.path.join(root, *[p.lstrip("/") for p in parts])


def read_text(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read().strip()
    except OSError:
        return default


def run(command, timeout=5, env=None):
    """Chạy lệnh, trả (returncode, stdout). Không bao giờ ném lỗi."""
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            env=env,
        )
        return result.returncode, result.stdout
    except (OSError, subprocess.SubprocessError):
        return -1, ""


def boot_id(root="/"):
    return read_text(proc_path(root, "proc/sys/kernel/random/boot_id"), "unknown")


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "pc-health-watch")


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "pc-health-watch")
