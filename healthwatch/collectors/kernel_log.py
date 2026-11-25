"""Nhận diện sự kiện nguy hiểm trong kernel log và theo dõi log liên tục.

Module chỉ dùng thư viện chuẩn, tương thích Python 3.8+.
"""

import re
import shutil
import subprocess
import threading
import time
from typing import Optional

# Lệnh mặc định để đọc kernel log. journalctl được ưu tiên vì có timestamp
# và không cần quyền root trên hầu hết bản phân phối hiện đại.
JOURNAL_COMMAND = [
    "journalctl",
    "-k",
    "-f",
    "-n",
    "0",
    "--no-pager",
    "-o",
    "short-iso",
]
DMESG_COMMAND = ["dmesg", "-w", "--level=err,crit,alert,emerg,warn"]


# Mỗi phần tử: (regex đã compile, category, severity, summary tiếng Việt).
# Các mẫu cụ thể phải đứng trước mẫu chung để summary chính xác hơn.
# Nhóm có tên (ví dụ (?P<dev>...)) sẽ được chèn vào summary bằng {dev}.
PATTERNS = [
    # ------------------------------------------------------------------
    # Đĩa / I/O
    # ------------------------------------------------------------------
    (
        re.compile(
            r"blk_update_request:\s*I/O error,?\s*dev\s+(?P<dev>[A-Za-z0-9._/-]+)",
            re.IGNORECASE,
        ),
        "disk",
        "critical",
        "Lỗi I/O trên thiết bị {dev}",
    ),
    (
        re.compile(
            r"nvme\s+(?P<dev>[^:\s]+):\s*I/O\s+\d+\s+QID\s+\d+\s+timeout",
            re.IGNORECASE,
        ),
        "disk",
        "critical",
        "Timeout I/O NVMe trên {dev}",
    ),
    (
        re.compile(
            r"nvme\s+(?P<dev>[^:\s]+):\s*controller is down",
            re.IGNORECASE,
        ),
        "disk",
        "critical",
        "Bộ điều khiển NVMe {dev} ngừng hoạt động, sẽ reset",
    ),
    (
        re.compile(
            r"nvme\s+(?P<dev>[^:\s]+):\s*Device not ready",
            re.IGNORECASE,
        ),
        "disk",
        "critical",
        "Thiết bị NVMe {dev} chưa sẵn sàng",
    ),
    (
        re.compile(
            r"(?P<dev>ata\d+(?:\.\d+)?):\s*exception Emask",
            re.IGNORECASE,
        ),
        "disk",
        "critical",
        "Lỗi ATA trên {dev} (exception Emask)",
    ),
    (
        re.compile(
            r"(?P<dev>ata\d+):\s*hard resetting link",
            re.IGNORECASE,
        ),
        "disk",
        "critical",
        "Đang hard reset liên kết {dev}",
    ),
    (
        re.compile(
            r"(?P<dev>ata\d+(?:\.\d+)?):\s*failed command",
            re.IGNORECASE,
        ),
        "disk",
        "critical",
        "Lệnh ATA thất bại trên {dev}",
    ),
    (
        re.compile(
            r"sd\s+\d+:\d+:\d+:\d+:\s*\[(?P<dev>[^\]]+)\].*FAILED Result",
            re.IGNORECASE,
        ),
        "disk",
        "critical",
        "Lệnh SCSI thất bại trên {dev}",
    ),
    (
        re.compile(
            r"Buffer I/O error on dev\s+(?P<dev>[A-Za-z0-9._/-]+)",
            re.IGNORECASE,
        ),
        "disk",
        "critical",
        "Lỗi I/O bộ đệm trên {dev}",
    ),
    (
        re.compile(
            r"I/O error,?\s*dev\s+(?P<dev>[A-Za-z0-9._/-]+)",
            re.IGNORECASE,
        ),
        "disk",
        "critical",
        "Lỗi I/O trên thiết bị {dev}",
    ),
    (
        re.compile(r"Medium Error", re.IGNORECASE),
        "disk",
        "critical",
        "Lỗi phương tiện lưu trữ (Medium Error)",
    ),
    # ------------------------------------------------------------------
    # Hệ thống tệp
    # ------------------------------------------------------------------
    (
        re.compile(
            r"EXT4-fs error \(device (?P<dev>[^)]+)\)",
            re.IGNORECASE,
        ),
        "filesystem",
        "critical",
        "Lỗi hệ thống tệp EXT4 trên {dev}",
    ),
    (
        re.compile(
            r"EXT4-fs \((?P<dev>[^)]+)\):\s*Remounting filesystem read-only",
            re.IGNORECASE,
        ),
        "filesystem",
        "critical",
        "EXT4 trên {dev} chuyển sang chỉ đọc",
    ),
    (
        re.compile(
            r"Aborting journal on device\s+(?P<dev>[A-Za-z0-9._/-]+)",
            re.IGNORECASE,
        ),
        "filesystem",
        "critical",
        "Hủy journal trên {dev}",
    ),
    (
        re.compile(r"BTRFS error", re.IGNORECASE),
        "filesystem",
        "critical",
        "Lỗi hệ thống tệp BTRFS",
    ),
    (
        re.compile(
            r"XFS \((?P<dev>[^)]+)\):\s*Corruption",
            re.IGNORECASE,
        ),
        "filesystem",
        "critical",
        "Phát hiện hỏng dữ liệu XFS trên {dev}",
    ),
    # ------------------------------------------------------------------
    # Bộ nhớ
    # ------------------------------------------------------------------
    (
        re.compile(
            r"Out of memory:\s*Killed process\s+\d+\s+\((?P<proc>[^)]+)\)",
            re.IGNORECASE,
        ),
        "memory",
        "critical",
        "Hết bộ nhớ: đã kill tiến trình {proc}",
    ),
    (
        re.compile(r"oom-kill:", re.IGNORECASE),
        "memory",
        "critical",
        "Trình OOM killer kích hoạt",
    ),
    (
        re.compile(r"page allocation failure", re.IGNORECASE),
        "memory",
        "warning",
        "Cấp phát bộ nhớ thất bại",
    ),
    # ------------------------------------------------------------------
    # Treo / lỗi kernel nghiêm trọng
    # ------------------------------------------------------------------
    (
        re.compile(
            r"INFO:\s*task\s+(?P<proc>\S+):\d+\s+blocked for more than\s+\d+\s+seconds",
            re.IGNORECASE,
        ),
        "hang",
        "critical",
        "Tiến trình {proc} bị chặn quá lâu",
    ),
    (
        re.compile(r"watchdog:\s*BUG:\s*soft lockup", re.IGNORECASE),
        "hang",
        "critical",
        "Phát hiện soft lockup CPU",
    ),
    (
        re.compile(
            r"rcu:\s*INFO:\s*rcu_\w+ self-detected stall",
            re.IGNORECASE,
        ),
        "hang",
        "critical",
        "RCU phát hiện stall",
    ),
    (
        re.compile(r"NMI watchdog:.*hard LOCKUP", re.IGNORECASE),
        "hang",
        "critical",
        "Phát hiện hard LOCKUP",
    ),
    (
        re.compile(r"Kernel panic", re.IGNORECASE),
        "hang",
        "critical",
        "Kernel panic",
    ),
    (
        re.compile(r"BUG:\s*unable to handle", re.IGNORECASE),
        "hang",
        "critical",
        "Lỗi kernel: unable to handle",
    ),
    (
        re.compile(r"general protection fault", re.IGNORECASE),
        "hang",
        "critical",
        "Lỗi general protection fault",
    ),
    # ------------------------------------------------------------------
    # GPU
    # ------------------------------------------------------------------
    (
        re.compile(
            r"NVRM:\s*Xid\s+\(PCI:(?P<dev>[^)]+)\):\s*(?P<code>\d+)",
            re.IGNORECASE,
        ),
        "gpu",
        "critical",
        "Lỗi GPU NVIDIA Xid {code} (PCI {dev})",
    ),
    (
        re.compile(
            r"NVRM:\s*GPU has fallen off the bus",
            re.IGNORECASE,
        ),
        "gpu",
        "critical",
        "GPU NVIDIA rơi khỏi bus PCI",
    ),
    (
        re.compile(r"amdgpu:\s*GPU fault detected", re.IGNORECASE),
        "gpu",
        "critical",
        "Phát hiện lỗi GPU AMD",
    ),
    (
        re.compile(
            r"\[drm:amdgpu_job_timedout\].*ring gfx timeout",
            re.IGNORECASE,
        ),
        "gpu",
        "critical",
        "GPU AMD bị timeout ring gfx",
    ),
    (
        re.compile(
            r"i915\s+\S+:\s*\[drm\]\s*GPU HANG",
            re.IGNORECASE,
        ),
        "gpu",
        "critical",
        "GPU Intel bị treo (GPU HANG)",
    ),
    (
        re.compile(r"nouveau.*fifo:\s*SCHED_ERROR", re.IGNORECASE),
        "gpu",
        "critical",
        "Lỗi GPU nouveau (SCHED_ERROR)",
    ),
    (
        # Mẫu chung phải đứng sau các mẫu DRM cụ thể ở trên.
        re.compile(r"\[drm:[^\]]+\].*\*ERROR\*", re.IGNORECASE),
        "gpu",
        "warning",
        "Lỗi DRM/GPU",
    ),
    # ------------------------------------------------------------------
    # Nhiệt độ
    # ------------------------------------------------------------------
    (
        re.compile(
            r"CPU\d+:\s*Core temperature above threshold",
            re.IGNORECASE,
        ),
        "thermal",
        "critical",
        "Nhiệt độ lõi CPU vượt ngưỡng, bị giảm xung",
    ),
    (
        re.compile(r"critical temperature reached", re.IGNORECASE),
        "thermal",
        "critical",
        "Đã đạt nhiệt độ tới hạn",
    ),
    (
        re.compile(
            r"Package temperature above threshold",
            re.IGNORECASE,
        ),
        "thermal",
        "warning",
        "Nhiệt độ package vượt ngưỡng",
    ),
    # ------------------------------------------------------------------
    # Nguồn / lỗi phần cứng
    # ------------------------------------------------------------------
    (
        re.compile(
            r"PCIe Bus Error:\s*severity=Corrected",
            re.IGNORECASE,
        ),
        "power",
        "warning",
        "Lỗi bus PCIe (đã sửa được)",
    ),
    (
        re.compile(
            r"PCIe Bus Error:\s*severity=Uncorrected",
            re.IGNORECASE,
        ),
        "power",
        "critical",
        "Lỗi bus PCIe không sửa được",
    ),
    (
        re.compile(r"mce:\s*\[Hardware Error\]", re.IGNORECASE),
        "power",
        "critical",
        "Lỗi phần cứng MCE",
    ),
    (
        re.compile(r"Machine check events logged", re.IGNORECASE),
        "power",
        "critical",
        "Đã ghi nhận sự kiện machine check",
    ),
]


