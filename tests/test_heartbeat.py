"""Test phần nhịp tim giao diện (heartbeat). Không chạy lệnh hệ thống thật."""

import json
import os
import subprocess
import tempfile
import types
import unittest

from healthwatch import heartbeat as hb


GNOME_COMMAND = [
    "gdbus",
    "call",
    "--session",
    "--dest",
    "org.gnome.Shell",
    "--object-path",
    "/org/gnome/Shell",
    "--method",
    "org.freedesktop.DBus.Peer.Ping",
]

KDE_COMMAND = [
    "gdbus",
    "call",
    "--session",
    "--dest",
    "org.kde.KWin",
    "--object-path",
    "/KWin",
    "--method",
    "org.freedesktop.DBus.Peer.Ping",
]


class FakeClock:
    """Đồng hồ giả: giá trị do test đặt, mỗi lần gọi trả cùng một mốc."""

    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class ProbeSequence:
    """Probe giả trả lần lượt các giá trị đã định, có ghi lại tham số gọi."""

    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def __call__(self, command, timeout=8, env=None):
        self.calls.append({"command": command, "timeout": timeout, "env": env})
        return self.values.pop(0)


def fake_run(returncode=None, error=None):
    """Tạo hàm thay cho subprocess.run, ghi lại kwargs."""

    def run(command, **kwargs):
        run.calls.append({"command": command, "kwargs": kwargs})
        if error is not None:
            raise error
        return types.SimpleNamespace(returncode=returncode)

    run.calls = []
    return run


def heartbeat(**overrides):
    base = {
        "status": "running",
        "gui": False,
        "mono": 1000.0,
        "gui_fail_since": 900.0,
    }
    base.update(overrides)
    return base


class DetectProbeTest(unittest.TestCase):
    def test_gnome(self):
        env = {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME", "DISPLAY": ":0"}
        command = hb.detect_probe(env=env, which=lambda name: "/usr/bin/" + name)
        self.assertEqual(command, GNOME_COMMAND)

    def test_kde(self):
        env = {"XDG_CURRENT_DESKTOP": "KDE"}
        command = hb.detect_probe(env=env, which=lambda name: "/usr/bin/" + name)
        self.assertEqual(command, KDE_COMMAND)

    def test_hyprland(self):
        env = {"HYPRLAND_INSTANCE_SIGNATURE": "instance-abc", "DISPLAY": ":0"}
        command = hb.detect_probe(env=env, which=lambda name: "/usr/bin/" + name)
        self.assertEqual(command, ["hyprctl", "-j", "version"])

    def test_x11(self):
        env = {"DISPLAY": ":0"}
        command = hb.detect_probe(env=env, which=lambda name: "/usr/bin/" + name)
        self.assertEqual(command, ["xset", "q"])

    def test_nothing(self):
        self.assertIsNone(hb.detect_probe(env={}, which=lambda name: "/usr/bin/" + name))

    def test_missing_binary_returns_none(self):
        env = {"XDG_CURRENT_DESKTOP": "GNOME"}
        self.assertIsNone(hb.detect_probe(env=env, which=lambda name: None))


class ProbeGuiTest(unittest.TestCase):
    def test_exit_zero_is_alive(self):
        run = fake_run(returncode=0)
        self.assertIs(hb.probe_gui(["probe"], timeout=3, env={"A": "1"}, run=run), True)
        call = run.calls[0]
        self.assertEqual(call["kwargs"]["timeout"], 3)
        self.assertEqual(call["kwargs"]["env"], {"A": "1"})
        self.assertIs(call["kwargs"]["stdout"], subprocess.DEVNULL)
        self.assertIs(call["kwargs"]["stderr"], subprocess.DEVNULL)

    def test_nonzero_exit_is_unknown(self):
        run = fake_run(returncode=1)
        self.assertIsNone(hb.probe_gui(["probe"], run=run))

    def test_timeout_is_hung(self):
        run = fake_run(error=subprocess.TimeoutExpired(["probe"], 8))
        self.assertIs(hb.probe_gui(["probe"], run=run), False)

    def test_oserror_is_unknown(self):
        run = fake_run(error=OSError("không khởi chạy được"))
        self.assertIsNone(hb.probe_gui(["probe"], run=run))

    def test_none_command_is_unknown(self):
        run = fake_run(returncode=0)
        self.assertIsNone(hb.probe_gui(None, run=run))
        self.assertEqual(run.calls, [])


class GuiMonitorTest(unittest.TestCase):
    def test_false_chain_keeps_first_fail_since_then_true_resets(self):
        clock = FakeClock(100.0)
        probe = ProbeSequence([False, False, True])
        monitor = hb.GuiMonitor(["probe"], clock=clock, probe=probe)

        self.assertIs(monitor.check_once(), False)
        self.assertIs(monitor.state, False)
        self.assertEqual(monitor.fail_since, 100.0)
        self.assertEqual(monitor.last_checked, 100.0)

        clock.value = 110.0
        self.assertIs(monitor.check_once(), False)
        # Vẫn giữ mốc của lần False đầu tiên, không bị đẩy về 110.
        self.assertEqual(monitor.fail_since, 100.0)
        self.assertEqual(monitor.last_checked, 110.0)

        clock.value = 120.0
        self.assertIs(monitor.check_once(), True)
        self.assertIs(monitor.state, True)
        self.assertIsNone(monitor.fail_since)
        self.assertEqual(monitor.last_checked, 120.0)

    def test_unknown_resets_fail_since(self):
        clock = FakeClock(10.0)
        probe = ProbeSequence([False, None])
        monitor = hb.GuiMonitor(["probe"], clock=clock, probe=probe)

        self.assertIs(monitor.check_once(), False)
        self.assertEqual(monitor.fail_since, 10.0)

        clock.value = 20.0
        self.assertIsNone(monitor.check_once())
        self.assertIsNone(monitor.state)
        self.assertIsNone(monitor.fail_since)

    def test_initial_state_is_unknown(self):
        monitor = hb.GuiMonitor(["probe"], probe=ProbeSequence([True]))
        self.assertIsNone(monitor.state)
        self.assertIsNone(monitor.fail_since)
        self.assertIsNone(monitor.last_checked)


class HeartbeatWriterTest(unittest.TestCase):
    def test_beat_then_read_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runtime", hb.HEARTBEAT_NAME)
            clock = FakeClock(1234.5)
            writer = hb.HeartbeatWriter(
                path=path, clock=clock, wall=lambda: 9876.25
            )

            writer.beat(False, 1200.0, desktop="ubuntu:GNOME")
            data = hb.read_heartbeat(path)

            self.assertIsNotNone(data)
            self.assertEqual(data["version"], 1)
            self.assertEqual(data["pid"], os.getpid())
            self.assertEqual(data["uid"], os.getuid())
            self.assertEqual(data["status"], "running")
            self.assertEqual(data["mono"], 1234.5)
            self.assertEqual(data["ts"], 9876.25)
            self.assertIs(data["gui"], False)
            self.assertEqual(data["gui_fail_since"], 1200.0)
            self.assertEqual(data["desktop"], "ubuntu:GNOME")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
            # Không còn file tạm nào sót lại.
            self.assertEqual(os.listdir(os.path.dirname(path)), [hb.HEARTBEAT_NAME])

            clock.value = 1235.5
            writer.stop()
            stopped = hb.read_heartbeat(path)
            self.assertIsNotNone(stopped)
            self.assertEqual(stopped["status"], "stopped")
            self.assertIsNone(stopped["gui"])
            self.assertIsNone(stopped["gui_fail_since"])

    def test_gui_none_is_written_as_json_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, hb.HEARTBEAT_NAME)
            hb.HeartbeatWriter(path=path).beat(None, None)
            with open(path, encoding="utf-8") as stream:
                raw = stream.read()
            self.assertIn('"gui":null', raw)


