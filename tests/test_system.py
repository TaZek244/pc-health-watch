import os
import tempfile
import unittest

from healthwatch.collectors.system import SystemCollector


class FakeStat:
    f_blocks = 1000
    f_bavail = 250
    f_frsize = 1024 * 1024


def write_file(root, name, content):
    path = os.path.join(root, name)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w") as stream:
        stream.write(content)


class SystemCollectorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = self.temp.name
        write_file(root, "proc/stat", "cpu  100 10 20 300 10 0 0 0\ncpu0 50 5 10 150 5\ncpu1 50 5 10 150 5\n")
        write_file(root, "proc/loadavg", "0.9 0.7 0.5 1/100 1\n")
        write_file(
            root,
            "proc/meminfo",
            "MemTotal:       102400 kB\nMemAvailable:    51200 kB\nSwapTotal:       2048 kB\nSwapFree:        1024 kB\n",
        )
        write_file(root, "proc/pressure/cpu", "some avg10=1.20 avg60=0.50 avg300=0.10 total=1\n")
        write_file(
            root,
            "proc/pressure/memory",
            "some avg10=2.00 avg60=1.00 avg300=0.1 total=1\nfull avg10=0.20 avg60=0.10 avg300=0.1 total=1\n",
        )
        write_file(
            root,
            "proc/pressure/io",
            "some avg10=3.00 avg60=2.00 avg300=0.1 total=1\nfull avg10=0.40 avg60=0.20 avg300=0.1 total=1\n",
        )
        write_file(
            root, "proc/self/mounts", "/dev/nvme0n1p2 / ext4 rw,relatime 0 0\n/dev/loop0 /snap/x squashfs ro 0 0\n"
        )
        write_file(
            root,
            "proc/diskstats",
            "259 0 nvme0n1 10 0 100 20 5 0 200 30 0 40 0\n259 1 nvme0n1p2 1 0 2 3 0 0 0 0 0 0 0\n",
        )
        write_file(root, "sys/class/hwmon/hwmon0/name", "coretemp\n")
        write_file(root, "sys/class/hwmon/hwmon0/temp1_input", "52000\n")
        write_file(root, "sys/class/hwmon/hwmon0/temp1_label", "Package id 0\n")
        write_file(root, "sys/class/hwmon/hwmon0/temp1_max", "80000\n")
        write_file(root, "sys/class/hwmon/hwmon0/temp1_crit", "100000\n")
        stat = "123 (Web Content) S 1 2 3 4 5 6 7 8 9 10 20 0 0 0 0 0 0 0 0\n"
        write_file(root, "proc/123/stat", stat)
        write_file(root, "proc/123/status", "Name:\tWeb Content\nVmRSS:\t819200 kB\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_and_deltas(self):
        collector = SystemCollector(self.temp.name, statvfs=lambda path: FakeStat())
        clock = iter([100.0, 105.0])
        collector.clock = lambda: next(clock)
        first = collector.sample()
        self.assertIsNone(first["cpu"]["percent"])
        self.assertEqual(first["top"]["memory"][0]["name"], "Web Content")
        write_file(self.temp.name, "proc/stat", "cpu  110 10 25 320 15 0 0 0\ncpu0 55 5 12 160 7\ncpu1 55 5 13 160 8\n")
        write_file(self.temp.name, "proc/diskstats", "259 0 nvme0n1 20 0 300 30 8 0 400 50 0 70 0\n")
        second = collector.sample()
        self.assertEqual(second["cpu"]["count"], 2)
        self.assertIsNotNone(second["cpu"]["percent"])
        self.assertEqual(second["disks"][0]["readonly"], False)
        io = second["io"][0]
        self.assertEqual(io["device"], "nvme0n1")
        # 5 giây: io_ticks +30 ms → 0.6%; 200 sector đọc = 100 KiB → 0.02 MB/s; trễ (10+20)/(10+3) ms
        self.assertEqual(io["util_percent"], 0.6)
        self.assertEqual(io["await_ms"], round(30 / 13.0, 1))
        self.assertEqual(len(second["io"]), 1)  # partition bị bỏ
        self.assertEqual(second["cpu"]["percent"], 37.5)  # (10+5)/40
        self.assertEqual(second["cpu"]["iowait_percent"], 12.5)
        self.assertEqual(second["memory"]["used_percent"], 50.0)
        self.assertEqual(second["pressure"]["memory"]["full_avg10"], 0.2)
        self.assertEqual([d["mount"] for d in second["disks"]], ["/"])
        self.assertEqual(second["top"]["cpu"][0]["name"], "Web Content")
        self.assertEqual(second["thermal"][0]["temp_c"], 52.0)


if __name__ == "__main__":
    unittest.main()
