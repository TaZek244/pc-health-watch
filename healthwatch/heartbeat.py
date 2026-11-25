"""Nhịp tim của giao diện: phát hiện GNOME Shell / KWin / compositor bị treo.

Daemon chạy dưới quyền người dùng thăm dò giao diện theo chu kỳ rồi ghi kết quả
ra file trong XDG_RUNTIME_DIR. Watchdog chạy bằng root đọc file này để quyết định
có tắt máy hay không — nên chỉ được coi là "treo" khi thăm dò bị TIMEOUT.

Giao diện không tồn tại (đã đăng xuất, chưa đăng nhập, thiếu lệnh, lỗi kết nối
trả về ngay) là "không biết" (None), tuyệt đối không phải treo. Một lần tắt máy
sai gây mất dữ liệu, còn bỏ sót một lần treo chỉ làm chậm phản ứng.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import List, Optional

HEARTBEAT_NAME = "pc-health-watch-heartbeat.json"

# Quá 120 giây không có nhịp tim mới thì file không còn đủ tin cậy để tắt máy:
# daemon có thể đã chết, bị dừng, hoặc chính nó đang bị treo vì lý do khác.
STALE_SECONDS = 120


def detect_probe(env=None, which=shutil.which) -> Optional[List[str]]:
    """Chọn lệnh thăm dò giao diện phù hợp với môi trường desktop hiện tại.

    Thứ tự ưu tiên: GNOME → KDE → Hyprland → X11. Chỉ trả lệnh khi lệnh đầu
    tiên thực sự tồn tại trên PATH; `which` được inject để test không cần
    chạy lệnh thật.
    """
    env = os.environ if env is None else env

    desktop = (env.get("XDG_CURRENT_DESKTOP") or "").upper()
    command = None
    if "GNOME" in desktop:
        # gnome-shell xử lý D-Bus trên main loop của nó. Nếu shell treo thì
        # lệnh gdbus treo theo cho tới khi hết timeout → dấu hiệu đáng tin.
        command = [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.gnome.Shell",
            "--object-path",
            "/org/gnome/Shell",
            "--method",
            "org.freedesktop.DBus.Peer.Ping",
        ]
    elif "KDE" in desktop:
        # KWin cũng trả lời Peer.Ping trên main loop; compositor treo → timeout.
        command = [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.kde.KWin",
            "--object-path",
            "/KWin",
            "--method",
            "org.freedesktop.DBus.Peer.Ping",
        ]
    elif env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        command = ["hyprctl", "-j", "version"]
    elif env.get("DISPLAY"):
        # X server treo thì xset không trả lời và bị timeout giết.
        command = ["xset", "q"]

    if command is None:
        return None

    try:
        found = which(command[0])
    except OSError:
        return None
    return command if found else None


def probe_gui(command, timeout=8, env=None, run=subprocess.run) -> Optional[bool]:
    """Chạy một lần thăm dò. True = giao diện sống, False = TIMEOUT, None = không biết.

    Chỉ `subprocess.TimeoutExpired` mới là bằng chứng treo. Exit code khác 0,
    lỗi khởi chạy (OSError), hoặc không có lệnh đều là "không biết" — không đủ
    căn cứ để watchdog tắt máy. `run` được inject để test.
    """
    if command is None:
        return None
    try:
        result = run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        # Lệnh không trả lời trong thời hạn: đây là tín hiệu treo duy nhất.
        return False
    except (OSError, subprocess.SubprocessError):
        return None
    return True if result.returncode == 0 else None


class GuiMonitor:
    """Luồng nền thăm dò giao diện và giữ trạng thái mới nhất.

    Trạng thái được đọc/ghi dưới lock vì luồng nền cập nhật còn luồng chính
    (hoặc bộ ghi nhịp tim) đọc. `clock` mặc định là `time.monotonic` để mốc
    `fail_since` không bị ảnh hưởng khi đổi giờ.
    """

    def __init__(
        self,
        command,
        interval=10,
        timeout=8,
        clock=time.monotonic,
        probe=probe_gui,
        env=None,
    ):
        self.command = command
        self.interval = interval
        self.timeout = timeout
        self.clock = clock
        self.probe = probe
        self.env = env
        self._lock = threading.Lock()
        self._state = None
        self._fail_since = None
        self._last_checked = None
        self._stop_event = threading.Event()
        self._thread = None

    @property
    def state(self):
        """True/False/None — kết quả thăm dò gần nhất."""
        with self._lock:
            return self._state

    @property
    def fail_since(self):
        """Mốc clock() bắt đầu chuỗi False liên tiếp, None nếu không đang False."""
        with self._lock:
            return self._fail_since

    @property
    def last_checked(self):
        """Mốc clock() của lần thăm dò gần nhất."""
        with self._lock:
            return self._last_checked

    def check_once(self):
        """Thăm dò đúng một lần, đồng bộ. Trả về kết quả vừa đo (dùng cho test)."""
        result = self.probe(self.command, timeout=self.timeout, env=self.env)
        now = self.clock()
        with self._lock:
            if result is False:
                # Giữ mốc của lần False đầu tiên trong chuỗi liên tiếp.
                if self._fail_since is None:
                    self._fail_since = now
            else:
                # True hoặc None đều phá chuỗi: không đủ chắc chắn để kết luận treo.
                self._fail_since = None
            self._state = result
            self._last_checked = now
        return result

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception:
                # Lỗi bất ngờ trong phép thăm dò không được giết luồng nền.
                # Không đặt fail_since: giữ nguyên "không biết" an toàn.
                pass
            # Event.wait cho phép stop() đánh thức ngay thay vì ngủ đủ interval.
            self._stop_event.wait(self.interval)

    def start(self):
        """Khởi động luồng nền daemon. Gọi nhiều lần vẫn chỉ có một luồng."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop_event.clear()
            thread = threading.Thread(target=self._loop, name="gui-heartbeat", daemon=True)
            self._thread = thread
        thread.start()
        return thread

    def stop(self, join_timeout=None):
        """Yêu cầu luồng nền dừng và chờ nó kết thúc (có giới hạn thời gian)."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            if join_timeout is None:
                # Đủ cho lần thăm dò đang dở (subprocess có timeout riêng) kết thúc.
                join_timeout = self.timeout + 1.0
            thread.join(timeout=join_timeout)
        return thread


def heartbeat_path(uid=None) -> str:
    """Đường dẫn file nhịp tim: ưu tiên XDG_RUNTIME_DIR, sau đó /run/user/<uid>."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return os.path.join(base, HEARTBEAT_NAME)
    if uid is None:
        uid = os.getuid()
    return os.path.join("/run/user", str(uid), HEARTBEAT_NAME)