class ReadHeartbeatsTest(unittest.TestCase):
    def test_read_all_skips_broken_and_non_object(self):
        with tempfile.TemporaryDirectory() as run_root:
            good_dir = os.path.join(run_root, "1000")
            os.makedirs(good_dir)
            good = heartbeat(uid=1000)
            with open(
                os.path.join(good_dir, hb.HEARTBEAT_NAME), "w", encoding="utf-8"
            ) as stream:
                json.dump(good, stream)

            broken_dir = os.path.join(run_root, "1001")
            os.makedirs(broken_dir)
            with open(
                os.path.join(broken_dir, hb.HEARTBEAT_NAME), "w", encoding="utf-8"
            ) as stream:
                stream.write("{khong-phai-json")

            list_dir = os.path.join(run_root, "1002")
            os.makedirs(list_dir)
            with open(
                os.path.join(list_dir, hb.HEARTBEAT_NAME), "w", encoding="utf-8"
            ) as stream:
                json.dump([1, 2, 3], stream)

            # Thư mục không có file nhịp tim cũng phải bị bỏ qua.
            os.makedirs(os.path.join(run_root, "1003"))

            result = hb.read_all_heartbeats(run_root=run_root)
            self.assertEqual(result, [good])

    def test_missing_run_root_returns_empty(self):
        self.assertEqual(
            hb.read_all_heartbeats(run_root="/khong/ton/tai/dau"), []
        )

    def test_read_heartbeat_bad_path(self):
        self.assertIsNone(hb.read_heartbeat("/khong/ton/tai.json"))


class GuiHungTest(unittest.TestCase):
    def test_enough_hang_time(self):
        self.assertIs(hb.gui_hung(heartbeat(), now_mono=1010.0), True)

    def test_exactly_at_threshold(self):
        self.assertIs(
            hb.gui_hung(heartbeat(gui_fail_since=965.0), now_mono=1010.0), True
        )

    def test_not_enough_hang_time(self):
        self.assertIs(
            hb.gui_hung(heartbeat(gui_fail_since=980.0), now_mono=1010.0), False
        )

    def test_stopped_is_not_hung(self):
        self.assertIs(
            hb.gui_hung(heartbeat(status="stopped"), now_mono=1010.0), False
        )

    def test_stale_heartbeat_is_not_trusted(self):
        # Nhịp tim cũ 201 giây: dù fail_since rất cũ cũng không được tắt máy.
        self.assertIs(
            hb.gui_hung(heartbeat(gui_fail_since=800.0), now_mono=1201.0), False
        )

    def test_gui_none_is_not_hung(self):
        self.assertIs(hb.gui_hung(heartbeat(gui=None), now_mono=1010.0), False)

    def test_gui_true_is_not_hung(self):
        self.assertIs(hb.gui_hung(heartbeat(gui=True), now_mono=1010.0), False)

    def test_missing_fail_since_is_not_hung(self):
        self.assertIs(
            hb.gui_hung(heartbeat(gui_fail_since=None), now_mono=1010.0), False
        )

    def test_non_dict_is_not_hung(self):
        self.assertIs(hb.gui_hung(None, now_mono=1010.0), False)


if __name__ == "__main__":
    unittest.main()
