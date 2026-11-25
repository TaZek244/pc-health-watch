"""Test freeze watchdog: state machine + mô phỏng dòng thời gian trên /proc giả với SysRq giả."""

import json
import os
import tempfile
import unittest
from unittest import mock

from healthwatch import watchdog as wd


def cfg(**kw):
    c = dict(wd.DEFAULTS)
    c.update(kw)
    return c


class DetectorTest(unittest.TestCase):
    def run_timeline(self, config, signal_at, until):
        det = wd.FreezeDetector(config)
        events = []
        for t in range(until):
            for kind, arg in det.update(signal_at(t), float(t)):
                events.append((t, kind, arg))
        return events

    def test_short_spike_ignored(self):
        events = self.run_timeline(cfg(), lambda t: {"memory": 10 <= t < 35, "io": False, "gui": False}, 200)
        self.assertEqual(events, [])

    def test_full_escalation_timed_from_signal_start(self):
        events = self.run_timeline(cfg(), lambda t: {"memory": t >= 10, "io": False, "gui": False}, 400)
        kinds = [(t, k) for t, k, _ in events]
        self.assertEqual(kinds[0], (40, "start"))  # 10 + detect 30
        self.assertIn((70, "oom_kill"), kinds)  # 10 + 60
        self.assertIn((130, "oom_kill"), kinds)  # 10 + 120
        self.assertEqual(sum(1 for _, k in kinds if k == "oom_kill"), 2)
        self.assertIn((290, "report"), kinds)  # 10 + 300 - 20
        self.assertIn((310, "action"), kinds)  # 10 + 300
        self.assertEqual(sum(1 for _, k in kinds if k == "action"), 1)

    def test_no_oom_kill_for_io_or_gui(self):
        events = self.run_timeline(cfg(), lambda t: {"memory": False, "io": True, "gui": t % 2 == 0}, 400)
        self.assertFalse(any(k == "oom_kill" for _, k, _ in events))
        self.assertTrue(any(k == "action" for _, k, _ in events))

    def test_recovery_cancels(self):
        events = self.run_timeline(cfg(), lambda t: {"memory": False, "io": 0 <= t < 100, "gui": False}, 400)
        kinds = [k for _, k, _ in events]
        self.assertEqual(kinds, ["start", "recovered"])
        self.assertEqual(events[-1][0], 115)  # hết dấu hiệu ở 100, ổn định 15 s

    def test_flicker_does_not_recover(self):
        # dấu hiệu tắt 5 s rồi bật lại: chưa đủ recover_seconds → vẫn tính là đang đơ
        events = self.run_timeline(cfg(), lambda t: {"io": not (100 <= t < 105), "memory": False, "gui": False}, 400)
        kinds = [k for _, k, _ in events]
        self.assertNotIn("recovered", kinds)
        self.assertIn("action", kinds)

    def test_config_floor(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(
                {"mode": "act", "action": "reboot", "action_after_seconds": 5, "detect_seconds": 1, "mode2": 1}, f
            )
        try:
            c = wd.load_config(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual((c["mode"], c["action"]), ("act", "reboot"))
        self.assertEqual(c["action_after_seconds"], 60)
        self.assertEqual(c["detect_seconds"], 10)

    def test_bad_mode_falls_back_to_log(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"mode": "yes"}, f)
        try:
            self.assertEqual(wd.load_config(f.name)["mode"], "log")
        finally:
            os.unlink(f.name)


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class FakeKmsg:
    def __init__(self):
        self.events = []
        self.lines = []

    def poll(self, now):
        pass

    def write(self, message, level=4):
        self.lines.append(message)


class SimulationTest(unittest.TestCase):
    """Mô phỏng: từ t=20 RAM thrashing (memory full stall 90%)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        write(
            self.root, "proc/meminfo", "MemTotal: 8000000 kB\nMemAvailable: 50000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
        )
        write(self.root, "proc/123/stat", "123 (chrome) D 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 0 0 1000000 0\n")
        self.reports = os.path.join(self.root, "reports")
        os.makedirs(self.reports)
        patcher = mock.patch.object(wd, "REPORT_DIR", self.reports)
        patcher.start()
        self.addCleanup(patcher.stop)

    def simulate(self, mode, until=400, stall_from=20):
        keys = []
        kmsg = FakeKmsg()
        w = wd.Watchdog(
            cfg(mode=mode), root=self.root, sysrq_fn=lambda k: keys.append(k) or True, kmsg=kmsg, sleep=lambda s: None
        )
        mem_total = 0
        for t in range(until):
            if t >= stall_from:
                mem_total += 900000  # 0.9 s nghẽn mỗi giây = 90%
            write(
                self.root,
                "proc/pressure/memory",
                f"some avg10=0 avg60=0 avg300=0 total={mem_total}\nfull avg10=0 avg60=0 avg300=0 total={mem_total}\n",
            )
            write(
                self.root,
                "proc/pressure/io",
                "some avg10=0 avg60=0 avg300=0 total=0\nfull avg10=0 avg60=0 avg300=0 total=0\n",
            )
            w.tick(float(t))
        return keys, kmsg, w

    def test_act_mode_kills_then_powers_off(self):
        keys, kmsg, w = self.simulate("act")
        self.assertEqual(keys, ["f", "f", "s", "u", "o"])
        reports = os.listdir(self.reports)
        self.assertEqual(len(reports), 1)
        with open(os.path.join(self.reports, reports[0])) as f:
            report = json.load(f)
        self.assertEqual(report["outcome"], "action_poweroff")
        self.assertEqual(report["reasons"], ["memory"])
        self.assertEqual(report["processes"]["d_state"][0]["name"], "chrome")
        self.assertTrue(report["samples"])
        self.assertTrue(any("nghi máy đơ" in line for line in kmsg.lines))

    def test_log_mode_never_touches_sysrq(self):
        keys, kmsg, w = self.simulate("log")
        self.assertEqual(keys, [])
        self.assertTrue(any("lẽ ra đã poweroff" in line for line in kmsg.lines))
        self.assertEqual(len(os.listdir(self.reports)), 1)

    def test_report_writer_is_reused_and_warm_up_works(self):
        w = wd.Watchdog(cfg(), root=self.root, sysrq_fn=lambda k: True, kmsg=FakeKmsg(), sleep=lambda s: None)
        w.warm_up()
        thread = w._writer.thread
        self.assertTrue(w.save_report({"a": 1}))
        self.assertTrue(w.save_report({"b": 2}))
        self.assertIs(w._writer.thread, thread)  # không tạo luồng mới cho mỗi báo cáo
        self.assertIsNone(w.processes)

    def test_stuck_writer_does_not_block(self):
        writer = wd.ReportWriter()
        with mock.patch.object(wd, "_write_json_atomic", side_effect=lambda *a: __import__("time").sleep(3)):
            self.assertFalse(writer.save("/x", {}, 0.2))  # ổ "treo": trả về sau timeout
            self.assertFalse(writer.save("/x", {}, 0.2))
            self.assertFalse(writer.save("/x", {}, 0.2))  # hàng đợi đầy: trả ngay, không chặn

    def test_healthy_machine_does_nothing(self):
        keys, kmsg, w = self.simulate("act", until=200, stall_from=10**9)
        self.assertEqual(keys, [])
        self.assertEqual(kmsg.lines, [])
        self.assertEqual(len(w.samples), 0)  # máy khỏe: không tốn RAM ghi mẫu

    def test_gui_signal_from_heartbeat(self):
        keys = []
        w = wd.Watchdog(
            cfg(mode="act"),
            root=self.root,
            sysrq_fn=lambda k: keys.append(k),
            kmsg=FakeKmsg(),
            heartbeats=lambda: [{"x": 1}],
            sleep=lambda s: None,
        )
        w.gui_hung = lambda hb, now, secs: now >= 50
        write(self.root, "proc/pressure/memory", "some total=0\nfull total=0\n")
        write(self.root, "proc/pressure/io", "some total=0\nfull total=0\n")
        for t in range(400):
            w.tick(float(t))
        self.assertEqual(keys, ["s", "u", "o"])  # không OOM-kill khi chỉ giao diện treo


if __name__ == "__main__":
    unittest.main()
