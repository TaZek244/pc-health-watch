"""Test phần lõi: config, rules, recorder, daemon."""

import json
import os
import tempfile
import unittest
from unittest import mock

from healthwatch import config as config_mod
from healthwatch import daemon as daemon_mod
from healthwatch.recorder import Recorder, read_recent
from healthwatch.rules import RuleEngine, ThresholdRule


def snap(mem=50.0, swap=0.0, psi_mem=0.0, psi_io=0.0, temp=50.0, free_gb=50.0, readonly=False, await_ms=1.0):
    return {
        "ts": 0,
        "cpu": {"percent": 10.0},
        "memory": {
            "total_mb": 8000,
            "available_mb": 8000 * (100 - mem) / 100,
            "used_percent": mem,
            "swap_total_mb": 2048,
            "swap_used_mb": 0,
            "swap_used_percent": swap,
        },
        "pressure": {"memory": {"full_avg10": psi_mem}, "io": {"full_avg10": psi_io}, "cpu": {"some_avg10": 0}},
        "disks": [{"mount": "/", "device": "/dev/sda2", "used_percent": 60, "free_gb": free_gb, "readonly": readonly}],
        "io": [{"device": "sda", "util_percent": 50, "await_ms": await_ms}],
        "thermal": [{"chip": "coretemp", "label": "Package id 0", "temp_c": temp}],
        "top": {"memory": [{"pid": 42, "name": "chrome", "rss_mb": 3000}], "cpu": []},
    }


def cfg():
    c, _ = config_mod.load_config(os.devnull + "-missing")
    return c


