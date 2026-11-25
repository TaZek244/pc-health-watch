"""Freeze watchdog — chạy bằng root, phát hiện máy đơ và tự tắt/khởi động lại an toàn sau N giây.

Nguyên tắc để vẫn sống khi máy đã đơ:
- mlockall: toàn bộ bộ nhớ tiến trình nằm cố định trong RAM, không bao giờ bị swap ra.
- Mọi module được import TRƯỚC khi vào vòng lặp; trong vòng lặp KHÔNG đọc đĩa, KHÔNG fork/exec
  (exec phải đọc file chương trình từ SSD — SSD treo là kẹt luôn).
- Chỉ đọc /proc, /sys, /run (nằm trong RAM), /dev/kmsg; ghi /proc/sysrq-trigger và /dev/kmsg.
- Ghi báo cáo xuống đĩa trong luồng riêng có timeout: ổ treo cũng không chặn việc tắt máy.
- stderr đặt non-blocking: journald đứng thì bỏ log, không đứng theo.

Cấu hình (root-owned): /etc/pc-health-watch/watchdog.json — xem DEFAULTS.
"""

import collections
import ctypes
import errno
import fcntl
import json
import os
import queue
import signal
import sys
import threading
import time

CONFIG_PATH = os.environ.get("HEALTHWATCH_WATCHDOG_CONFIG", "/etc/pc-health-watch/watchdog.json")
REPORT_DIR = "/var/lib/pc-health-watch"
STATUS_PATH = "/run/pc-health-watch/watchdog-status.json"
KEEP_REPORTS = 30

DEFAULTS = {
    # off = tắt; log = chỉ ghi "lẽ ra đã tắt máy" (chạy thử); act = làm thật
    "mode": "log",
    "action": "poweroff",  # poweroff | reboot
    "action_after_seconds": 300,  # tính từ lúc dấu hiệu đơ BẮT ĐẦU
    "detect_seconds": 30,  # dấu hiệu phải kéo dài bấy lâu mới coi là đơ
    "recover_seconds": 15,  # hết dấu hiệu liên tục bấy lâu thì coi là đã hồi phục
    "oom_kill_after_seconds": 60,  # thrashing RAM: SysRq-f sau bấy lâu (0 = không)
    "memory_full_stall_percent": 50,  # % thời gian MỌI tác vụ chờ RAM (cửa sổ 10 s)
    "io_full_stall_percent": 80,  # % thời gian MỌI tác vụ chờ ổ đĩa
    "gui_hang_seconds": 45,  # thăm dò giao diện timeout liên tục bấy lâu
}

SIGNAL_TEXT = {
    "memory": "hết RAM — hệ thống thrashing",
    "io": "ổ đĩa treo — mọi tác vụ chờ I/O",
    "gui": "giao diện (gnome-shell/X) không phản hồi",
}


def load_config(path=CONFIG_PATH):
    config = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, ValueError):
        return config
    if not isinstance(raw, dict):
        return config
    if raw.get("mode") in ("off", "log", "act"):
        config["mode"] = raw["mode"]
    if raw.get("action") in ("poweroff", "reboot"):
        config["action"] = raw["action"]
    for key in DEFAULTS:
        value = raw.get(key)
        if isinstance(DEFAULTS[key], int) and isinstance(value, (int, float)) and not isinstance(value, bool):
            if value >= 0:
                config[key] = value
    # chặn cấu hình nguy hiểm: không cho tắt máy sớm hơn 60 s hoặc phát hiện dưới 10 s
    config["action_after_seconds"] = max(60, config["action_after_seconds"])
    config["detect_seconds"] = max(10, config["detect_seconds"])
    config["recover_seconds"] = max(5, config["recover_seconds"])
    return config


# ---------------------------------------------------------------- đo đạc (chỉ /proc, /sys, /run)


def _read(path):
    try:
        with open(path, encoding="ascii", errors="replace") as stream:
            return stream.read()
    except OSError:
        return ""


