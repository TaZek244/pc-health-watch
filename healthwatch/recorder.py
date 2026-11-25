"""Hộp đen: ghi số liệu xuống đĩa (JSONL, fsync) để sau khi reset cứng vẫn đọc lại được.

fsync mỗi lần ghi để dữ liệu thực sự nằm trên SSD — nếu chỉ nằm trong page cache thì
một lần đơ + reset sẽ mất đúng những phút quan trọng nhất trước khi đơ.
Ngày cũ được nén gzip (~12 lần), xóa sau retention_days và tổng không vượt max_total_mb.
"""

import gzip
import json
import os
import re
import threading
import time
import zlib

from . import util

_NAME_RE = re.compile(r"^blackbox-(.+)\.jsonl(?:\.gz)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _files(directory):
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(name for name in names if _NAME_RE.match(name))


def iter_records(directory, names=None):
    """Đọc cả JSONL thường và gzip; bỏ qua dòng/file hỏng."""
    for name in sorted(names if names is not None else _files(directory)):
        if not _NAME_RE.match(name):
            continue
        path = os.path.join(directory, name)
        try:
            opener = gzip.open if name.endswith(".gz") else open
            kwargs = {"mode": "rt", "encoding": "utf-8", "errors": "replace"}
            with opener(path, **kwargs) as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(record, dict):
                        yield record
        except (OSError, EOFError, zlib.error):
            continue


def storage_usage(directory) -> dict:
    files = []
    for name in _files(directory):
        try:
            size = os.path.getsize(os.path.join(directory, name))
        except OSError:
            continue
        files.append({"name": name, "bytes": size, "compressed": name.endswith(".gz")})
    files.sort(key=lambda item: item["name"])
    return {
        "files": files,
        "total_bytes": sum(item["bytes"] for item in files),
        "oldest": files[0]["name"] if files else None,
        "newest": files[-1]["name"] if files else None,
    }


class Recorder:
    def __init__(self, directory=None, boot=None, retention_days=14, clock=time.time, max_total_mb=200, compress_after_days=1):
        self.directory = directory or util.state_dir()
        self.boot = boot or util.boot_id()
        self.retention_days = retention_days
        self.max_total_mb = max_total_mb
        self.compress_after_days = compress_after_days
        self.clock = clock
        self._stream = None
        self._day = None
        self._last_maintenance = float("-inf")
        self.failed = False
        self._lock = threading.Lock()

    def path_for(self, day):
        return os.path.join(self.directory, "blackbox-{}.jsonl".format(day))

    def _open(self):
        day = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        if self._stream is not None and day == self._day:
            return self._stream
        self.close()
        os.makedirs(self.directory, exist_ok=True)
        self._stream = open(self.path_for(day), "a", encoding="utf-8")
        self._day = day
        self._last_maintenance = self.clock()
        self.compress_old(self.clock())
        self.enforce_limits(self.clock())
        return self._stream

    def write(self, record_type, payload=None):
        record = {"type": record_type, "boot_id": self.boot}
        if payload:
            record.update(payload)
        record.setdefault("ts", round(self.clock(), 3))
        with self._lock:
            self._write_locked(record)

    def _write_locked(self, record):
        try:
            stream = self._open()
            now = self.clock()
            if now - self._last_maintenance >= 3600:
                self.compress_old(now)
                self.enforce_limits(now)
                self._last_maintenance = now
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            self.failed = False
        except (OSError, ValueError, TypeError):
            # đĩa đầy / read-only: không được làm sập daemon, cảnh báo vẫn phải chạy
            self.failed = True
            self.close()

    def compress_old(self, now=None):
        now = self.clock() if now is None else now
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        cutoff_day = time.strftime("%Y-%m-%d", time.localtime(now - self.compress_after_days * 86400))
        for name in _files(self.directory):
            match = _NAME_RE.match(name)
            if not match or not _DATE_RE.match(match.group(1)) or name.endswith(".gz") or match.group(1) >= today or match.group(1) > cutoff_day:
                continue
            source = os.path.join(self.directory, name)
            temporary = source + ".tmp"
            try:
                with open(source, "rb") as input_stream, gzip.open(temporary, "wb") as output_stream:
                    while True:
                        chunk = input_stream.read(1024 * 1024)
                        if not chunk:
                            break
                        output_stream.write(chunk)
                with open(temporary, "rb") as check:
                    os.fsync(check.fileno())
                os.replace(temporary, source + ".gz")
                os.unlink(source)
            except OSError:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def enforce_limits(self, now=None):
        now = self.clock() if now is None else now
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        cutoff = now - self.retention_days * 86400
        for name in _files(self.directory):
            match = _NAME_RE.match(name)
            if not match or not _DATE_RE.match(match.group(1)) or match.group(1) >= today:
                continue
            path = os.path.join(self.directory, name)
            try:
                old = time.mktime(time.strptime(match.group(1), "%Y-%m-%d")) < cutoff
                old = old or os.path.getmtime(path) < cutoff
            except (OSError, ValueError, OverflowError):
                old = False
            if old:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        usage = storage_usage(self.directory)
        limit = self.max_total_mb * 1024 * 1024
        for item in usage["files"]:
            if usage["total_bytes"] <= limit:
                break
            if item["name"].startswith("blackbox-{}".format(today)):
                continue
            try:
                os.unlink(os.path.join(self.directory, item["name"]))
                usage["total_bytes"] -= item["bytes"]
            except OSError:
                pass

    def cleanup(self):
        self.enforce_limits(self.clock())

    def close(self):
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
        self._stream = None
        self._day = None


def read_recent(directory=None, seconds=600, now=None):
    """Đọc các bản ghi trong `seconds` giây gần nhất."""
    directory = directory or util.state_dir()
    now = time.time() if now is None else now
    # chỉ mở các file đủ phủ khoảng thời gian cần (mỗi file 1 ngày) — không giải nén cả 14 ngày
    names = _files(directory)[-(int(seconds // 86400) + 2):]
    return [record for record in iter_records(directory, names) if record.get("ts", 0) >= now - seconds]
