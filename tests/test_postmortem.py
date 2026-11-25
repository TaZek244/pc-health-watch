import json
import os
import tempfile
import unittest

from healthwatch import postmortem


class PostmortemTest(unittest.TestCase):
    def test_list_boots_old_and_new_format(self):
        text = """IDX BOOT ID FIRST ENTRY LAST ENTRY
 -1 2f3c0000-1111-2222-3333-abcdef0000a1 Mon 2026-11-23 08:01:02 +07—Mon 2026-11-23 17:44:10 +07
  0 1234567890abcdef1234567890abcdef  Mon 2026-11-24 08:01:02 +07 - Mon 2026-11-24 17:44:10 +07  42
"""
        result = postmortem.list_boots(lambda command: (0, text))
        self.assertEqual(result[0]["index"], -1)
        self.assertEqual(result[0]["boot_id"], "2f3c0000111122223333abcdef0000a1")
        self.assertEqual(len(result), 2)

    def test_shutdown_cases(self):
        self.assertIsNone(postmortem.shutdown_was_clean(""))
        self.assertTrue(postmortem.shutdown_was_clean("x\nReached target Reboot"))
        self.assertFalse(postmortem.shutdown_was_clean("kernel panic\nfoo"))
        self.assertFalse(postmortem.shutdown_was_clean("PM: suspend entry"))

    def test_load_blackbox(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "blackbox-2026.jsonl")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "sample", "boot_id": "AA-BB", "ts": 2}) + "\n")
                stream.write("{broken\n")
                stream.write(json.dumps({"type": "sample", "boot_id": "ccdd", "ts": 1}) + "\n")
            records = postmortem.load_blackbox(directory, "aabb")
            self.assertEqual([record["ts"] for record in records], [2])

    def test_analyze_ram_disk_and_normal_hang(self):
        sample = {
            "type": "sample",
            "ts": 1000,
            "memory": {"used_percent": 97},
            "top": {"memory": [{"name": "chrome", "rss_mb": 900}]},
        }
        report = postmortem.analyze([sample], [], False)
        self.assertTrue(any("Hết RAM" in finding["text"] for finding in report["findings"]))
        report = postmortem.analyze(
            [{"type": "sample", "ts": 1}], [{"category": "disk", "severity": "critical", "summary": "I/O"}], False
        )
        self.assertTrue(any("Kernel báo lỗi ổ đĩa" in finding["text"] for finding in report["findings"]))
        report = postmortem.analyze([{"type": "sample", "ts": 1}], [], False)
        self.assertTrue(any("đơ đột ngột" in finding["text"] for finding in report["findings"]))

    def test_kernel_events_runner_and_build_report(self):
        def classify(line):
            return {"category": "disk", "severity": "critical", "summary": line}

        def runner(command):
            if command[:2] == ["journalctl", "--list-boots"]:
                return 0, " -1 aabbccdd00112233445566778899aabb Mon first  -  last\n"
            if command[1:3] == ["-b", "-1"]:
                return 0, "2026-11-24T10:00:00+07 kernel error"
            return 0, "2026-11-24T10:00:00+07 kernel error"

        events = postmortem.kernel_events_for_boot(-1, runner, classify)
        self.assertEqual(len(events), 1)
        report = postmortem.build_report(-1, tempfile.mkdtemp(), runner, classify)
        self.assertIn("findings", report)


class ReviewFixesTest(unittest.TestCase):
    def test_stopped_target_is_not_clean(self):
        text = "x\nsystemd[1]: Stopped target Sound Card.\npulseaudio: Shutting down.\nkernel: usb 1-1: disconnect"
        self.assertFalse(postmortem.shutdown_was_clean(text))

    def test_readonly_reported_once_with_mounts(self):
        samples = [
            {"type": "sample", "ts": t, "disks": [{"mount": "/", "readonly": True, "free_gb": 9}]} for t in (1, 2, 3)
        ]
        texts = [f["text"] for f in postmortem.analyze(samples, [], False)["findings"]]
        self.assertEqual(sum("read-only" in t for t in texts), 1)

    def test_kernel_finding_has_example(self):
        events = [{"category": "disk", "severity": "critical", "summary": "Lỗi I/O trên sda"}]
        texts = [f["text"] for f in postmortem.analyze([], events, False)["findings"]]
        self.assertTrue(any("Lỗi I/O trên sda" in t for t in texts))

    def test_build_report_uses_blackbox_events_and_stop(self):
        boot = "0123456789abcdef0123456789abcdef"

        def runner(command, timeout=5):
            if "--list-boots" in command:
                return 0, " -1 %s Mon 2026-11-23 08:01:02 +07—Mon 2026-11-23 17:44:10 +07\n" % boot
            return 1, ""

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "blackbox-2026-11-23.jsonl"), "w") as f:
                f.write(
                    json.dumps({"type": "start", "boot_id": "01234567-89ab-cdef-0123-456789abcdef", "ts": 1}) + "\n"
                )
                f.write(
                    json.dumps(
                        {
                            "type": "event",
                            "boot_id": boot,
                            "ts": 2,
                            "category": "gpu",
                            "severity": "critical",
                            "summary": "GPU rớt khỏi bus",
                        }
                    )
                    + "\n"
                )
            report = postmortem.build_report(-1, d, runner=runner, classify=lambda line: None)
        self.assertEqual(report["blackbox_records"], 2)
        self.assertFalse(report["clean"])  # có start, không có stop
        self.assertTrue(any("GPU" in f["text"] for f in report["findings"]))
        self.assertIn("BÁO CÁO", postmortem.format_report(report))


if __name__ == "__main__":
    unittest.main()