class PsiStall:
    """Tỉ lệ nghẽn thật trên cửa sổ trượt, tính từ bộ đếm total= (µs) của /proc/pressure/*.

    Phản ứng nhanh và chính xác hơn avg10 (avg10 là trung bình trượt hàm mũ, trễ vài giây).
    """

    def __init__(self, root="/", window=10.0):
        self.root = root
        self.window = window
        self.history = {key: collections.deque() for key in ("memory", "io")}

    def _totals(self, kind):
        out = {}
        for line in _read(os.path.join(self.root, "proc/pressure", kind)).splitlines():
            parts = line.split()
            if parts and parts[0] in ("some", "full"):
                for field in parts[1:]:
                    if field.startswith("total="):
                        try:
                            out[parts[0]] = int(field[6:])
                        except ValueError:
                            pass
        return out

    def sample(self, now):
        for kind, history in self.history.items():
            totals = self._totals(kind)
            if totals:
                history.append((now, totals))
            while len(history) > 2 and now - history[1][0] >= self.window:
                history.popleft()

    def percent(self, kind, level="full"):
        history = self.history[kind]
        if len(history) < 2:
            return None
        (t0, a), (t1, b) = history[0], history[-1]
        if t1 - t0 < 1 or level not in a or level not in b:
            return None
        return max(0.0, min(100.0, (b[level] - a[level]) / ((t1 - t0) * 1e6) * 100.0))


def memory_info(root="/"):
    values = {}
    for line in _read(os.path.join(root, "proc/meminfo")).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                values[parts[0].rstrip(":")] = int(parts[1])
            except ValueError:
                pass
    swap_used = values.get("SwapTotal", 0) - values.get("SwapFree", 0)
    return {
        "available_mb": round(values.get("MemAvailable", 0) / 1024.0),
        "total_mb": round(values.get("MemTotal", 0) / 1024.0),
        "swap_used_mb": round(swap_used / 1024.0),
    }


def process_summary(root="/", top_n=5):
    """Top RAM + các tiến trình kẹt ở trạng thái D (chờ I/O không ngắt được) — bằng chứng treo I/O."""
    procs, stuck = [], []
    try:
        pids = [p for p in os.listdir(os.path.join(root, "proc")) if p.isdigit()]
    except OSError:
        return {"top_memory": [], "d_state": [], "d_state_count": 0}
    for pid in pids:
        stat = _read(os.path.join(root, "proc", pid, "stat"))
        close = stat.rfind(")")
        if close < 0:
            continue
        name = stat[stat.find("(") + 1 : close]
        fields = stat[close + 2 :].split()
        if not fields:
            continue
        try:
            rss_pages = int(fields[21])
        except (IndexError, ValueError):
            rss_pages = 0
        procs.append((rss_pages, int(pid), name))
        if fields[0] == "D":
            stuck.append({"pid": int(pid), "name": name})
    page_mb = os.sysconf("SC_PAGE_SIZE") / 1048576.0
    procs.sort(reverse=True)
    return {
        "top_memory": [{"pid": pid, "name": name, "rss_mb": round(rss * page_mb)} for rss, pid, name in procs[:top_n]],
        "d_state": stuck[:10],
        "d_state_count": len(stuck),
    }


