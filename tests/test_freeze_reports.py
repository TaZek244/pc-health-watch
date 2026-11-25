"""Test đọc/hiển thị báo cáo đóng băng."""

import json
import os
import tempfile
import unittest

from healthwatch import freeze_reports as fr


class FreezeReportTest(unittest.TestCase):
    def test_load_filter_and_format(self):
        with tempfile.TemporaryDirectory() as d:
            report = {
                "boot_id": "01234567-89ab-cdef-0123-456789abcdef",
                "outcome": "action_poweroff",
                "started_ts": 1000.0,
                "duration_seconds": 300,
                "reason_text": ["hết RAM — hệ thống thrashing"],
                "samples": [{"memory_full": 70.0, "available_mb": 40}, {"memory_full": 92.5, "available_mb": 12}],
                "processes": {"top_memory": [{"name": "chrome", "rss_mb": 5000}], "d_state": [{"name": "kswapd0"}],
                              "d_state_count": 1},
                "actions": [{"t": 60, "do": "oom_kill"}],
                "kernel_events": [{"category": "memory", "summary": "OOM giết chrome"}],
            }
            with open(os.path.join(d, "freeze-20261125-101010.json"), "w") as f:
                json.dump(report, f)
            with open(os.path.join(d, "freeze-20261125-111111.json"), "w") as f:
                f.write("{hỏng")
            self.assertEqual(fr.load_freeze_reports("ffff", d), [])
            found = fr.load_freeze_reports("0123456789abcdef0123456789abcdef", d)
            self.assertEqual(len(found), 1)
            text = fr.format_freeze(found[0])
            for part in ("TẮT MÁY", "thrashing", "92%", "12 MB", "chrome 5000 MB", "kswapd0", "Giây 60", "OOM giết chrome"):
                self.assertIn(part, text)


if __name__ == "__main__":
    unittest.main()
