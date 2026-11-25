# Freeze Watchdog (`pc-health-watch-watchdog`)

Khi máy Linux bị đơ cứng — chuột đứng yên, bàn phím không ăn, màn hình đóng băng — phản xạ tự nhiên của người dùng là giữ nút nguồn 5 giây để tắt cưỡng bức. Tài liệu này mô tả dịch vụ **`pc-health-watch-watchdog.service`**: một watchdog chạy ở tầng hệ thống giúp phát hiện máy đơ từ sớm, thử giải cứu tự động khi nghẽn RAM, ghi lại bằng chứng giây-từng-giây và tắt/khởi động lại máy an toàn qua Magic SysRq.

---

## 1. Nó làm gì và vì sao cần?

### 1.1. Tại sao tốt hơn tự giữ nút nguồn?
Giữ nút nguồn cắt điện đột ngột trong khi hệ điều hành hoàn toàn không hay biết. Ngược lại, **Freeze Watchdog** can thiệp theo quy trình có kiểm soát:

- **Có cơ hội cứu sống máy mà không cần tắt:** Nếu nguyên nhân đơ là **thrashing RAM** (hết RAM, hệ thống kẹt cứng vì liên tục đẩy trang nhớ ra vào swap), ở giây thứ 60 watchdog sẽ kích hoạt `SysRq f` để gọi in-kernel **OOM Killer** tiêu diệt tiến trình ngốn RAM nhất. Trong đa số trường hợp, máy hồi phục ngay lập tức và bạn không mất phiên làm việc.
- **Giảm tối đa rủi ro hỏng filesystem `ext4`:** Khi buộc phải tắt máy ở giây thứ 300, watchdog gửi lần lượt `SysRq s` (*sync* — đẩy toàn bộ dữ liệu bẩn từ page cache trong RAM xuống SSD) rồi `SysRq u` (*unmount/remount read-only* — chuyển mọi phân vùng sang chỉ-đọc) trước khi gọi `SysRq o` (*poweroff*) hoặc `SysRq b` (*reboot*).
- **Để lại báo cáo nguyên nhân chính xác:** Trước khi tắt máy, watchdog ghi lại báo cáo đóng băng (`freeze-YYYYmmdd-HHMMSS.json`) chứa chuỗi mẫu đo mỗi giây một lần trong suốt lúc máy đơ, danh sách top tiến trình và các sự kiện lỗi kernel. Khi bật máy lại, `healthwatch report` sẽ chỉ rõ tại sao lần boot trước bị treo.

### 1.2. Kiến trúc sống sót khi máy đang đơ
Khi máy đơ vì nghẽn RAM hoặc SSD treo I/O, một tiến trình thông thường cũng sẽ bị đóng băng ngay khi cố đọc file trên đĩa hoặc cấp phát bộ nhớ. Để vẫn chạy được trong điều kiện khắc nghiệt đó, `pc-health-watch-watchdog.service` được thiết kế đặc biệt:

- **Chạy bằng quyền `root` từ đường dẫn an toàn:** Dịch vụ hệ thống chạy từ bản cài thuộc sở hữu `root` (`root-owned`) tại `/opt/pc-health-watch` và đọc cấu hình `root-owned` tại `/etc/pc-health-watch/watchdog.json`. Tuyệt đối không chạy mã quyền `root` trực tiếp từ thư mục của người dùng để tránh lỗ hổng leo thang đặc quyền.
- **Khóa bộ nhớ và ưu tiên tối đa:**
  - Gọi `mlockall()` ngay khi khởi động để khóa toàn bộ các trang bộ nhớ của tiến trình vào RAM vật lý, không bao giờ bị kernel đẩy ra swap.
  - Thiết lập `OOMScoreAdjust=-1000` (miễn nhiễm với OOM Killer), `Nice=-15` (ưu tiên lập lịch CPU cao) và `IOSchedulingClass=realtime`.
