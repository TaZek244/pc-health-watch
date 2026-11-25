"""Vòng lặp chính: đo → ghi hộp đen → xét luật → cảnh báo; luồng phụ theo dõi kernel log và SMART."""

import json
import os
import queue
import signal
import sys
import threading
import time

from . import config as config_mod
from . import util
from .notify import Notifier
from .recorder import Recorder
from .recording import AdaptiveSchedule, compact, is_elevated
from .rules import RuleEngine

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def log(message):
    try:
        print("healthwatch: " + message, file=sys.stderr, flush=True)
    except Exception:
        pass


def status_path():
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "pc-health-watch-status.json")


def write_status(snapshot, active):
    """Ghi trạng thái mới nhất (atomic) để `healthwatch status` đọc mà không phải đo lại."""
    path = status_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "snapshot": snapshot, "active": active}, stream, ensure_ascii=False)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        pass


def _uid_of(pid, root="/"):
    for line in util.read_text(util.proc_path(root, "proc", str(pid), "status")).splitlines():
        if line.startswith("Uid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def pick_victim(snapshot, protect, uid=None, root="/"):
    """Tiến trình ngốn RAM nhất thuộc người dùng hiện tại, không nằm trong danh sách bảo vệ."""
    uid = os.getuid() if uid is None else uid
    for proc in (snapshot.get("top") or {}).get("memory") or []:
        pid, name = proc.get("pid"), proc.get("name") or ""
        if not pid or name in protect or pid == os.getpid():
            continue
        if _uid_of(pid, root) == uid:
            return proc
    return None


class Daemon:
    def __init__(self, cfg, collector=None, recorder=None, notifier=None, follower_factory=None):
        self.config = cfg
        if collector is None:
            from .collectors.system import SystemCollector

            collector = SystemCollector()
        self.collector = collector
        storage = cfg.get("storage") or {}
        self.recorder = recorder or Recorder(
            retention_days=storage.get("retention_days", cfg["retention_days"]),
            max_total_mb=storage.get("max_total_mb", 200),
            compress_after_days=storage.get("compress_after_days", 1),
        )
        self.notifier = notifier or Notifier(cfg, log)
        self.engine = RuleEngine(cfg)
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.follower_factory = follower_factory
        self.follower = None
        self.active = {}  # (rule,key) -> alert dict đang hiệu lực
        self.smart_problems = {}
        self.dedupe = {}
        self.pressure_since = None
        self.last_relief = float("-inf")
        self.schedule = AdaptiveSchedule(cfg)
        self.next_wait = cfg["interval_seconds"]
        self.config_mtime = self._config_mtime()

    # ---------------------------------------------------------------- tiện ích

    @staticmethod
    def _config_mtime():
        try:
            return os.stat(config_mod.default_path()).st_mtime_ns
        except OSError:
            return None

    def reload(self):
        cfg, error = config_mod.load_config()
        if error:
            log(error)
        self.config = cfg
        self.engine.config = cfg
        self.notifier.config = cfg
        storage = cfg.get("storage") or {}
        self.recorder.retention_days = storage.get("retention_days", cfg["retention_days"])
        self.recorder.max_total_mb = storage.get("max_total_mb", 200)
        self.recorder.compress_after_days = storage.get("compress_after_days", 1)
        self.schedule = AdaptiveSchedule(cfg)
        log("đã nạp lại cấu hình")

    def emit(self, severity, title, body, rule, key="", tag=None, value=None):
        record = {"rule": rule, "key": key, "severity": severity, "title": title, "body": body, "value": value}
        self.recorder.write("alert", record)
        log(f"[{severity}] {title} — {body}")
        self.notifier.send(severity, title, body, tag=tag or "hw-" + rule)

    # ---------------------------------------------------------------- luồng phụ

    def start_kernel_follower(self):
        if not self.config["rules"]["kernel_log"].get("enabled", True):
            return
        factory = self.follower_factory
        if factory is None:
            try:
                from .collectors.kernel_log import KernelLogFollower

                factory = KernelLogFollower
            except ImportError as error:
                log(f"không có module kernel_log: {error}")
                return
        try:
            self.follower = factory(self.events.put)
            self.follower.start()
        except Exception as error:  # không để lỗi luồng phụ chặn daemon
            log(f"không theo dõi được kernel log: {error}")

    def start_smart_thread(self):
        def loop():
            while not self.stop_event.is_set():
                settings = self.config["rules"]["smart"]
                if settings.get("enabled", True):
                    self.check_smart()
                self.stop_event.wait(max(5, settings.get("every_minutes", 60)) * 60)

        threading.Thread(target=loop, name="smart", daemon=True).start()

    def check_smart(self):
        try:
            from .collectors import smart
        except ImportError:
            return
        try:
            results = smart.read_all()
        except Exception as error:
            log(f"lỗi đọc SMART: {error}")
            return
        for disk in results:
            self.recorder.write("smart", {"smart": disk})
            device = disk.get("device", "?")
            problems = set(disk.get("problems") or [])
            new = problems - self.smart_problems.get(device, set())
            self.smart_problems[device] = problems
            if new:
                self.events.put(
                    {
                        "category": "smart",
                        "severity": "critical",
                        "summary": "SSD {} ({}) có dấu hiệu lỗi".format(device, disk.get("model") or "?"),
                        "line": "; ".join(sorted(new)),
                    }
                )

    # ---------------------------------------------------------------- xử lý trong vòng lặp

    def drain_events(self, now):
        min_rank = SEVERITY_RANK.get(self.config["rules"]["kernel_log"].get("min_severity", "warning"), 1)
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            self.recorder.write("event", dict(event, ts=round(now, 3)))
            severity = event.get("severity", "warning")
            if SEVERITY_RANK.get(severity, 1) < min_rank:
                continue
            key = (event.get("category"), event.get("summary"))
            if now - self.dedupe.get(key, -1e9) < 300:  # cùng lỗi trong 5 phút: chỉ ghi, không báo lại
                continue
            self.dedupe[key] = now
            self.emit(
                severity,
                event.get("summary") or "Lỗi kernel",
                (event.get("line") or "")[:300],
                "kernel_" + str(event.get("category")),
                tag="hw-kernel-" + str(event.get("category")),
            )

    def maybe_relieve_memory(self, snapshot, now):
        settings = self.config["actions"]["relieve_memory"]
        if not settings.get("enabled"):
            self.pressure_since = None
            return
        full = ((snapshot.get("pressure") or {}).get("memory") or {}).get("full_avg10")
        if full is None or full < settings["pressure_full_avg10"]:
            self.pressure_since = None
            return
        if self.pressure_since is None:
            self.pressure_since = now
        if now - self.pressure_since < settings["for_seconds"] or now - self.last_relief < 60:
            return
        victim = pick_victim(snapshot, set(settings.get("protect") or []))
        if not victim:
            return
        try:
            os.kill(victim["pid"], signal.SIGTERM)
        except OSError as error:
            log(f"không đóng được {victim}: {error}")
            return
        self.last_relief = now
        self.pressure_since = None
        self.emit(
            "critical",
            "Đã đóng {} để cứu máy".format(victim.get("name")),
            "Bộ nhớ nghẽn {:.0f}% kéo dài. {} đang dùng {:.0f} MB RAM.".format(
                full, victim.get("name"), victim.get("rss_mb") or 0
            ),
            "relieve_memory",
        )

    def tick(self, now):
        snapshot = self.collector.sample()
        for alert in self.engine.evaluate(snapshot, now):
            ident = f"{alert.rule}:{alert.key}"
            if alert.recovered:
                self.active.pop(ident, None)
            else:
                self.active[ident] = alert.to_dict()
            self.emit(
                alert.severity, alert.title, alert.body, alert.rule, alert.key, tag="hw-" + ident, value=alert.value
            )
        decision = dict(snapshot or {})
        decision["active_alerts"] = list(self.active.values())
        elevated = is_elevated(decision, self.config)
        if self.schedule.due(now, elevated):
            self.recorder.write("sample", dict(compact(snapshot, full=elevated), elevated=elevated))
        self.next_wait = self.schedule.elevated_seconds if (self.schedule.last_elevated or now < self.schedule.cooldown_until) else self.config["interval_seconds"]
        self.drain_events(now)
        self.maybe_relieve_memory(snapshot, now)
        write_status(snapshot, list(self.active.values()))
        return snapshot

    # ---------------------------------------------------------------- chạy

    def run(self):
        self.recorder.write("start", {"version": _version()})
        self.start_kernel_follower()
        self.start_smart_thread()
        log("bắt đầu theo dõi (mỗi {} giây)".format(self.config["interval_seconds"]))
        try:
            while not self.stop_event.is_set():
                mtime = self._config_mtime()
                if mtime != self.config_mtime:
                    self.config_mtime = mtime
                    self.reload()
                try:
                    self.tick(time.monotonic())
                except Exception as error:  # một vòng lỗi không được giết daemon
                    log(f"lỗi vòng đo: {error!r}")
                self.stop_event.wait(self.next_wait)
        finally:
            if self.follower is not None:
                try:
                    self.follower.stop()
                except Exception:
                    pass
            self.recorder.write("stop")
            self.recorder.close()
            log("đã dừng")


def _version():
    from . import __version__

    return __version__


def run_daemon():
    import fcntl

    cfg, error = config_mod.load_config()
    if error:
        log(error)
    lock_path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "pc-health-watch.lock")
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("daemon đã đang chạy")
        return 0
    daemon = Daemon(cfg)
    signal.signal(signal.SIGTERM, lambda *_: daemon.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: daemon.stop_event.set())
    signal.signal(signal.SIGHUP, lambda *_: daemon.reload())
    daemon.run()
    return 0
