#!/usr/bin/env python3
"""Cửa sổ cảnh báo GTK3 giữa màn hình, luôn nằm trên cùng (dùng được trên GNOME X11/Wayland).

Chạy độc lập: python3 popup.py --title "..." --body "..." [--seconds 30]
Chỉ một popup tại một thời điểm: popup mới thay nội dung popup cũ (khóa file trong XDG_RUNTIME_DIR).
"""

import argparse
import fcntl
import os
import sys

CSS = b"""
window.hw-popup { background-color: #1e1e24; border: 3px solid #e5484d; border-radius: 12px; }
.hw-title { color: #ff6b6b; font-size: 20px; font-weight: bold; }
.hw-body { color: #eeeeee; font-size: 14px; }
.hw-hint { color: #9a9aa5; font-size: 11px; }
button.hw-btn { padding: 6px 16px; }
"""


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument("--seconds", type=int, default=45)
    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    lock = open(os.path.join(runtime, "pc-health-watch-popup.lock"), "w")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # đã có popup đang hiện; thông báo desktop vẫn được gửi riêng

    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, GLib, Gtk
    except (ImportError, ValueError):
        return 1  # không có python3-gi: notify-send đã lo phần cảnh báo

    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    screen = Gdk.Screen.get_default()
    if screen is None:
        return 1
    Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    window = Gtk.Window(title="PC Health Watch")
    window.get_style_context().add_class("hw-popup")
    window.set_decorated(False)
    window.set_keep_above(True)
    window.set_position(Gtk.WindowPosition.CENTER)
    window.set_type_hint(Gdk.WindowTypeHint.DIALOG)
    window.set_default_size(460, -1)
    window.set_border_width(20)
    window.stick()

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    title = Gtk.Label(label="⚠ " + args.title, xalign=0)
    title.get_style_context().add_class("hw-title")
    title.set_line_wrap(True)
    body = Gtk.Label(label=args.body, xalign=0)
    body.get_style_context().add_class("hw-body")
    body.set_line_wrap(True)
    body.set_max_width_chars(60)
    hint = Gtk.Label(xalign=0)
    hint.get_style_context().add_class("hw-hint")

    buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)

    def open_monitor(_button):
        for command in (["gnome-system-monitor", "-p"], ["gnome-system-monitor"], ["x-terminal-emulator", "-e", "htop"]):
            try:
                GLib.spawn_async(command, flags=GLib.SpawnFlags.SEARCH_PATH)
                break
            except GLib.Error:
                continue
        Gtk.main_quit()

    monitor = Gtk.Button(label="Mở System Monitor")
    monitor.connect("clicked", open_monitor)
    close = Gtk.Button(label="Đã hiểu")
    close.connect("clicked", lambda _b: Gtk.main_quit())
    for button in (monitor, close):
        button.get_style_context().add_class("hw-btn")
        buttons.pack_start(button, False, False, 0)

    for widget in (title, body, hint, buttons):
        box.pack_start(widget, False, False, 0)
    window.add(box)
    window.connect("destroy", lambda _w: Gtk.main_quit())
    window.connect("key-press-event", lambda _w, e: e.keyval == Gdk.KEY_Escape and Gtk.main_quit())

    remaining = [max(5, args.seconds)]

    def tick():
        remaining[0] -= 1
        if remaining[0] <= 0:
            Gtk.main_quit()
            return False
        hint.set_text("Tự đóng sau {} giây · Esc để đóng".format(remaining[0]))
        return True

    tick()
    GLib.timeout_add_seconds(1, tick)
    window.show_all()
    window.present()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