- **Vòng lặp chính 100% không chạm đĩa, không tạo tiến trình con:**
  - Trong vòng lặp giám sát, watchdog **không đọc đĩa** và **không gọi `fork`/`exec`** (vì tạo tiến trình con cần nạp binary/thư viện từ đĩa — nếu SSD đang treo I/O thì tiến trình gọi sẽ bị kẹt theo ở trạng thái `D` - *uninterruptible sleep*).
  - Watchdog chỉ tương tác với các pseudo-filesystem nằm hoàn toàn trong RAM: đọc `/proc`, `/sys`, đọc `/dev/kmsg` (ring buffer log của kernel trong RAM), đọc file nhịp tim trên `tmpfs` (`/run/user/<uid>/...`), và ghi lệnh điều khiển vào `/proc/sysrq-trigger`.
  - Việc ghi `/proc/sysrq-trigger` bằng quyền `root` luôn hoạt động trực tiếp với kernel, **không phụ thuộc** giá trị sysctl `kernel.sysrq`.

### 1.3. Ba tín hiệu phát hiện máy đơ
Mỗi giây một lần, watchdog đọc trường `total=` (tổng số micro-giây bị nghẽn tích lũy) từ `/proc/pressure/memory` và `/proc/pressure/io` (Linux PSI — *Pressure Stall Information*), rồi tự tính tỉ lệ phần trăm thời gian hệ thống bị stall thực tế trên cửa sổ trượt **10 giây**.

Máy bị coi là **bắt đầu đơ** khi **bất kỳ một** trong 3 tín hiệu sau duy trì liên tục từ `detect_seconds` trở lên (mặc định **30 giây**):

1. **Thrashing RAM (`memory full stall`):** Tỉ lệ `full` stall của `/proc/pressure/memory` ≥ **50%** thời gian (`memory_full_stall_percent`) liên tục ≥ 30s. Mọi tác vụ đều kẹt vì chờ cấp phát/swap trang nhớ.
2. **Treo I/O (`io full stall`):** Tỉ lệ `full` stall của `/proc/pressure/io` ≥ **80%** thời gian (`io_full_stall_percent`) liên tục ≥ 30s. Toàn bộ hệ thống đứng chờ ổ đĩa phản hồi.
3. **Giao diện đồ họa treo (`GUI hang`):** Daemon phía người dùng (`pc-health-watch.service`) thăm dò compositor/window manager mỗi **10 giây** một lần:
   - Với GNOME: gọi D-Bus `gdbus` (`org.freedesktop.DBus.Peer.Ping` tới `org.gnome.Shell`).
   - Với KDE Plasma (`KWin`), `Hyprland`, hoặc X server (`xset q`).
   - Kết quả được ghi vào file "nhịp tim" trên RAM tại `/run/user/<uid>/pc-health-watch-heartbeat.json` để watchdog hệ thống đọc.
   - **Quy tắc an toàn:** **CHỈ** khi lệnh thăm dò bị **TIMEOUT liên tục ≥ 45 giây** (`gui_hang_seconds`) thì mới tính là giao diện bị treo. Nếu giao diện không tồn tại, người dùng đã đăng xuất, hoặc đang ở TTY văn bản, trạng thái được ghi nhận là *"không biết"* và **không** kích hoạt cảnh báo đơ.

Ngoài ra, các sự kiện kernel nghiêm trọng đọc trực tiếp từ `/dev/kmsg` (như `soft lockup`, `hung task`, `GPU has fallen off the bus`, `NVMe timeout`…) được thu thập liên tục để đưa vào báo cáo làm bằng chứng chẩn đoán.

---

## 2. Sơ đồ dòng thời gian leo thang (Escalation Timeline)

Thời điểm `t = 0s` là lúc dấu hiệu đơ **bắt đầu** (không phải lúc xác nhận). Nhờ vậy máy được tắt sau đúng 5 phút kể từ khi bắt đầu đơ — khớp với thói quen tự chờ ~5 phút rồi giữ nút nguồn.