def _clean_group(value):
    """Bỏ dấu câu thừa ở cuối giá trị nhóm regex (nếu có)."""
    if isinstance(value, str):
        return value.strip().rstrip(",;:")
    return value


def _render_summary(template, groups):
    """Chèn các nhóm regex vào summary; lỗi định dạng thì giữ nguyên mẫu."""
    try:
        cleaned = {key: _clean_group(value) for key, value in groups.items()}
        return template.format(**cleaned)
    except (KeyError, IndexError, ValueError):
        return template


def classify(line) -> Optional[dict]:
    """Phân loại một dòng kernel log.

    Trả về dict {"category", "severity", "summary", "line"} hoặc None nếu
    dòng không khớp mẫu nguy hiểm nào. Dùng search nên tiền tố timestamp
    kiểu journalctl/dmesg không ảnh hưởng.
    """
    if not isinstance(line, str):
        return None
    text = line.strip()
    if not text:
        return None

    for pattern, category, severity, summary in PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        return {
            "category": category,
            "severity": severity,
            "summary": _render_summary(summary, match.groupdict()),
            "line": text,
        }
    return None


def scan_lines(lines) -> list:
    """Phân loại nhiều dòng, bỏ qua các dòng không nguy hiểm."""
    events = []
    for line in lines:
        event = classify(line)
        if event is not None:
            events.append(event)
    return events


