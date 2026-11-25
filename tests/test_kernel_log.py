"""Kiểm thử nhận diện và theo dõi kernel log."""

import sys
import threading
import unittest

from healthwatch.collectors.kernel_log import (
    Deduper,
    KernelLogFollower,
    classify,
    scan_lines,
)


class TestClassifyDisk(unittest.TestCase):
    def test_disk_critical(self):
        cases = [
            "blk_update_request: I/O error, dev sda, sector 12345",
            "I/O error, dev nvme0n1, sector 12345",
            "Buffer I/O error on dev sda1, logical block 0",
            "nvme nvme0: I/O 123 QID 4 timeout",
            "nvme nvme0: controller is down; will reset",
            "nvme nvme0: Device not ready",
            "ata1.00: exception Emask 0x0 SAct 0x0 SErr 0x0 action 0x0",
            "ata1: hard resetting link",
            "ata1.00: failed command: READ FPDMA QUEUED",
            "sd 0:0:0:0: [sda] tag#0 FAILED Result: hostbyte=DID_OK",
            "blk_update_request: critical medium error, dev sda",
        ]
        for line in cases:
            with self.subTest(line=line):
                event = classify(line)
                self.assertIsNotNone(event)
                self.assertEqual(event["category"], "disk")
                self.assertEqual(event["severity"], "critical")
                self.assertEqual(event["line"], line.strip())


class TestClassifyFilesystem(unittest.TestCase):
    def test_filesystem_critical(self):
        cases = [
            "EXT4-fs error (device nvme0n1p2): ext4_lookup: deleted inode",
            "EXT4-fs (sda1): Remounting filesystem read-only",
            "Aborting journal on device sda1-8.",
            "BTRFS error (device sda1): parent transid verify failed",
            "XFS (sda1): Corruption detected. Unmount and run xfs_repair",
        ]
        for line in cases:
            with self.subTest(line=line):
                event = classify(line)
                self.assertIsNotNone(event)
                self.assertEqual(event["category"], "filesystem")
                self.assertEqual(event["severity"], "critical")


class TestClassifyMemory(unittest.TestCase):
    def test_memory_cases(self):
        cases = [
            (
                "Out of memory: Killed process 1234 (chrome) total-vm:8000000kB",
                "critical",
            ),
            ("oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null)", "critical"),
            (
                "page allocation failure: order:0, mode:0x14000c0",
                "warning",
            ),
        ]
        for line, severity in cases:
            with self.subTest(line=line):
                event = classify(line)
                self.assertIsNotNone(event)
                self.assertEqual(event["category"], "memory")
                self.assertEqual(event["severity"], severity)

    def test_oom_summary_contains_process_name(self):
        event = classify("Out of memory: Killed process 1234 (chrome)")
        self.assertIsNotNone(event)
        self.assertIn("chrome", event["summary"])


class TestClassifyHang(unittest.TestCase):
    def test_hang_critical(self):
        cases = [
            "INFO: task kworker/0:1:123 blocked for more than 120 seconds.",
            "watchdog: BUG: soft lockup - CPU#3 stuck for 22s! [swapper/3:0]",
            "rcu: INFO: rcu_sched self-detected stall on CPU",
            "NMI watchdog: Watchdog detected hard LOCKUP on cpu 2",
            "Kernel panic - not syncing: Fatal exception",
            "BUG: unable to handle page fault for address: ffffffff",
            "general protection fault, probably for non-canonical address",
        ]
        for line in cases:
            with self.subTest(line=line):
                event = classify(line)
                self.assertIsNotNone(event)
                self.assertEqual(event["category"], "hang")
                self.assertEqual(event["severity"], "critical")


class TestClassifyGpu(unittest.TestCase):
    def test_gpu_critical(self):
        cases = [
            "NVRM: Xid (PCI:0000:01:00): 79, pid=1234, engine 0",
            "NVRM: GPU has fallen off the bus.",
            "amdgpu: GPU fault detected",
            "[drm:amdgpu_job_timedout] *ERROR* ring gfx timeout, seq=1",
            "i915 0000:00:02.0: [drm] GPU HANG: ecode 12:1:85dffefc",
            "nouveau 0000:01:00.0: fifo: SCHED_ERROR 0a [CTXSW_TIMEOUT]",
        ]
        for line in cases:
            with self.subTest(line=line):
                event = classify(line)
                self.assertIsNotNone(event)
                self.assertEqual(event["category"], "gpu")
                self.assertEqual(event["severity"], "critical")

    def test_gpu_generic_drm_error_is_warning(self):
        event = classify("[drm:some_driver] *ERROR* something went wrong")
        self.assertIsNotNone(event)
        self.assertEqual(event["category"], "gpu")
        self.assertEqual(event["severity"], "warning")