```text
t = 0s        : Tín hiệu vượt ngưỡng (bắt đầu thu mẫu 1 giây/lần vào RAM)
 │
 ├─► t = 30s   : Vẫn liên tục (detect_seconds) → XÁC NHẬN NGHI ĐƠ
 │               Ghi vào /dev/kmsg: "pc-health-watch: nghi máy đơ (lý do)"
 │
 ├─► t = 60s   : [Chỉ khi đơ do Thrashing RAM & oom_kill_after_seconds > 0]
 │               Kích SysRq 'f' (lần 1) ──► Kernel OOM Killer diệt tiến trình ngốn RAM nhất.
 │
 ├─► t = 120s  : [Nếu vẫn còn Thrashing RAM chưa hồi phục]
 │               Kích SysRq 'f' (lần 2 - tối đa 1 lần lặp lại).
 │
 ├─► t = 280s  : Khởi chạy luồng riêng (có timeout) ghi báo cáo đóng băng xuống đĩa:
 │               /var/lib/pc-health-watch/freeze-YYYYmmdd-HHMMSS.json
 │               (Nếu SSD treo cứng khiến luồng ghi bị kẹt, luồng chính vẫn đi tiếp).
 │
 └─► t = 300s  : Chạm ngưỡng action_after_seconds:
                 • Nếu mode = "log" (mặc định): Chỉ ghi log "lẽ ra đã tắt máy", không can thiệp.
                 • Nếu mode = "act":
                     1. Ghi SysRq 's' (Sync dữ liệu xuống đĩa)  ──► chờ 5 giây
                     2. Ghi SysRq 'u' (Remount read-only)       ──► chờ 2 giây
                     3. Ghi SysRq 'o' (poweroff - mặc định) hoặc 'b' (reboot) theo cấu hình "action"
```

> **Cơ chế tự hủy khi hồi phục (`recover_seconds`):** Ở bất kỳ thời điểm nào trước `300s` (ví dụ sau khi `SysRq f` diệt xong trình duyệt ngốn RAM ở giây 60), nếu **mọi dấu hiệu đơ biến mất liên tục trong 15 giây** (`recover_seconds`), watchdog sẽ hủy chu trình leo thang, xóa trạng thái cảnh báo và ghi log `"đã hồi phục"`.

| Mốc thời gian | Hành động | Điều kiện & Ghi chú |
| :--- | :--- | :--- |
| **`0s`** | Dấu hiệu bắt đầu | Bắt đầu lưu mẫu 1s/lần vào RAM. |
| **`30s`** | Xác nhận nghi đơ | Tín hiệu liên tục đủ `detect_seconds`; ghi `/dev/kmsg`: `"pc-health-watch: nghi máy đơ (lý do)"`. |
| **`60s`** | Giải cứu OOM lần 1 (`SysRq f`) | Chỉ chạy khi lý do là **thrashing RAM** và `oom_kill_after_seconds > 0`. |
| **`120s`** | Giải cứu OOM lần 2 (`SysRq f`) | Lặp lại tối đa 1 lần nữa nếu thrashing RAM vẫn tiếp diễn. |
| **`280s`** | Ghi báo cáo đóng băng | Ghi `/var/lib/pc-health-watch/freeze-YYYYmmdd-HHMMSS.json` trên luồng riêng có timeout. |
| **`300s`** | Kết thúc / Tắt máy an toàn | `mode="log"`: ghi `"lẽ ra đã tắt máy"`. `mode="act"`: `SysRq s` → chờ 5s → `SysRq u` → chờ 2s → `SysRq o`/`b`. |
| **Bất kỳ lúc nào** | **Hồi phục (`15s` sạch)** | Hết mọi dấu hiệu đơ liên tục `recover_seconds` (15s) → hủy leo thang, ghi `"đã hồi phục"`. |

---

