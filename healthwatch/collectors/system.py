"""Thu thập các số liệu sức khỏe hệ thống Linux."""

import glob
import json
import os
import re
import time

from healthwatch import util


_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk"}
_PHYSICAL_DISK = re.compile(r"^(?:sd[a-z]+|nvme\d+n\d+|vd[a-z]+|mmcblk\d+)$")


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value):
    return None if value is None else round(float(value), 1)


class SystemCollector(object):
    def __init__(self, root="/", top_n=5, statvfs=os.statvfs):
        self.root = root
        self.top_n = top_n
        self.statvfs = statvfs
        self._cpu_prev = None
        self._io_prev = {}
        self._io_time = None
        self._proc_prev = {}
        self._cpu_count = 1
        self.clock = time.monotonic

    def _read(self, *parts):
        return util.read_text(util.proc_path(self.root, *parts), "")

    def _cpu(self):
        aggregate = None
        count = 0
        for line in self._read("proc/stat").splitlines():
            fields = line.split()
            if not fields or not fields[0].startswith("cpu"):
                continue
            if fields[0] == "cpu":
                try:
                    aggregate = [int(x) for x in fields[1:]]
                except ValueError:
                    aggregate = None
            elif re.match(r"^cpu\d+$", fields[0]):
                count += 1
        result = {"percent": None, "iowait_percent": None, "count": count or None,
                  "load1": None, "load5": None, "load15": None}
        load = self._read("proc/loadavg").split()
        for index, key in enumerate(("load1", "load5", "load15")):
            if index < len(load):
                result[key] = _round(_number(load[index]))
        if aggregate is not None and self._cpu_prev is not None:
            delta = [max(0, b - a) for a, b in zip(self._cpu_prev, aggregate)]
            total = sum(delta)
            idle = (delta[3] if len(delta) > 3 else 0) + (delta[4] if len(delta) > 4 else 0)
            wait = delta[4] if len(delta) > 4 else 0
            if total:
                result["percent"] = _round((total - idle) * 100.0 / total)
                result["iowait_percent"] = _round(wait * 100.0 / total)
        self._cpu_prev = aggregate
        return result

    def _memory(self):
        values = {}
        for line in self._read("proc/meminfo").splitlines():
            fields = line.split()
            if len(fields) >= 2:
                try:
                    values[fields[0].rstrip(":")] = int(fields[1])
                except ValueError:
                    pass
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        swap_total = values.get("SwapTotal")
        swap_free = values.get("SwapFree")
        used = None if total is None or not total else (total - (available or 0)) * 100.0 / total
        swap_used = None if swap_total is None or swap_free is None else swap_total - swap_free
        swap_pct = None if swap_used is None or not swap_total else swap_used * 100.0 / swap_total
        return {"total_mb": None if total is None else round(total / 1024.0),
                "available_mb": None if available is None else round(available / 1024.0),
                "used_percent": _round(used),
                "swap_total_mb": None if swap_total is None else round(swap_total / 1024.0),
                "swap_used_mb": None if swap_used is None else round(swap_used / 1024.0),
                "swap_used_percent": _round(swap_pct)}

    def _pressure(self):
        base = util.proc_path(self.root, "proc/pressure")
        if not os.path.isdir(base):
            return None
        output = {}
        for kind in ("cpu", "memory", "io"):
            text = util.read_text(os.path.join(base, kind), "")
            if not text:
                output[kind] = None
                continue
            item = {}
            for line in text.splitlines():
                fields = line.split()
                if not fields:
                    continue
                prefix = "some" if fields[0] == "some" else "full" if fields[0] == "full" else None
                if prefix is None:
                    continue
                for field in fields[1:]:
                    if "=" in field:
                        key, value = field.split("=", 1)
                        if key in ("avg10", "avg60"):
                            item[prefix + "_" + key] = _round(_number(value))
            output[kind] = item or None
        return output

    def _disks(self):
        text = self._read("proc/self/mounts") or self._read("proc/mounts")
        result = []
        for line in text.splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            device, mount, fstype, options = [x.replace("\\040", " ").replace("\\011", "\t") for x in fields[:4]]
            if fstype not in _FILESYSTEMS or mount.startswith("/snap/") or device.startswith("/dev/loop"):
                continue
            try:
                stat = self.statvfs(util.proc_path(self.root, mount))
                total = stat.f_blocks * stat.f_frsize
                free = stat.f_bavail * stat.f_frsize
                used = None if not total else (total - free) * 100.0 / total
                result.append({"mount": mount, "device": device, "fstype": fstype,
                               "used_percent": _round(used), "free_gb": _round(free / (1024.0 ** 3)),
                               "readonly": "ro" in options.split(",")})
            except OSError:
                pass
        return result

    def _diskstats(self):
        current = {}
        for line in self._read("proc/diskstats").splitlines():
            fields = line.split()
            if len(fields) < 14 or not _PHYSICAL_DISK.match(fields[2]):
                continue
            try:
                current[fields[2]] = [int(fields[3]), int(fields[5]), int(fields[6]), int(fields[7]), int(fields[9]), int(fields[10]), int(fields[12])]
            except ValueError:
                pass
        result = []
        now = self.clock()
        elapsed = None if self._io_time is None else now - self._io_time
        self._io_time = now
        for dev, values in current.items():
            previous = self._io_prev.get(dev)
            if previous and elapsed and elapsed > 0:
                delta = [max(0, b - a) for a, b in zip(previous, values)]
                reads, sectors_read, read_ticks, writes, sectors_write, write_ticks, io_ticks = delta
                operations = reads + writes
                # io_ticks tính bằng ms → % thời gian ổ bận trong khoảng elapsed giây
                result.append({"device": dev, "util_percent": _round(min(100.0, io_ticks / 10.0 / elapsed)),
                               "read_mb_s": _round(sectors_read * 512.0 / 1024 / 1024 / elapsed),
                               "write_mb_s": _round(sectors_write * 512.0 / 1024 / 1024 / elapsed),
                               "await_ms": _round((read_ticks + write_ticks) / float(operations) if operations else 0)})
        self._io_prev = current
        return result

    def _thermal(self):
        result = []
        for directory in glob.glob(util.proc_path(self.root, "sys/class/hwmon/hwmon*")):
            chip = util.read_text(os.path.join(directory, "name"), "") or os.path.basename(directory)
            for input_path in glob.glob(os.path.join(directory, "temp*_input")):
                stem = input_path[:-6]
                raw = _number(util.read_text(input_path, ""))
                temp = None if raw is None else raw / 1000.0
                if temp is None or temp <= 0 or temp > 150:
                    continue
                result.append({"chip": chip, "label": util.read_text(stem + "_label", "") or os.path.basename(stem),
                               "temp_c": _round(temp), "high_c": _round((_number(util.read_text(stem + "_max", "")) or 0) / 1000.0) or None,
                               "crit_c": _round((_number(util.read_text(stem + "_crit", "")) or 0) / 1000.0) or None})
        if result:
            return result
        for directory in glob.glob(util.proc_path(self.root, "sys/class/thermal/thermal_zone*")):
            temp = _number(util.read_text(os.path.join(directory, "temp"), ""))
            temp = None if temp is None else temp / 1000.0
            if temp is not None and 0 < temp <= 150:
                result.append({"chip": util.read_text(os.path.join(directory, "type"), "") or os.path.basename(directory),
                               "label": util.read_text(os.path.join(directory, "type"), "") or os.path.basename(directory),
                               "temp_c": _round(temp), "high_c": None, "crit_c": None})
        return result

    def _processes(self):
        current = {}
        for path in glob.glob(util.proc_path(self.root, "proc/[0-9]*")):
            pid_text = os.path.basename(path)
            if not pid_text.isdigit():
                continue
            try:
                stat = util.read_text(os.path.join(path, "stat"), "")
                close = stat.rfind(")")
                if close < 0:
                    continue
                name = stat[stat.find("(") + 1:close]
                fields = stat[close + 2:].split()
                utime, stime = int(fields[11]), int(fields[12])
                status = util.read_text(os.path.join(path, "status"), "")
                rss = 0
                for line in status.splitlines():
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1])
                        break
                current[int(pid_text)] = (name, rss, utime + stime)
            except (OSError, ValueError, IndexError):
                pass
        return current

    def _top(self, cpu_delta):
        current = self._processes()
        memory = sorted(({"pid": pid, "name": x[0], "rss_mb": _round(x[1] / 1024.0)} for pid, x in current.items()), key=lambda x: x["rss_mb"], reverse=True)[:self.top_n]
        cpu = []
        for pid, value in current.items():
            old = self._proc_prev.get(pid)
            if old is not None and cpu_delta:
                cpu.append({"pid": pid, "name": value[0], "cpu_percent": _round(max(0, value[2] - old[1]) * 100.0 * (self._cpu_count or 1) / cpu_delta)})
        cpu.sort(key=lambda x: x["cpu_percent"], reverse=True)
        self._proc_prev = {pid: (value[0], value[2]) for pid, value in current.items()}
        return {"memory": memory, "cpu": cpu[:self.top_n]}

    def sample(self):
        before = self._cpu_prev
        cpu = self._cpu()
        self._cpu_count = cpu["count"] or 1
        cpu_delta = None
        if before is not None and self._cpu_prev is not None:
            cpu_delta = sum(max(0, b - a) for a, b in zip(before, self._cpu_prev))
        return {"ts": time.time(), "cpu": cpu, "memory": self._memory(), "pressure": self._pressure(),
                "disks": self._disks(), "io": self._diskstats(), "thermal": self._thermal(), "top": self._top(cpu_delta)}


def main():
    collector = SystemCollector()
    print(json.dumps(collector.sample(), ensure_ascii=False))
    time.sleep(1)
    print(json.dumps(collector.sample(), ensure_ascii=False))


if __name__ == "__main__":
    main()