class ConfigTest(unittest.TestCase):
    def test_merge_partial_and_reject_bad_types(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"rules": {"memory_used": {"warning": 80, "critical": "x"}}, "bogus": 1, "interval_seconds": -3}, f)
        try:
            c, err = config_mod.load_config(f.name)
        finally:
            os.unlink(f.name)
        self.assertIsNone(err)
        self.assertEqual(c["rules"]["memory_used"]["warning"], 80)
        self.assertEqual(c["rules"]["memory_used"]["critical"], 96)
        self.assertEqual(c["interval_seconds"], 5)
        self.assertNotIn("bogus", c)

    def test_broken_json_returns_defaults_with_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{oops")
        try:
            c, err = config_mod.load_config(f.name)
        finally:
            os.unlink(f.name)
        self.assertIsNotNone(err)
        self.assertEqual(c, config_mod.DEFAULTS)


class RuleTest(unittest.TestCase):
    def setUp(self):
        self.engine = RuleEngine(cfg())

    def run_series(self, values, step=5, **kw):
        out = []
        for i, v in enumerate(values):
            out.append(self.engine.evaluate(snap(**{kw.get("field", "mem"): v}), i * step))
        return out

    def test_memory_needs_hold_time(self):
        # warning 90 / for 20s: 3 mẫu (0,5,10s) chưa đủ, mẫu ở 20s mới báo
        res = self.run_series([91, 91, 91, 91, 91])
        names = [[a.severity for a in r if a.rule == "memory_used"] for r in res]
        self.assertEqual(names, [[], [], [], [], ["warning"]])

    def test_spike_does_not_alert(self):
        res = self.run_series([99, 50, 99, 50, 99, 50])
        self.assertFalse(any(a.rule == "memory_used" for r in res for a in r))

    def test_escalate_repeat_and_recover(self):
        c = cfg()
        c["notify"]["repeat_minutes"] = 1
        engine = RuleEngine(c)
        alerts = []
        t = 0
        for v in [97] * 6 + [97] * 12 + [50] * 6:  # 30s, rồi thêm 60s critical, rồi hồi phục
            alerts += [(t, a.severity, a.recovered) for a in engine.evaluate(snap(mem=v), t) if a.rule == "memory_used"]
            t += 5
        sev = [s for _, s, _ in alerts]
        self.assertEqual(sev[0], "critical")
        self.assertIn("critical", sev[1:-1])  # nhắc lại sau 1 phút
        self.assertTrue(alerts[-1][2])  # báo đã hồi phục

    def test_hysteresis_prevents_flapping(self):
        rule = ThresholdRule("x", lambda s: [("k", s, None)], lambda lvl, v, c: (lvl, ""), hysteresis=3)
        settings = {"warning": 90, "critical": 99, "for_seconds": 0}
        out = []
        for i, v in enumerate([91, 89, 91, 89, 86]):
            out += [a.severity for a in rule.evaluate(v, settings, i, 0)]
        self.assertEqual(out, ["warning", "info"])

    def test_disk_free_lower_is_worse_and_readonly(self):
        alerts = self.engine.evaluate(snap(free_gb=0.5, readonly=True), 0)
        rules = {a.rule: a.severity for a in alerts}
        self.assertEqual(rules.get("disk_free_gb"), "critical")
        self.assertEqual(rules.get("disk_readonly"), "critical")

    def test_missing_pressure_is_ignored(self):
        s = snap()
        s["pressure"] = None
        self.assertEqual(self.engine.evaluate(s, 0), [])

    def test_disabled_rule(self):
        c = cfg()
        c["rules"]["temperature"]["enabled"] = False
        engine = RuleEngine(c)
        alerts = [a for t in range(0, 60, 5) for a in engine.evaluate(snap(temp=99), t)]
        self.assertFalse(any(a.rule == "temperature" for a in alerts))


class RecorderTest(unittest.TestCase):
    def test_write_read_and_truncated_line(self):
        with tempfile.TemporaryDirectory() as d:
            rec = Recorder(d, boot="b-1", clock=lambda: 1000.0)
            rec.write("start")
            rec.write("sample", {"cpu": {"percent": 1}})
            rec.close()
            path = os.path.join(d, os.listdir(d)[0])
            with open(path, "a") as f:
                f.write('{"type":"sample","ts":10')  # dòng bị cắt do reset
            recs = read_recent(d, seconds=100, now=1050.0)
            self.assertEqual([r["type"] for r in recs], ["start", "sample"])
            self.assertEqual(recs[0]["boot_id"], "b-1")

    def test_unwritable_dir_does_not_raise(self):
        rec = Recorder("/proc/nope/pc-health-watch", boot="x")
        rec.write("sample", {"a": 1})
        self.assertTrue(rec.failed)

    def test_retention(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "blackbox-2000-01-01.jsonl")
            open(old, "w").close()
            os.utime(old, (0, 0))
            Recorder(d, boot="x", retention_days=1).write("start")
            self.assertFalse(os.path.exists(old))


class FakeCollector(object):
    def __init__(self, series):
        self.series = list(series)

    def sample(self):
        return self.series.pop(0) if len(self.series) > 1 else self.series[0]


class FakeNotifier(object):
    def __init__(self):
        self.sent = []
        self.config = None

    def send(self, severity, title, body, tag=None):
        self.sent.append((severity, title))


class DaemonTest(unittest.TestCase):
    def make(self, series, c=None):
        c = c or cfg()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        notifier = FakeNotifier()
        d = daemon_mod.Daemon(
            c,
            collector=FakeCollector(series),
            recorder=Recorder(self.tmp.name, boot="b"),
            notifier=notifier,
            follower_factory=lambda cb: None,
        )
        return d, notifier

    def test_tick_records_and_alerts(self):
        d, notifier = self.make([snap(mem=99)])
        with mock.patch.object(daemon_mod, "write_status"):
            for t in range(0, 40, 5):
                d.tick(float(t))
        self.assertIn("critical", [s for s, _ in notifier.sent])
        recs = read_recent(self.tmp.name, seconds=10 ** 10, now=10 ** 10)
        types = {r["type"] for r in recs}
        self.assertTrue({"sample", "alert"} <= types)

    def test_kernel_events_deduped(self):
        d, notifier = self.make([snap()])
        ev = {"category": "disk", "severity": "critical", "summary": "Lỗi I/O trên sda", "line": "x"}
        for _ in range(3):
            d.events.put(dict(ev))
        d.drain_events(100.0)
        d.events.put(dict(ev))
        d.drain_events(500.0)
        self.assertEqual(len(notifier.sent), 2)

    def test_relieve_memory_disabled_by_default(self):
        d, notifier = self.make([snap(psi_mem=90)])
        with mock.patch("os.kill") as kill:
            for t in range(0, 60, 5):
                d.maybe_relieve_memory(snap(psi_mem=90), float(t))
            kill.assert_not_called()

    def test_relieve_memory_kills_user_process(self):
        c = cfg()
        c["actions"]["relieve_memory"]["enabled"] = True
        d, notifier = self.make([snap()], c)
        with mock.patch("os.kill") as kill, mock.patch.object(daemon_mod, "_uid_of", return_value=os.getuid()):
            for t in range(0, 30, 5):
                d.maybe_relieve_memory(snap(psi_mem=90), float(t))
            kill.assert_called_once()
            self.assertEqual(kill.call_args[0][0], 42)


if __name__ == "__main__":
    unittest.main()