## 3. Những gì Watchdog KHÔNG cứu được & Lớp bảo vệ bổ sung ở mức Kernel

Vì `pc-health-watch-watchdog` là một tiến trình userspace, nó chỉ chạy được khi kernel Linux vẫn còn lập lịch (schedule) cho các luồng CPU. Nếu xảy ra **kernel treo cứng toàn bộ** (hard lockup trên mọi core CPU, kernel panic tức thì do lỗi phần cứng RAM/CPU/PCIe, hoặc deadlock trong interrupt handler), không một phần mềm userspace nào còn chạy được.

Để bọc lót cho trường hợp này trên **Ubuntu 20.04**, bạn nên bật thêm 3 lớp bảo vệ ở tầng phần cứng và kernel dưới đây.

### 3.1. Hardware Watchdog thông qua `systemd`
Mainboard PC/laptop thường có sẵn bộ đếm thời gian phần cứng (Hardware Watchdog Timer — ví dụ chip Intel TCO `iTCO_wdt` hoặc AMD `sp5100_tco`). Khi được kích hoạt, `systemd` (PID 1) sẽ định kỳ "đá" (ping) bộ đếm này. Nếu kernel treo cứng toàn bộ khiến `systemd` không ping kịp, mạch phần cứng trên mainboard sẽ tự động reset máy.

- **Kiểm tra thiết bị phần cứng trên Ubuntu 20.04:**
  ```bash
  ls -l /dev/watchdog*
  sudo wdctl
  lsmod | grep -E 'iTCO_wdt|sp5100_tco'
  ```
- **Cấu hình (`systemd` 245 trên Ubuntu 20.04 hỗ trợ sẵn):**
  Sửa file `/etc/systemd/system.conf`, bỏ dấu `#` và đặt:
  ```ini
  RuntimeWatchdogSec=60s
  RebootWatchdogSec=2min
  ```
  Sau đó áp dụng ngay mà không cần reboot (`daemon-reload` KHÔNG đọc lại `system.conf`):
  ```bash
  sudo systemctl daemon-reexec
  ```
- **Ưu điểm:** Cứu được cả trường hợp kernel chết cứng hoàn toàn mà không cần người dùng bấm nút nguồn.
- **Nhược điểm & Lưu ý:** Thời gian tối đa (`max_timeout`) phụ thuộc giới hạn phần cứng của từng chip (kiểm tra bằng `sudo wdctl`; nhiều dòng `iTCO_wdt` giới hạn tối đa khoảng 30–120 giây). Ngoài ra, reset bằng hardware watchdog là cắt ngang ở mức mạch điện nên không có bước `sync`/`unmount` và không ghi được file báo cáo JSON.

### 3.2. Tự động Reboot khi Kernel Panic / Lockup (`sysctl`)
Kernel Linux có các bộ phát hiện lỗi nội bộ cho *soft lockup* (CPU kẹt trong kernel mode > 20s), *hard lockup* (CPU không phản hồi ngắt NMI) và *hung task* (tiến trình kẹt ở trạng thái `D` quá 120s). Mặc định Ubuntu chỉ in stack trace ra log chứ không khởi động lại.

- **Cấu hình trên Ubuntu 20.04:** Tạo file `/etc/sysctl.d/99-panic.conf`:
  ```ini
  # Tự động reboot sau 10 giây khi xảy ra Kernel Panic
  kernel.panic = 10

  # Chuyển soft lockup và hard lockup thành Kernel Panic để tự reboot
  kernel.softlockup_panic = 1
  kernel.hardlockup_panic = 1

  # Tùy chọn (CẨN THẬN): panic khi có tiến trình kẹt I/O (D-state) > 120s
  # kernel.hung_task_panic = 0
  ```
  Áp dụng bằng lệnh `sudo sysctl --system`.