class HeartbeatWriter:
    """Ghi nhịp tim nguyên tử để watchdog root luôn đọc được file hoàn chỉnh.

    Dùng `time.monotonic` (CLOCK_MONOTONIC) cho trường `mono` vì trên Linux cả
    tiến trình root và tiến trình người dùng đều thấy cùng một đồng hồ này,
    và nó không nhảy khi suspend hay khi người dùng đổi giờ hệ thống. Nhờ vậy
    watchdog so sánh được mốc thời gian với file do user ghi mà không lệch múi giờ.
    """

    def __init__(self, path=None, clock=time.monotonic, wall=time.time):
        self.path = path or heartbeat_path()
        self.clock = clock
        self.wall = wall
        self._lock = threading.Lock()
        self._desktop = ""

    def _write(self, status, gui_state, gui_fail_since, desktop):
        payload = {
            "version": 1,
            "pid": os.getpid(),
            "uid": os.getuid(),
            "status": status,
            "mono": self.clock(),
            "ts": self.wall(),
            "gui": gui_state,
            "gui_fail_since": gui_fail_since,
            "desktop": desktop,
        }
        directory = os.path.dirname(self.path) or "."
        temp_path = None
        try:
            os.makedirs(directory, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix=".pc-health-watch-heartbeat-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
            # File tạm cùng thư mục nên os.replace là thao tác nguyên tử trên
            # cùng filesystem: watchdog không bao giờ thấy JSON bị cắt cụt.
            os.chmod(temp_path, 0o644)
            os.replace(temp_path, self.path)
            temp_path = None
        except OSError:
            # /run/user đầy, mất quyền, ...: bỏ qua, lần sau thử lại.
            pass
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def beat(self, gui_state, gui_fail_since, desktop=""):
        """Ghi một nhịp tim trạng thái "running" với kết quả giao diện hiện tại."""
        with self._lock:
            self._desktop = desktop
            self._write("running", gui_state, gui_fail_since, desktop)

    def stop(self):
        """Ghi nhịp tim cuối cùng với status "stopped" (daemon tắt bình thường)."""
        with self._lock:
            self._write("stopped", None, None, self._desktop)


def read_heartbeat(path) -> Optional[dict]:
    """Đọc một file nhịp tim. File hỏng/không phải object JSON → None."""
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_all_heartbeats(run_root="/run/user") -> List[dict]:
    """Đọc mọi nhịp tim dưới `run_root` (dùng bởi watchdog root).

    Bỏ qua thư mục không có file, file hỏng và JSON không phải object.
    """
    heartbeats = []
    try:
        names = os.listdir(run_root)
    except OSError:
        return heartbeats
    for name in sorted(names):
        path = os.path.join(run_root, name, HEARTBEAT_NAME)
        heartbeat = read_heartbeat(path)
        if heartbeat is not None:
            heartbeats.append(heartbeat)
    return heartbeats


def gui_hung(heartbeat, now_mono, hang_seconds=45) -> bool:
    """True chỉ khi đủ mọi bằng chứng để watchdog tắt máy vì giao diện treo.

    Điều kiện: daemon còn chạy, lần thăm dò gần nhất là False, đã False liên
    tục đủ `hang_seconds`, và bản thân nhịp tim không quá cũ (quá 120 giây thì
    không đủ tin cậy — thà bỏ sót còn hơn tắt máy oan).
    """
    if not isinstance(heartbeat, dict):
        return False
    if heartbeat.get("status") != "running":
        return False
    if heartbeat.get("gui") is not False:
        return False
    fail_since = heartbeat.get("gui_fail_since")
    mono = heartbeat.get("mono")
    if fail_since is None or mono is None:
        return False
    try:
        fail_since = float(fail_since)
        mono = float(mono)
        now_mono = float(now_mono)
    except (TypeError, ValueError):
        return False
    if now_mono - mono > STALE_SECONDS:
        return False
    return (now_mono - fail_since) >= hang_seconds