class TestClassifyThermal(unittest.TestCase):
    def test_thermal_cases(self):
        cases = [
            (
                "CPU0: Core temperature above threshold, cpu clock throttled",
                "critical",
            ),
            (
                "critical temperature reached (100 C), shutting down",
                "critical",
            ),
            (
                "Package temperature above threshold, cpu clock throttled",
                "warning",
            ),
        ]
        for line, severity in cases:
            with self.subTest(line=line):
                event = classify(line)
                self.assertIsNotNone(event)
                self.assertEqual(event["category"], "thermal")
                self.assertEqual(event["severity"], severity)


class TestClassifyPower(unittest.TestCase):
    def test_power_cases(self):
        cases = [
            (
                "PCIe Bus Error: severity=Corrected, type=Physical Layer",
                "warning",
            ),
            (
                "PCIe Bus Error: severity=Uncorrected (Non-Fatal), type=Data Link Layer",
                "critical",
            ),
            ("mce: [Hardware Error]: Machine check events logged", "critical"),
            ("Machine check events logged", "critical"),
        ]
        for line, severity in cases:
            with self.subTest(line=line):
                event = classify(line)
                self.assertIsNotNone(event)
                self.assertEqual(event["category"], "power")
                self.assertEqual(event["severity"], severity)


class TestClassifyNormal(unittest.TestCase):
    def test_normal_lines_return_none(self):
        lines = [
            "usb 1-1: new high-speed USB device number 2 using xhci_hcd",
            "wlp2s0: associated",
            "EXT4-fs (nvme0n1p2): mounted filesystem with ordered data mode. Opts: (null)",
            "Linux version 5.15.0-91-generic (buildd@lcy02-amd64-045)",
            "audit: type=1400 audit(1700000000.000:42): apparmor=\"STATUS\"",
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assertIsNone(classify(line))

    def test_timestamp_prefix_does_not_matter(self):
        line = "2024-01-01T10:00:00+0700 host kernel: nvme nvme0: I/O 1 QID 2 timeout"
        event = classify(line)
        self.assertIsNotNone(event)
        self.assertEqual(event["category"], "disk")


class TestScanLines(unittest.TestCase):
    def test_scan_lines_skips_none(self):
        lines = [
            "usb 1-1: new high-speed USB device number 2",
            "nvme nvme0: I/O 1 QID 2 timeout",
            "wlp2s0: associated",
            "Kernel panic - not syncing: Fatal exception",
        ]
        events = scan_lines(lines)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["category"], "disk")
        self.assertEqual(events[1]["category"], "hang")


class TestDeduper(unittest.TestCase):
    def test_duplicate_within_window(self):
        deduper = Deduper(window=300)
        event = {
            "category": "disk",
            "severity": "critical",
            "summary": "Lỗi I/O trên sda",
        }
        self.assertFalse(deduper.seen(event, 1000.0))
        self.assertTrue(deduper.seen(event, 1100.0))
        self.assertTrue(deduper.seen(event, 1200.0))

    def test_different_summary_is_not_duplicate(self):
        deduper = Deduper(window=300)
        first = {"category": "disk", "summary": "Lỗi I/O trên sda"}
        second = {"category": "disk", "summary": "Lỗi I/O trên sdb"}
        self.assertFalse(deduper.seen(first, 1000.0))
        self.assertFalse(deduper.seen(second, 1001.0))

    def test_expires_after_window(self):
        deduper = Deduper(window=300)
        event = {"category": "disk", "summary": "Lỗi I/O trên sda"}
        self.assertFalse(deduper.seen(event, 1000.0))
        self.assertTrue(deduper.seen(event, 1100.0))
        self.assertFalse(deduper.seen(event, 1400.0))


class TestKernelLogFollower(unittest.TestCase):
    def test_follower_with_fake_command(self):
        # Lệnh giả bằng Python để không phụ thuộc journalctl/dmesg thật.
        script = (
            "import sys; "
            "sys.stdout.write('a\\n"
            "nvme nvme0: I/O 1 QID 2 timeout\\n'); "
            "sys.stdout.flush()"
        )
        events = []
        received = threading.Event()

        def callback(event):
            events.append(event)
            received.set()

        follower = KernelLogFollower(
            callback,
            command=[sys.executable, "-c", script],
        )
        follower.start()
        try:
            self.assertTrue(
                received.wait(5.0),
                "không nhận được sự kiện từ lệnh giả",
            )
        finally:
            follower.stop()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["category"], "disk")
        self.assertEqual(events[0]["severity"], "critical")
        self.assertIn("nvme0", events[0]["summary"])


if __name__ == "__main__":
    unittest.main()