- **Ưu điểm:** Tự thoát khỏi trạng thái kernel treo vòng lặp vô tận.
- **Nhược điểm (Cảnh báo quan trọng về `hung_task_panic`):** **Không nên** bật `kernel.hung_task_panic = 1` trên máy tính cá nhân thông thường. Khi bạn chép dữ liệu lớn ra USB 2.0 chậm, thẻ nhớ SD hoặc ổ mạng NFS bị nghẽn quá 120 giây, tiến trình chép file sẽ nằm ở trạng thái `D` và khiến kernel tưởng nhầm là máy treo rồi tự reboot đột ngột làm mất dữ liệu.

### 3.3. Lưu log Kernel qua lần Reboot cứng (`pstore` & `netconsole`)
Khi máy sập vì Kernel Panic hoặc Hardware Watchdog reset, `systemd-journald` không kịp ghi những dòng log cuối cùng xuống SSD.

- **Dùng `pstore` (Persistent Storage trên bộ nhớ NVRAM của UEFI):**
  Ubuntu 20.04 đi kèm `systemd` 245 đã có sẵn dịch vụ `systemd-pstore.service` (hỗ trợ từ `systemd` 243). Khi kernel panic, các dòng `dmesg` cuối cùng được ghi vào vùng nhớ EFI NVRAM; ở lần khởi động kế tiếp, `systemd-pstore.service` tự động chép chúng ra đĩa tại `/var/lib/systemd/pstore/`.
  ```bash
  # Kiểm tra backend pstore đang hoạt động (thường là efi)
  cat /sys/module/pstore/parameters/backend
  sudo systemctl status systemd-pstore.service
  ls -la /var/lib/systemd/pstore/
  ```
  Để kernel tự động dump log vào `pstore` ngay cả khi panic, thêm tham số `printk.always_kmsg_dump=1` vào `GRUB_CMDLINE_LINUX_DEFAULT` trong `/etc/default/grub` rồi chạy `sudo update-grub`.
  - *Ưu điểm:* Không cần máy thứ hai, giữ được stack trace của chính khoảnh khắc kernel chết.
  - *Nhược điểm:* Dung lượng NVRAM UEFI rất nhỏ (chỉ lưu được vài chục KB cuối cùng) và một số BIOS giá rẻ không bật sẵn vùng EFI pstore.
- **Dùng `netconsole` (nhắc ngắn):** Nếu có máy tính thứ hai trong mạng LAN, bạn có thể nạp module kernel `netconsole` để bắn trực tiếp mọi gói tin `/dev/kmsg` qua giao thức UDP tới máy kia theo thời gian thực. Ngay cả khi SSD và màn hình chết tức khắc, máy nhận vẫn lưu được trọn vẹn dòng log cuối cùng.

---

## 4. Cài đặt, Cấu hình và Cách sử dụng

### 4.1. Cài đặt và các lệnh quản lý
Trình cài đặt sẽ sao chép mã nguồn sang `/opt/pc-health-watch` (thuộc quyền `root:root`), tạo cấu hình mặc định tại `/etc/pc-health-watch/watchdog.json`, hỏi bạn chế độ vận hành (`mode`) và kích hoạt dịch vụ systemd:

```bash
# Cài đặt hoặc cập nhật freeze watchdog (chạy KHÔNG kèm sudo — script tự gọi sudo;
# lần đầu sẽ hỏi: bật act hay giữ log, poweroff hay reboot)
./install.sh --watchdog

# Kiểm tra trạng thái dịch vụ
sudo systemctl status pc-health-watch-watchdog

# Xem log hoạt động của watchdog theo thời gian thực
journalctl -u pc-health-watch-watchdog -f

# TẮT KHẨN CẤP watchdog (dừng ngay và không tự bật lại khi boot)
sudo systemctl disable --now pc-health-watch-watchdog
```

### 4.2. File cấu hình `/etc/pc-health-watch/watchdog.json`
File này thuộc sở hữu của `root`. Sau khi chỉnh sửa, chạy `sudo systemctl restart pc-health-watch-watchdog` để áp dụng:

