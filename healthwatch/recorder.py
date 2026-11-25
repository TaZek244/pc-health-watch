"""Hộp đen: ghi số liệu xuống đĩa (JSONL, fsync) để sau khi reset cứng vẫn đọc lại được.

fsync mỗi lần ghi để dữ liệu thực sự nằm trên SSD — nếu chỉ nằm trong page cache thì
một lần đơ + reset sẽ mất đúng những phút quan trọng nhất trước khi đơ.
"""

import json
import os
import threading
import time

from . import util


class Recorder:
    def __init__(self, directory=None, boot=None, retention_days=14, clock=time.time):
        self.directory = directory or util.state_dir()
        self.boot = boot or util.boot_id()
        self.retention_days = retention_days
        self.clock = clock
        self._stream = None
        self._day = None
        self.failed = False
        self._lock = threading.Lock()

    def path_for(self, day):
        return os.path.join(self.directory, f"blackbox-{day}.jsonl")

    def _open(self):
        day = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        if self._stream is not None and day == self._day:
            return self._stream
        self.close()
        os.makedirs(self.directory, exist_ok=True)
        self._stream = open(self.path_for(day), "a", encoding="utf-8")
        self._day = day
        self.cleanup()
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
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            self.failed = False
        except (OSError, ValueError, TypeError):
            # đĩa đầy / read-only: không được làm sập daemon, cảnh báo vẫn phải chạy
            self.failed = True
            self.close()

    def cleanup(self):
        cutoff = self.clock() - self.retention_days * 86400
        try:
            names = os.listdir(self.directory)
        except OSError:
            return
        for name in names:
            if not (name.startswith("blackbox-") and name.endswith(".jsonl")):
                continue
            path = os.path.join(self.directory, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)
            except OSError:
                pass

    def close(self):
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
        self._stream = None
        self._day = None


def read_recent(directory=None, seconds=600, now=None):
    """Đọc các bản ghi trong `seconds` giây gần nhất (dùng cho `healthwatch status --history`)."""
    directory = directory or util.state_dir()
    now = now or time.time()
    out = []
    try:
        names = sorted(n for n in os.listdir(directory) if n.startswith("blackbox-") and n.endswith(".jsonl"))
    except OSError:
        return out
    for name in names[-2:]:
        try:
            with open(os.path.join(directory, name), encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and record.get("ts", 0) >= now - seconds:
                        out.append(record)
        except OSError:
            continue
    return out