class Kmsg:
    """Đọc/ghi log kernel qua /dev/kmsg (nằm trong RAM, không chạm đĩa)."""

    def __init__(self, classify=None):
        self.classify = classify
        self.events = collections.deque(maxlen=50)
        self.read_fd = self.write_fd = None
        try:
            self.read_fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
            os.lseek(self.read_fd, 0, os.SEEK_END)  # chỉ đọc sự kiện mới
        except OSError:
            self.read_fd = None
        try:
            self.write_fd = os.open("/dev/kmsg", os.O_WRONLY)
        except OSError:
            self.write_fd = None

    def poll(self, now):
        if self.read_fd is None:
            return
        for _ in range(500):
            try:
                raw = os.read(self.read_fd, 8192)
            except BlockingIOError:
                return
            except OSError as error:
                if error.errno == errno.EPIPE:  # bộ đệm vòng bị ghi đè — đọc tiếp từ bản ghi kế
                    continue
                return
            if not raw:
                return
            text = raw.decode("utf-8", "replace")
            message = text.split(";", 1)[1].strip() if ";" in text else text.strip()
            if self.classify and "pc-health-watch" not in message:
                try:
                    event = self.classify(message)
                except Exception:
                    event = None
                if event:
                    event = dict(event, mono=round(now, 1), ts=round(time.time(), 1))
                    self.events.append(event)

    def write(self, message, level=4):
        if self.write_fd is None:
            return
        try:
            os.write(self.write_fd, f"<{level}>pc-health-watch: {message}".encode()[:1000])
        except OSError:
            pass


# ---------------------------------------------------------------- logic quyết định (thuần, test được)


class FreezeDetector:
    """State machine: tín hiệu mỗi giây → danh sách việc cần làm.

    Việc trả về: ("start", reasons) ("oom_kill", n) ("report", None) ("action", None) ("recovered", duration)
    """

    def __init__(self, config):
        self.config = config
        self.since = {}
        self.last_episode = None
        self.reset()

    def reset(self):
        self.freeze_start = None
        self.reasons = set()
        self.clear_since = None
        self.oom_kills = 0
        self.report_done = False
        self.action_done = False

    @property
    def frozen(self):
        return self.freeze_start is not None

    def elapsed(self, now):
        return 0.0 if self.freeze_start is None else now - self.freeze_start

    def update(self, signals, now):
        cfg = self.config
        out = []
        for name, on in signals.items():
            if on:
                self.since.setdefault(name, now)
            else:
                self.since.pop(name, None)
        sustained = [n for n, s in self.since.items() if now - s >= cfg["detect_seconds"]]
        if self.freeze_start is None:
            if sustained:
                # đồng hồ tắt máy tính từ lúc dấu hiệu bắt đầu, không phải lúc xác nhận
                self.freeze_start = min(self.since[n] for n in sustained)
                self.reasons = set(sustained)
                out.append(("start", sorted(self.reasons)))
            return out
        self.reasons.update(sustained)
        if any(signals.values()):
            self.clear_since = None
        else:
            if self.clear_since is None:
                self.clear_since = now
            if now - self.clear_since >= cfg["recover_seconds"]:
                duration = round(self.clear_since - self.freeze_start, 1)
                self.last_episode = {"duration": duration, "reasons": sorted(self.reasons)}
                out.append(("recovered", duration))
                self.reset()
                return out
        elapsed = now - self.freeze_start
        oom_after = cfg["oom_kill_after_seconds"]
        if "memory" in self.reasons and oom_after > 0 and self.oom_kills < 2:
            if elapsed >= oom_after * (self.oom_kills + 1):
                self.oom_kills += 1
                out.append(("oom_kill", self.oom_kills))
        if not self.report_done and elapsed >= cfg["action_after_seconds"] - 20:
            self.report_done = True
            out.append(("report", None))
        if not self.action_done and elapsed >= cfg["action_after_seconds"]:
            self.action_done = True
            out.append(("action", None))
        return out


# ---------------------------------------------------------------- watchdog


def sysrq(key, path="/proc/sysrq-trigger"):
    """Root ghi /proc/sysrq-trigger luôn được, không phụ thuộc sysctl kernel.sysrq."""
    try:
        with open(path, "w") as stream:
            stream.write(key)
        return True
    except OSError:
        return False


def _boot_id():
    return _read("/proc/sys/kernel/random/boot_id").strip()