class Deduper:
    """Chống spam cảnh báo trùng lặp trong một cửa sổ thời gian."""

    def __init__(self, window=300):
        self.window = float(window)
        self._last = {}
        self._lock = threading.Lock()

    def seen(self, event, now):
        """Trả True nếu cùng (category, summary) vừa xuất hiện trong window giây."""
        if isinstance(event, dict):
            key = (event.get("category"), event.get("summary"))
        else:
            key = (None, None)
        try:
            current = float(now)
        except (TypeError, ValueError):
            current = time.monotonic()

        with self._lock:
            last = self._last.get(key)
            if last is not None and current - last < self.window:
                # Cập nhật mốc để chuỗi cảnh báo liên tục không bị lặp lại.
                self._last[key] = current
                return True
            self._last[key] = current

            # Dọn bớt mục cũ để dict không phình mãi khi chạy lâu.
            if len(self._last) > 512:
                cutoff = current - self.window
                self._last = {old_key: old_time for old_key, old_time in self._last.items() if old_time >= cutoff}
            return False


class KernelLogFollower:
    """Đọc kernel log trong luồng nền và gọi callback cho mỗi sự kiện."""

    RESTART_DELAY = 10.0

    def __init__(self, callback, command=None):
        self.callback = callback
        self.command = list(command) if command is not None else None
        self._thread = None
        self._stop = threading.Event()
        self._process = None
        self._process_lock = threading.Lock()
        self._prefer_dmesg = False

    def start(self):
        """Khởi động luồng theo dõi (không làm gì nếu đã chạy)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="kernel-log-follower",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout=5):
        """Dừng luồng và kết thúc tiến trình con đang đọc log."""
        self._stop.set()
        with self._process_lock:
            process = self._process
        if process is not None:
            try:
                process.terminate()
            except (OSError, subprocess.SubprocessError):
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    def _next_command(self):
        """Chọn lệnh đọc log và cho biết có được phép fallback hay không."""
        if self.command is not None:
            # Lệnh do người dùng/test cung cấp: dùng cố định, không fallback.
            return self.command, False
        if not self._prefer_dmesg and shutil.which("journalctl") is not None:
            return list(JOURNAL_COMMAND), True
        self._prefer_dmesg = True
        return list(DMESG_COMMAND), True

    def _handle_line(self, line):
        """Phân loại một dòng và gọi callback, nuốt mọi lỗi."""
        try:
            event = classify(line)
        except Exception:
            return
        if event is None:
            return
        try:
            self.callback(event)
        except Exception:
            # Callback lỗi không được làm chết luồng theo dõi.
            pass

    def _run(self):
        while not self._stop.is_set():
            command, allow_fallback = self._next_command()
            process = None
            started_at = time.monotonic()
            line_count = 0
            returncode = None

            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    universal_newlines=True,
                    bufsize=1,
                )
                with self._process_lock:
                    self._process = process

                stream = process.stdout
                if stream is not None:
                    for line in iter(stream.readline, ""):
                        if self._stop.is_set():
                            break
                        line_count += 1
                        self._handle_line(line)

                try:
                    returncode = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    returncode = None
                except OSError:
                    returncode = None
            except (OSError, subprocess.SubprocessError, ValueError):
                returncode = -1
            finally:
                with self._process_lock:
                    if self._process is process:
                        self._process = None
                if process is not None:
                    try:
                        if process.poll() is None:
                            process.terminate()
                    except (OSError, subprocess.SubprocessError):
                        pass
                    try:
                        if process.stdout is not None:
                            process.stdout.close()
                    except (OSError, ValueError):
                        pass

            if self._stop.is_set():
                break

            elapsed = time.monotonic() - started_at
            # journalctl thoát ngay với lỗi và chưa đọc được dòng nào:
            # chuyển sang dmesg thay vì lặp lại vô ích.
            if (
                allow_fallback
                and not self._prefer_dmesg
                and line_count == 0
                and elapsed < 5.0
                and returncode not in (0, None)
            ):
                self._prefer_dmesg = True
                continue

            # Tiến trình chết vì bất kỳ lý do gì: chờ rồi khởi động lại.
            self._stop.wait(self.RESTART_DELAY)
