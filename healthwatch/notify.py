"""Gửi cảnh báo: notify-send (GNOME/mọi DE), popup GTK3 giữa màn hình, âm thanh.

Daemon chạy dưới systemd --user có thể thiếu DISPLAY/DBUS → tự lấy từ
`systemctl --user show-environment`, cuối cùng đoán giá trị mặc định.
"""

import os
import shutil
import subprocess
import sys

from . import util

ICONS = {"critical": "dialog-error", "warning": "dialog-warning", "info": "dialog-information"}
SOUNDS = (
    "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga",
    "/usr/share/sounds/freedesktop/stereo/bell.oga",
)
GUI_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY", "XDG_RUNTIME_DIR")


def gui_env(base=None, runner=util.run):
    env = dict(os.environ if base is None else base)
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")) or not env.get("DBUS_SESSION_BUS_ADDRESS"):
        code, out = runner(["systemctl", "--user", "show-environment"], timeout=3)
        if code == 0:
            for line in out.splitlines():
                key, sep, value = line.partition("=")
                if sep and key in GUI_VARS and not env.get(key):
                    env[key] = value
    runtime = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and os.path.exists(os.path.join(runtime, "bus")):
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + os.path.join(runtime, "bus")
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        env["DISPLAY"] = ":0"
    return env


class Notifier:
    def __init__(self, config, log=None):
        self.config = config
        self.log = log or (lambda message: None)
        self.popup_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "popup.py")

    def send(self, severity, title, body, tag=None):
        settings = self.config["notify"]
        env = gui_env()
        urgency = {"critical": "critical", "warning": "normal"}.get(severity, "low")
        if settings.get("desktop", True) and shutil.which("notify-send"):
            command = ["notify-send", "-a", "PC Health Watch", "-u", urgency, "-i", ICONS.get(severity, ICONS["info"])]
            if tag:  # GNOME/dunst thay thế thông báo cũ cùng tag thay vì xếp chồng
                command += ["-h", "string:x-canonical-private-synchronous:" + tag]
            self._spawn(command + [title, body], env)
        if severity == "critical" and settings.get("popup", True):
            self._spawn([sys.executable, self.popup_script, "--title", title, "--body", body], env)
        if severity == "critical" and settings.get("sound", True):
            self._beep(env)

    def _beep(self, env):
        sound = next((path for path in SOUNDS if os.path.exists(path)), None)
        if sound and shutil.which("paplay"):
            self._spawn(["paplay", sound], env)
        elif shutil.which("canberra-gtk-play"):
            self._spawn(["canberra-gtk-play", "-i", "dialog-warning"], env)

    def _spawn(self, command, env):
        # không chờ: daemon phải tiếp tục đo kể cả khi giao diện đang đứng
        try:
            subprocess.Popen(
                command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
            )
        except OSError as error:
            self.log(f"không chạy được {command[0]}: {error}")