def _write_json_atomic(path, data, fsync=True):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=1)
        stream.flush()
        if fsync:
            os.fsync(stream.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


class ReportWriter:
    """Một luồng ghi đĩa tạo sẵn từ lúc khởi động, nhận việc qua hàng đợi."""

    def __init__(self):
        self.jobs = queue.Queue(maxsize=2)
        self.thread = threading.Thread(target=self._loop, name="report-writer", daemon=True)
        self.thread.start()

    def _loop(self):
        while True:
            path, report, done = self.jobs.get()
            try:
                _write_json_atomic(path, report)
            except (OSError, TypeError, ValueError):
                pass
            done.set()

    def save(self, path, report, timeout):
        done = threading.Event()
        try:
            self.jobs.put_nowait((path, report, done))
        except queue.Full:  # lần ghi trước còn kẹt trên ổ treo
            return False
        return done.wait(timeout)


class Watchdog:
    def __init__(self, config, root="/", sysrq_fn=sysrq, kmsg=None, heartbeats=None, sleep=time.sleep, log=None):
        self.config = config
        self.root = root
        self.sysrq = sysrq_fn
        self.kmsg = kmsg
        self.heartbeats = heartbeats or (lambda: [])
        self.sleep = sleep
        self.log = log or (lambda message: None)
        self.psi = PsiStall(root)
        self.detector = FreezeDetector(config)
        self.samples = collections.deque(maxlen=900)
        self.processes = None
        self.last_proc_scan = -1e9
        self.gui_hung = None
        self.actions = []  # lịch sử việc đã làm trong đợt đơ hiện tại
        self.stop_event = threading.Event()
        self._writer = None

    # -- tín hiệu
    def signals(self, now):
        cfg = self.config
        mem = self.psi.percent("memory", "full")
        io = self.psi.percent("io", "full")
        gui = False
        if self.gui_hung is not None:
            for heartbeat in self.heartbeats():
                try:
                    if self.gui_hung(heartbeat, now, cfg["gui_hang_seconds"]):
                        gui = True
                except Exception:
                    pass
        return {
            "memory": mem is not None and mem >= cfg["memory_full_stall_percent"],
            "io": io is not None and io >= cfg["io_full_stall_percent"],
            "gui": gui,
        }, {"memory_full": mem, "io_full": io}

    def note(self, message, level=4):
        self.log(message)
        if self.kmsg is not None:
            self.kmsg.write(message, level)

    # -- báo cáo
    def build_report(self, now, outcome, duration=None, reasons=None):
        duration = self.detector.elapsed(now) if duration is None else duration
        reasons = sorted(self.detector.reasons) if reasons is None else reasons
        return {
            "version": 1,
            "boot_id": _boot_id(),
            "outcome": outcome,  # action_poweroff | action_reboot | would_act | recovered
            "mode": self.config["mode"],
            "started_ts": round(time.time() - duration, 1),
            "duration_seconds": round(duration, 1),
            "reasons": reasons,
            "reason_text": [SIGNAL_TEXT.get(r, r) for r in reasons],
            "actions": list(self.actions),
            "processes": self.processes,
            "kernel_events": list(self.kmsg.events) if self.kmsg is not None else [],
            "samples": list(self.samples)[-300:],
            "config": self.config,
        }

    def save_report(self, report, timeout=10.0):
        """Giao cho luồng ghi tạo sẵn; ổ treo thì bỏ qua sau `timeout` giây.

        Không tạo luồng mới lúc máy đang đơ: pthread_create phải cấp và khóa (mlockall) stack mới,
        dễ thất bại hoặc kẹt khi RAM đã cạn.
        """
        if self._writer is None:
            self._writer = ReportWriter()
        name = "freeze-{}.json".format(time.strftime("%Y%m%d-%H%M%S"))
        return self._writer.save(os.path.join(REPORT_DIR, name), report, timeout)

    def warm_up(self):
        """Chạy trước mọi đường code dùng lúc đơ khi máy còn khỏe: bộ nhớ được cấp + khóa sẵn,
        không phải xin trang mới đúng lúc RAM cạn."""
        if self._writer is None:
            self._writer = ReportWriter()
        self.processes = process_summary(self.root)
        memory_info(self.root)
        json.dumps(self.build_report(time.monotonic(), "warmup"), ensure_ascii=False)
        self.processes = None

    # -- một vòng
    def tick(self, now):
        self.psi.sample(now)
        if self.kmsg is not None:
            self.kmsg.poll(now)
        signals, values = self.signals(now)
        frozen = self.detector.frozen or any(signals.values())
        if frozen and now - self.last_proc_scan >= 10:
            self.processes = process_summary(self.root)
            self.last_proc_scan = now
        if frozen:
            sample = dict(
                values, mono=round(now, 1), ts=round(time.time(), 1), **{"sig_" + k: v for k, v in signals.items()}
            )
            sample.update(memory_info(self.root))
            if self.processes:
                sample["d_state_count"] = self.processes.get("d_state_count")
            self.samples.append(sample)
        for kind, arg in self.detector.update(signals, now):
            self.handle(kind, arg, now)
        return signals

    def handle(self, kind, arg, now):
        cfg = self.config
        act = cfg["mode"] == "act"
        if kind == "start":
            self.actions = [{"t": 0, "do": "detect", "reasons": arg}]
            self.note(
                "nghi máy đơ ({}); sẽ {} sau {} s nếu không hồi phục [mode={}]".format(
                    ", ".join(SIGNAL_TEXT.get(r, r) for r in arg),
                    "tắt máy" if cfg["action"] == "poweroff" else "khởi động lại",
                    int(cfg["action_after_seconds"] - self.detector.elapsed(now)),
                    cfg["mode"],
                ),
                level=3,
            )
        elif kind == "oom_kill":
            t = round(self.detector.elapsed(now))
            if act:
                self.note(f"thrashing RAM {t} s — gọi OOM killer của kernel (SysRq-f) lần {arg}", level=3)
                self.sysrq("f")
            else:
                self.note(f"[log] lẽ ra đã gọi OOM killer (SysRq-f) lần {arg}")
            self.actions.append({"t": t, "do": "oom_kill" if act else "would_oom_kill"})
        elif kind == "report":
            outcome = ("action_" + cfg["action"]) if act else "would_act"
            self.actions.append({"t": round(self.detector.elapsed(now)), "do": "report"})
            ok = self.save_report(self.build_report(now, outcome))
            self.note("đã ghi báo cáo đóng băng" if ok else "không ghi được báo cáo (ổ đĩa treo?)")
        elif kind == "action":
            if not act:
                self.note(
                    "[log] máy đơ {} s — lẽ ra đã {} (mode=log, không làm gì)".format(
                        round(self.detector.elapsed(now)), cfg["action"]
                    )
                )
                return
            self.emergency_stop()
        elif kind == "recovered":
            self.note(f"máy đã hồi phục sau {arg} s đơ", level=5)
            self.actions.append({"t": arg, "do": "recovered"})
            if arg >= self.config["detect_seconds"]:
                episode = self.detector.last_episode or {}
                self.save_report(self.build_report(now, "recovered", arg, episode.get("reasons", [])))
            self.samples.clear()

    def emergency_stop(self):
        key = "o" if self.config["action"] == "poweroff" else "b"
        self.note(
            "máy đơ quá lâu — sync, remount read-only rồi {} (SysRq s,u,{})".format(
                "tắt máy" if key == "o" else "khởi động lại", key
            ),
            level=2,
        )
        self.sysrq("s")  # đẩy dữ liệu trong RAM xuống đĩa
        self.sleep(5)
        self.sysrq("u")  # remount read-only: lần khởi động sau ext4 sạch
        self.sleep(2)
        self.sysrq(key)

    def write_status(self, signals, now):
        data = {
            "pid": os.getpid(),
            "mode": self.config["mode"],
            "action": self.config["action"],
            "frozen": self.detector.frozen,
            "elapsed": round(self.detector.elapsed(now), 1),
            "reasons": sorted(self.detector.reasons),
            "signals": signals,
            "memory_full": self.psi.percent("memory", "full"),
            "io_full": self.psi.percent("io", "full"),
            "ts": round(time.time(), 1),
        }
        try:
            _write_json_atomic(STATUS_PATH, data, fsync=False)  # /run là tmpfs: không chạm đĩa
        except (OSError, TypeError, ValueError):
            pass

    def run(self):
        while not self.stop_event.is_set():
            now = time.monotonic()
            try:
                signals = self.tick(now)
                self.write_status(signals, now)
            except Exception as error:  # không bao giờ để watchdog chết vì một vòng lỗi
                self.log(f"lỗi vòng lặp: {error!r}")
            self.stop_event.wait(1.0)


# ---------------------------------------------------------------- khởi động


def _stderr_nonblocking():
    try:
        fd = sys.stderr.fileno()
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)
    except (OSError, ValueError, AttributeError):
        pass