```json
{
  "mode": "log",
  "action": "poweroff",
  "action_after_seconds": 300,
  "detect_seconds": 30,
  "recover_seconds": 15,
  "oom_kill_after_seconds": 60,
  "memory_full_stall_percent": 50,
  "io_full_stall_percent": 80,
  "gui_hang_seconds": 45
}
```

| Khóa cấu hình | Mặc định | Ý nghĩa |
| :--- | :---: | :--- |
| `mode` | `"log"` | `"log"`: chỉ ghi nhận và tạo báo cáo, không tắt máy; `"act"`: thực thi OOM-kill và tắt/khởi động lại máy thật; `"off"`: vô hiệu hóa watchdog. |
| `action` | `"poweroff"` | Lệnh cuối cùng ở giây 300 khi `mode="act"`: `"poweroff"` (`SysRq o`) hoặc `"reboot"` (`SysRq b`). |
| `action_after_seconds` | `300` | Thời gian (giây) từ lúc dấu hiệu đơ bắt đầu đến lúc thực thi `action` (mặc định 5 phút; tối thiểu 60). |
| `detect_seconds` | `30` | Thời gian (giây) tín hiệu phải vượt ngưỡng liên tục để xác nhận máy đơ (tối thiểu 10). |
| `recover_seconds` | `15` | Thời gian (giây) hết mọi tín hiệu đơ liên tục để hủy leo thang và đánh dấu `"đã hồi phục"`. |
| `oom_kill_after_seconds` | `60` | Thời điểm (giây) kích hoạt `SysRq f` khi đơ do thrashing RAM (lặp lại tối đa 1 lần ở `120s`; đặt `0` để tắt). |
| `memory_full_stall_percent` | `50` | Ngưỡng `%` thời gian PSI memory `full` stall trên cửa sổ 10s để tính là thrashing RAM. |
| `io_full_stall_percent` | `80` | Ngưỡng `%` thời gian PSI I/O `full` stall trên cửa sổ 10s để tính là treo I/O. |
| `gui_hang_seconds` | `45` | Thời gian (giây) lệnh thăm dò giao diện đồ họa bị timeout liên tục để tính là treo GUI. |

### 4.3. Quy trình chuyển từ `"log"` sang `"act"` và đọc báo cáo
1. **Giai đoạn chạy thử (`"mode": "log"`):** Khi mới cài đặt, hãy giữ nguyên chế độ mặc định `"log"` trong vài ngày làm việc bình thường. Ở chế độ này, nếu phát hiện đơ, watchdog vẫn ghi dòng cảnh báo vào `/dev/kmsg`, thu mẫu 1 giây/lần, xuất báo cáo ở giây 280 và ở giây 300 chỉ ghi vào log `"lẽ ra đã tắt máy"` chứ không đụng vào máy của bạn.
2. **Kiểm tra xem có báo nhầm không:** Theo dõi `journalctl -u pc-health-watch-watchdog`. Nếu trong vài ngày sử dụng nặng (biên dịch code, chép file, chơi game) không có lần nào báo nhầm, hoặc các ngưỡng đã được tinh chỉnh phù hợp, hãy đổi `"mode"` thành `"act"` trong `/etc/pc-health-watch/watchdog.json` rồi khởi động lại dịch vụ.
3. **Đọc báo cáo sau khi máy đơ:**
   - Ngay sau khi máy được bật lại, chạy lệnh:
     ```bash
     healthwatch report
     ```
   - Lệnh này sẽ tự động tìm và hiển thị báo cáo đóng băng của lần boot trước từ `/var/lib/pc-health-watch/freeze-YYYYmmdd-HHMMSS.json` (bao gồm lý do kích hoạt, diễn biến PSI memory/IO theo từng giây, top tiến trình ngốn tài nguyên và các lỗi kernel bắt được từ `/dev/kmsg`).

---

## 5. Cách thử nghiệm an toàn (Safe Testing)

