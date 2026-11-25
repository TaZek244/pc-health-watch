import gzip
import json
import os
import tempfile
import unittest

from healthwatch import config as config_mod, postmortem
from healthwatch.recording import AdaptiveSchedule, compact, is_elevated
from healthwatch.recorder import Recorder, read_recent


def sample():
    return {
        "ts": 100,
        "cpu": {"iowait_percent": 0},
        "memory": {"used_percent": 81, "swap_used_percent": 0},
        "pressure": {"memory": {"some_avg10": 0}, "io": {"some_avg10": 0}},
        "disks": [{"mount": "/", "free_gb": 10, "used_percent": 50, "readonly": False, "device": "x"}],
        "io": [{"device": "idle", "util_percent": 0, "read_mb_s": 0, "write_mb_s": 0}],
        "thermal": [{"chip": "core", "label": str(i), "temp_c": i} for i in range(20)],
        "top": {
            "memory": [{"pid": i, "rss_mb": i} for i in range(10)],
            "cpu": [{"pid": i, "cpu_percent": i / 2.0} for i in range(10)],
        },
    }


class RecordingTest(unittest.TestCase):
    def test_compact_and_elevated(self):
        cfg, _ = config_mod.load_config(os.devnull + "-missing")
        source = sample()
        small = compact(source)
        self.assertEqual(len(small["thermal"]), 3)
        self.assertEqual(small["io"], [])
        self.assertTrue(is_elevated(source, cfg))
        source["memory"]["used_percent"] = 10
        self.assertFalse(is_elevated(source, cfg))

    def test_schedule_normal_elevated_and_cooldown(self):
        schedule = AdaptiveSchedule({"recording": {"normal_seconds": 30, "elevated_seconds": 2, "cooldown_seconds": 120}})
        self.assertTrue(schedule.due(0, False))
        self.assertFalse(schedule.due(10, False))
        self.assertTrue(schedule.due(30, False))
        self.assertTrue(schedule.due(31, True))
        self.assertFalse(schedule.due(32, True))
        self.assertTrue(schedule.due(33, True))
        self.assertTrue(schedule.due(35, False))
        self.assertTrue(schedule.due(155, False))
        self.assertFalse(schedule.due(156, False))


class StorageTest(unittest.TestCase):
    def test_compress_and_read_gzip_and_truncated(self):
        with tempfile.TemporaryDirectory() as directory:
            old = os.path.join(directory, "blackbox-2026-11-23.jsonl")
            with open(old, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "sample", "boot_id": "b", "ts": 100}) + "\n")
            recorder = Recorder(directory, compress_after_days=1)
            recorder.compress_old(1764028800)  # 2025-11-25 UTC; file vẫn là ngày cũ
            # Dùng mốc có ngày 2026-11-25 để kiểm tra file hôm nay không bị nén.
            recorder.compress_old(1795564800)
            self.assertTrue(os.path.exists(old + ".gz"))
            self.assertEqual(read_recent(directory, seconds=10**9, now=1000)[0]["boot_id"], "b")
            with open(old + ".gz", "ab") as stream:
                stream.write(b"cut")
            self.assertEqual(len(list(postmortem.load_blackbox(directory, "b"))), 1)

    def test_today_not_compressed(self):
        with tempfile.TemporaryDirectory() as directory:
            now = 1795564800
            day = __import__("time").strftime("%Y-%m-%d", __import__("time").localtime(now))
            path = os.path.join(directory, "blackbox-{}.jsonl".format(day))
            open(path, "w").close()
            Recorder(directory, compress_after_days=0).compress_old(now)
            self.assertTrue(os.path.exists(path))

    def test_quota_removes_oldest_but_not_today(self):
        with tempfile.TemporaryDirectory() as directory:
            for day in ("2026-11-20", "2026-11-21", "2026-11-25"):
                with open(os.path.join(directory, "blackbox-{}.jsonl".format(day)), "wb") as stream:
                    stream.write(b"x" * 1000)
            Recorder(directory, max_total_mb=0.001).enforce_limits(1795564800)
            self.assertFalse(os.path.exists(os.path.join(directory, "blackbox-2026-11-20.jsonl")))
            self.assertTrue(os.path.exists(os.path.join(directory, "blackbox-2026-11-25.jsonl")))


if __name__ == "__main__":
    unittest.main()