def _log(message):
    try:
        sys.stderr.write("watchdog: " + message + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):  # BlockingIOError khi journald đứng: bỏ dòng log
        pass


def _mlockall():
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.mlockall(1 | 2) != 0:  # MCL_CURRENT | MCL_FUTURE
            return f"mlockall lỗi errno {ctypes.get_errno()}"
        return None
    except OSError as error:
        return f"mlockall: {error}"


def _prune_reports():
    try:
        names = sorted(n for n in os.listdir(REPORT_DIR) if n.startswith("freeze-") and n.endswith(".json"))
        for name in names[:-KEEP_REPORTS]:
            os.unlink(os.path.join(REPORT_DIR, name))
    except OSError:
        pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    config = load_config()
    if "--print-config" in argv:
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 0
    if os.geteuid() != 0:
        print("watchdog cần chạy bằng root (dịch vụ pc-health-watch-watchdog)", file=sys.stderr)
        return 1
    _stderr_nonblocking()
    if config["mode"] == "off":
        _log("mode=off — không theo dõi")
        return 0

    # nạp trước mọi thứ cần dùng — sau mlockall không được đọc đĩa nữa
    classify = None
    try:
        from .collectors.kernel_log import classify
    except ImportError as error:
        _log(f"không có kernel_log: {error}")
    gui_hung = read_all = None
    try:
        from .heartbeat import gui_hung
        from .heartbeat import read_all_heartbeats as read_all
    except ImportError as error:
        _log(f"không có heartbeat (bỏ tín hiệu giao diện): {error}")
    for path in (REPORT_DIR, os.path.dirname(STATUS_PATH)):
        try:
            os.makedirs(path, mode=0o755, exist_ok=True)
        except OSError:
            pass
    _prune_reports()
    threading.stack_size(512 * 1024)  # mlockall khóa cả stack luồng: giữ nhỏ
    problem = _mlockall()
    if problem:
        _log(problem + " — vẫn chạy nhưng có thể bị swap khi thiếu RAM")
    try:
        with open("/proc/self/oom_score_adj", "w") as stream:
            stream.write("-1000")
    except OSError:
        pass

    watchdog = Watchdog(config, kmsg=Kmsg(classify), heartbeats=read_all, log=_log)
    watchdog.gui_hung = gui_hung
    watchdog.warm_up()
    signal.signal(signal.SIGTERM, lambda *_: watchdog.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: watchdog.stop_event.set())
    _log(
        "bắt đầu: mode={mode}, {action} sau {action_after_seconds} s đơ, phát hiện sau {detect_seconds} s".format(
            **config
        )
    )
    if watchdog.kmsg is not None:
        watchdog.kmsg.write("watchdog bắt đầu (mode={})".format(config["mode"]), 6)
    watchdog.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