> **⚠️ NGUYÊN TẮC AN TOÀN TRƯỚC KHI THỬ:**
> 1. **Đóng và lưu toàn bộ công việc quan trọng** trước khi chạy các lệnh mô phỏng đơ máy.
> 2. **Bắt buộc kiểm tra `/etc/pc-health-watch/watchdog.json` đang để `"mode": "log"`** để máy không bị tắt thật trong lúc thử nghiệm.
> 3. Mở sẵn một cửa sổ terminal chạy `journalctl -u pc-health-watch-watchdog -f` hoặc `sudo dmesg -w` để quan sát trực tiếp phản ứng của watchdog.

### 5.1. Thử nghiệm phát hiện Thrashing RAM với `stress-ng`
Gói `stress-ng` có sẵn trên kho phần mềm chuẩn của Ubuntu 20.04 (`sudo apt install stress-ng`). Lệnh dưới đây tạo 2 worker cấp phát và chạm liên tục vào `110%` dung lượng RAM vật lý trong 120 giây để ép hệ thống rơi vào trạng thái swap thrashing:

```bash
# Đảm bảo đang ở mode="log" và đã lưu mọi tài liệu đang mở!
stress-ng --vm 2 --vm-bytes 110% --timeout 120s
```

- **Kết quả kỳ vọng:** Sau khoảng 30 giây (`detect_seconds`) khi PSI memory `full` stall vượt 50%, bạn sẽ thấy dòng `"pc-health-watch: nghi máy đơ (memory)"` xuất hiện trong `dmesg` / `journalctl`. Khi `stress-ng` kết thúc (hoặc bạn nhấn `Ctrl+C`), sau 15 giây (`recover_seconds`) watchdog sẽ ghi nhận `"đã hồi phục"`.

### 5.2. Thử nghiệm phát hiện Giao diện đồ họa treo (GUI Hang)
Bạn có thể giả lập `gnome-shell` bị đơ cứng bằng cách gửi tín hiệu `SIGSTOP` để tạm dừng tiến trình, rồi dùng `SIGCONT` để đánh thức nó lại sau 60–90 giây.

> **⚠️ CẢNH BÁO X11 vs. WAYLAND:**
> - **Chỉ thực hiện cách này trên phiên X11** (kiểm tra bằng `echo $XDG_SESSION_TYPE` trả về `x11`) và **phải chạy lệnh từ TTY văn bản (`Ctrl+Alt+F3`) hoặc qua phiên SSH từ máy khác**. Nếu bạn chạy `kill -STOP` ngay trong ứng dụng Terminal nằm bên trong giao diện GNOME, toàn bộ màn hình đồ họa sẽ đứng yên khiến bạn không thể gõ tiếp lệnh `kill -CONT`!
> - **Trên Wayland:** `gnome-shell` đồng thời là display server quản lý toàn bộ input/output; việc dừng `gnome-shell` trên Wayland có thể làm treo cứng cả phiên đăng nhập hoặc không chuyển sang TTY được. Nếu dùng Wayland, chỉ thử khi bạn đang kết nối vào máy qua **SSH** từ một thiết bị khác.

Quy trình thực hiện từ **TTY3 (`Ctrl+Alt+F3`)** hoặc qua **SSH**:

```bash
# 1. Đóng băng tiến trình gnome-shell
kill -STOP $(pgrep -x gnome-shell)

# 2. Chờ 60–90 giây (lớn hơn gui_hang_seconds=45s) và quan sát log:
#    journalctl -u pc-health-watch-watchdog -f
#    (Daemon người dùng ping D-Bus bị timeout >= 45s -> watchdog báo nghi đơ GUI)

# 3. Đánh thức lại gnome-shell để giao diện hoạt động bình thường
kill -CONT $(pgrep -x gnome-shell)
```

