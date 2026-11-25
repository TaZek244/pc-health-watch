import json
import os
import tempfile
import unittest

from healthwatch.collectors.smart import list_disks, parse_smart_json, read_smart


NVME = {"device": {"name": "/dev/nvme0n1"}, "model_name": "Fast NVMe", "smart_status": {"passed": True}, "temperature": {"current": 35}, "nvme_smart_health_information_log": {"critical_warning": 0, "percentage_used": 3, "media_errors": 0, "power_on_hours": 12, "unsafe_shutdowns": 1}}
SATA = {"device": {"name": "/dev/sda"}, "model_name": "Old SATA", "smart_status": {"passed": True}, "temperature": {"current": 40}, "ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 2}}, {"id": 9, "raw": {"value": 100}}, {"id": 197, "raw": {"value": 0}}, {"id": 198, "raw": {"value": 0}}]}}


class SmartTest(unittest.TestCase):
    def test_parse_nvme_and_sata(self):
        nvme = parse_smart_json(NVME)
        self.assertEqual(nvme["percent_used"], 3)
        self.assertEqual(nvme["problems"], [])
        sata = parse_smart_json(SATA)
        self.assertEqual(sata["reallocated"], 2)
        self.assertTrue(sata["problems"])

    def test_read_smart_ignores_exit_code_and_lists_disks(self):
        calls = []
        def runner(command):
            calls.append(command)
            return 2, json.dumps(NVME)
        self.assertEqual(read_smart("/dev/nvme0n1", use_sudo=False, runner=runner)["model"], "Fast NVMe")
        self.assertEqual(calls[0], ["smartctl", "-j", "-a", "/dev/nvme0n1"])
        with tempfile.TemporaryDirectory() as root:
            for name in ("sda", "loop0", "nvme0n1", "zram0", "dm-0"):
                os.makedirs(os.path.join(root, "sys/block", name))
            self.assertEqual(list_disks(root), ["/dev/nvme0n1", "/dev/sda"])


if __name__ == "__main__":
    unittest.main()