Sau khi chạy `kill -CONT`, chờ 15 giây (`recover_seconds`), watchdog sẽ ghi nhận giao diện phản hồi trở lại và báo `"đã hồi phục"`, sau đó nhấn `Ctrl+Alt+F2` (hoặc `F1`/`F7`) để quay về màn hình đồ họa.

---

## 6. Câu hỏi thường gặp (FAQ)

### Hỏi: Đang chép file dung lượng lớn hoặc compile dự án nặng thì có bị báo nhầm là đơ không?
**Đáp:** Rất hiếm khi bị báo nhầm. Watchdog sử dụng chỉ số PSI **`full` stall** chứ không dùng `% CPU` hay `% I/O utilization`.
- Khi compile nặng (CPU 100%), nếu vẫn đủ RAM thì `memory full stall` gần như bằng `0%`.
- Khi chép file lớn, chỉ số `io some` có thể cao, nhưng `io full stall ≥ 80%` kéo dài liên tục suốt **30 giây** chỉ xảy ra khi gần như **mọi luồng trong hệ thống** đều bị chặn hoàn toàn vì chờ ổ đĩa (tức là máy thực sự đã đơ không thao tác được gì).
- Nếu máy bạn dùng ổ cứng HDD cơ học cũ hoặc thường xuyên chạy tác vụ I/O cực nặng khiến `mode="log"` ghi nhận cảnh báo sớm, bạn chỉ cần tăng `io_full_stall_percent` (ví dụ lên `90`) hoặc tăng `detect_seconds` (ví dụ lên `60`) trong `/etc/pc-health-watch/watchdog.json`.

### Hỏi: Khi gập máy ngủ (Suspend / Sleep) rồi mở lại thì watchdog có tính nhầm thời gian ngủ là máy đơ không?
**Đáp:** Không. Trong thời gian hệ thống ở trạng thái Suspend (`s2idle` hoặc `deep`), bộ đếm stall của `/proc/pressure/*` không tăng và vòng lặp đo cửa sổ trượt 10 giây không tích lũy stall giả. Với giao diện đồ họa, chỉ khi lệnh thăm dò đang chạy mà bị **timeout liên tục ≥ 45 giây** lúc máy đang thức mới tính là treo.

### Hỏi: Khi tôi đăng xuất (Log out) ra màn hình đăng nhập GDM hoặc làm việc trên màn hình đen TTY thì sao?
**Đáp:** Không bị báo nhầm. Khi bạn đăng xuất hoặc không mở phiên đồ họa, daemon người dùng không có tiến trình `gnome-shell`/`KWin`/`Hyprland` hay X server để ping. Theo thiết kế, trường hợp giao diện không tồn tại hoặc đã đăng xuất được đánh dấu là **"không biết"** và bị bỏ qua; watchdog chỉ kích hoạt khi giao diện **đang tồn tại nhưng lệnh thăm dò bị timeout liên tục ≥ 45 giây**. Trong lúc đó, hai bộ dò cấp hệ thống là **Thrashing RAM** và **Treo I/O** vẫn bảo vệ máy bình thường.

### Hỏi: Máy có nhiều người dùng (Multi-user) cùng đăng nhập thì nhịp tim giao diện hoạt động thế nào?
**Đáp:** Mỗi người dùng đăng nhập vào phiên đồ họa sẽ chạy một instance `pc-health-watch.service` (systemd `--user`) riêng và ghi file nhịp tim vào thư mục runtime tương ứng của mình tại `/run/user/<uid>/pc-health-watch-heartbeat.json`. Dịch vụ hệ thống `pc-health-watch-watchdog.service` (chạy bằng `root`) có quyền đọc các file nhịp tim trong `/run/user/*/pc-health-watch-heartbeat.json`. Nếu phiên đồ họa không hoạt động hoặc người dùng đó đã đăng xuất thì không tính; chỉ khi một phiên đồ họa đang chạy thực sự bị timeout thăm dò liên tục ≥ `gui_hang_seconds` (45s) thì tín hiệu treo giao diện mới được kích hoạt.
