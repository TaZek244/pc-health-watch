# Cẩm nang Chẩn đoán & Xử lý Ubuntu 20.04 Đơ cứng (Troubleshooting Checklist)

Tài liệu này dành cho người dùng **Ubuntu 20.04 LTS** (đặc biệt trên máy **dual-boot** cài trên SSD riêng) gặp hiện tượng máy đơ cứng, đứng hình, chuột/bàn phím không phản hồi và phải bấm nút nguồn/reset cứng.

Kết hợp tài liệu này với hộp đen (`~/.local/state/pc-health-watch/blackbox-YYYY-MM-DD.jsonl`) và lệnh `healthwatch report` của **pc-health-watch** để khoanh vùng nguyên nhân nhanh nhất.

---

## Mục lục

1. [Checklist 10 nguyên nhân phổ biến gây đơ cứng](#1-checklist-10-nguyên-nhân-phổ-biến-gây-đơ-cứng)
2. [Xử lý ngay khi máy đang đơ (Trước khi bấm nút nguồn)](#2-xử-lý-ngay-khi-máy-đang-đơ-trước-khi-bấm-nút-nguồn)
3. [Điều tra sau khi phải Reset cứng (Post-mortem)](#3-điều-tra-sau-khi-phải-reset-cứng-post-mortem)
4. [Các tham số Kernel (GRUB) hữu ích để thử nghiệm](#4-các-tham-số-kernel-grub-hữu-ích-để-thử-nghiệm)

---

## 1. Checklist 10 nguyên nhân phổ biến gây đơ cứng

### 1.1. Hết RAM dẫn đến Thrashing (Swap nghẽn, thiếu Userspace OOM Killer)
Ubuntu 20.04 mặc định chỉ tạo `/swapfile` 2GB và **không** cài sẵn `systemd-oomd` hay userspace OOM killer. Khi mở nhiều tab trình duyệt hoặc IDE nặng, kernel liên tục đẩy trang nhớ ra vào swap (thrashing), làm I/O bão hòa và giao diện GNOME đứng cứng hoàn toàn trước khi kernel kịp kích hoạt in-kernel OOM killer.

- **Triệu chứng:** Chuột giật cục rồi đứng hẳn, đèn ổ cứng sáng liên tục, âm thanh có thể lặp lại vài giây cuối, máy đơ khi mở nhiều tab Chrome/Firefox/VS Code. Trong hộp đen `pc-health-watch`, `memory.available_mb` tụt sát 0, `pressure.memory.full_avg10` và `pressure.io.full_avg10` tăng vọt.
- **Cách xác nhận:**
  ```bash
  free -h
  swapon --show
  cat /proc/pressure/memory
  journalctl -k -b -1 | grep -i -E 'oom|out of memory|killed process'
  ```
- **Cách khắc phục:**
  - [ ] Cài đặt `earlyoom` để chủ động diệt tiến trình ngốn RAM trước khi máy treo cứng:
    ```bash
    sudo apt update && sudo apt install earlyoom
    sudo systemctl enable --now earlyoom
    ```
  - [ ] Bật nén RAM với `zram-config`:
    ```bash
    sudo apt install zram-config
    ```
  - [ ] Tăng kích thước `/swapfile` (ví dụ lên 4GB hoặc 8GB):
    ```bash
    sudo swapoff /swapfile
    sudo fallocate -l 8G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    ```
  - [ ] Giới hạn RAM của trình duyệt (bật *Memory Saver* hoặc dùng extension đóng tab ngầm).

---

### 1.2. Lỗi Driver GPU (NVIDIA Proprietary / Nouveau / AMD / Intel i915)
Driver đồ họa xung đột với kernel hoặc Xorg/Wayland là thủ phạm hàng đầu khiến màn hình đóng băng nhưng nhạc nền hoặc SSH đôi khi vẫn chạy.

- **Triệu chứng:** Màn hình đứng yên, chớp tắt hoặc rác hình (artifacts); chuột không di chuyển được hoặc di chuyển được nhưng không bấm được gì; xảy ra khi xem video có tăng tốc phần cứng, chơi game, hoặc dùng nhiều màn hình.
- **Cách xác nhận:**
  ```bash
  lspci -nnk | grep -A3 -E 'VGA|3D'
  ubuntu-drivers devices
  journalctl -k -b -1 | grep -i -E 'NVRM|Xid|nouveau|amdgpu|i915|drm|fallen off the bus|GPU hang'
  ```
  *(Lưu ý: lỗi `NVRM: Xid` hoặc `GPU has fallen off the bus` là dấu hiệu điển hình của GPU NVIDIA bị treo hoặc mất kết nối PCIe).*
- **Cách khắc phục:**
  - [ ] **Với NVIDIA:** Thay driver open-source `nouveau` bằng driver proprietary được khuyến nghị (ví dụ `nvidia-driver-535`):
    ```bash
    sudo ubuntu-drivers autoinstall
    # Hoặc cài bản cụ thể:
    sudo apt install nvidia-driver-535
    ```
  - [ ] **Với Intel (`i915`) / AMD (`amdgpu`) đời mới:** Nâng cấp lên kernel HWE (5.15) thay vì kernel GA (5.4) để có driver DRM mới hơn (xem mục 1.9).
  - [ ] Tắt Hardware Acceleration trong trình duyệt nếu chỉ đơ khi xem YouTube/WebGL.

---

### 1.3. Lỗi SSD (NVMe APST, SATA ALPM, Firmware hoặc Cáp/Khe cắm)
Một số SSD NVMe gặp lỗi firmware khi chuyển trạng thái tiết kiệm điện sâu (APST - Autonomous Power State Transition) trên Linux khiến ổ biến mất khỏi bus PCIe. Với SSD SATA, tính năng tiết kiệm điện ALPM hoặc cáp SATA lỏng cũng gây treo I/O toàn hệ thống.

- **Triệu chứng:** Máy đang chạy bình thường bỗng nhiên không mở được ứng dụng mới, các cửa sổ đang mở báo lỗi I/O rồi treo cứng; sau khi bấm nút Reset, đôi khi BIOS không nhận ổ SSD (phải tắt hẳn nguồn bật lại mới nhận).
- **Cách xác nhận:**
  ```bash
  sudo apt install smartmontools nvme-cli
  sudo smartctl -a /dev/nvme0n1   # Hoặc /dev/sda cho SSD SATA
  sudo nvme smart-log /dev/nvme0n1
  dmesg | grep -i -E 'nvme|ata|ahci|I/O error|reset|timeout|DRDY'
  systemctl status fstrim.timer
  ```
- **Cách khắc phục:**
  - [ ] **Nếu NVMe bị timeout do APST:** Thêm tham số kernel `nvme_core.default_ps_max_latency_us=0` vào GRUB (xem mục 4) để tắt tiết kiệm điện sâu trên NVMe.
  - [ ] **Nếu SSD SATA bị lỗi link power (ALPM):** Kiểm tra `/sys/class/scsi_host/host*/link_power_management_policy` và thử chuyển sang `med_power_with_dipm` hoặc `max_performance`.
  - [ ] Đảm bảo `fstrim.timer` đang bật để duy trì hiệu năng SSD hàng tuần:
    ```bash
    sudo systemctl enable --now fstrim.timer
    ```
  - [ ] Kiểm tra và cập nhật firmware cho SSD (qua `fwupd` hoặc công cụ của hãng trên Windows), cắm lại khe M.2 hoặc thay cáp SATA nếu `UDMA_CRC_Error_Count` tăng.

---

### 1.4. Lỗi Filesystem `ext4` (Remount Read-Only)
Khi ổ đĩa bị mất kết nối đột ngột hoặc có bad block/metadata hỏng, kernel sẽ tự động chuyển phân vùng `/` sang chế độ chỉ đọc (`read-only`) để bảo vệ dữ liệu.

- **Triệu chứng:** Ứng dụng đột ngột văng, không tạo/lưu được file, gõ lệnh trong terminal báo `Read-only file system`, hoặc máy đứng ở màn hình `initramfs` khi khởi động lại. Trong hộp đen `pc-health-watch`, trường `disks[*].readonly` chuyển thành `true`.
- **Cách xác nhận:**
  ```bash
  findmnt /
  grep -w ro /proc/mounts
  journalctl -k -b -1 | grep -i -E 'EXT4-fs error|remount-ro|Buffer I/O error|journal_abort'
  ```
- **Cách khắc phục:**
  - [ ] **Không** chạy `fsck` trực tiếp trên phân vùng `/` đang được mount read-write.
  - [ ] Khởi động từ **Ubuntu Live USB** (hoặc vào Recovery Mode từ GRUB với filesystem chưa mount rw), xác định phân vùng bằng `lsblk` và sửa lỗi:
    ```bash
    sudo fsck.ext4 -f -y /dev/nvme0n1p2
    ```

---

### 1.5. Quá nhiệt (Overheating) và Thermal Throttling
Bụi bẩn bít khe tản nhiệt, keo tản nhiệt khô hoặc thiếu daemon quản lý nhiệt trên CPU Intel làm nhiệt độ chạm ngưỡng `crit_c` (thường 100°C), gây tụt xung thê thảm hoặc tự sập/treo.

- **Triệu chứng:** Quạt rú to liên tục, vỏ máy nóng rát, xung nhịp CPU tụt xuống 400-800 MHz làm máy đơ lết, hoặc máy đứng hình/tắt nguồn đột ngột khi tải nặng.
- **Cách xác nhận:**
  ```bash
  sudo apt install lm-sensors
  sudo sensors-detect --auto
  sensors
  journalctl -k -b -1 | grep -i -E 'thermal|throttling|above threshold|overheating'
  ```
- **Cách khắc phục:**
  - [ ] Cài đặt và kích hoạt `thermald` (đặc biệt quan trọng cho CPU Intel):
    ```bash
    sudo apt install thermald
    sudo systemctl enable --now thermald
    ```
  - [ ] Vệ sinh quạt, khe tản nhiệt và tra lại keo tản nhiệt nếu nhiệt độ idle > 65°C hoặc tải nhẹ đã lên > 90°C.

---

### 1.6. Xung đột Dual-Boot với Windows (Fast Startup, RTC, Secure Boot)
Dù Ubuntu cài trên ổ SSD riêng, việc dùng chung máy với Windows vẫn gây ra nhiều vấn đề phần cứng và phân vùng.

- **Triệu chứng:**
  1. Ổ NTFS dùng chung không ghi được hoặc gây treo file manager khi truy cập.
  2. Giờ hệ thống bị lệch 7 tiếng mỗi khi chuyển qua lại giữa Windows và Ubuntu.
  3. Cài xong driver NVIDIA trên Ubuntu nhưng sau khi reboot vẫn chạy độ phân giải thấp hoặc treo ở màn hình đen do Secure Boot chặn kernel module chưa ký.
  4. Card Wi-Fi/Bluetooth hoặc âm thanh không hoạt động khi reboot ấm từ Windows sang Ubuntu.
- **Cách xác nhận:**
  ```bash
  timedatectl status
  mokutil --sb-state
  dmesg | grep -i -E 'ntfs|hibernat|secure boot|signature'
  ```
- **Cách khắc phục:**
  - [ ] **Tắt Fast Startup và Hibernation trên Windows:** Mở Command Prompt (Admin) trên Windows và chạy `powercfg /h off`. Khi chuyển từ Windows sang Ubuntu, nên **Shut down** hoàn toàn thay vì Restart nếu thiết bị ngoại vi bị kẹt firmware.
  - [ ] **Đồng bộ đồng hồ phần cứng (RTC):** Đặt Ubuntu dùng giờ local giống Windows (hoặc chỉnh registry Windows dùng UTC):
    ```bash
    timedatectl set-local-rtc 1 --adjust-system-clock
    ```
  - [ ] **Secure Boot + NVIDIA:** Khi cài `nvidia-driver`, đặt mật khẩu MOK và chọn **Enroll MOK** ở màn hình xanh khi reboot; hoặc tắt Secure Boot trong BIOS/UEFI nếu không bắt buộc.

---

### 1.7. Lỗi Suspend / Resume (Đơ khi gập/mở nắp máy)
Nhiều laptop hiện đại mặc định dùng chế độ ngủ `s2idle` (Modern Standby) thay vì `deep` (S3 truyền thống), hoặc ngược lại BIOS triển khai S3 lỗi, dẫn đến khi mở nắp máy màn hình tối đen, đèn nguồn sáng nhưng máy đơ cứng.

- **Triệu chứng:** Máy chạy ổn định suốt cả ngày nhưng cứ gập màn hình (suspend) hoặc để máy tự sleep rồi đánh thức (resume) là màn hình đen thui, quạt quay nhưng không lên hình.
- **Cách xác nhận:**
  ```bash
  cat /sys/power/mem_sleep
  journalctl -b -1 | grep -i -E 'PM: suspend|acpi|freeze|wakeup'
  ```
  *(Chế độ đang được chọn sẽ nằm trong ngoặc vuông, ví dụ `[s2idle] deep`).*
- **Cách khắc phục:**
  - [ ] Thử đổi chế độ `mem_sleep` bằng tham số kernel `mem_sleep_default=deep` (hoặc `mem_sleep_default=s2idle` nếu đang ở `deep` mà bị lỗi) trong GRUB.
  - [ ] Với card NVIDIA, đảm bảo các dịch vụ `nvidia-suspend.service`, `nvidia-hibernate.service`, `nvidia-resume.service` đã được bật.

---

### 1.8. Lỗi phần cứng RAM (Bad RAM)
Một thanh RAM bị lỗi bit ở vùng địa chỉ cao có thể khiến máy chạy bình thường khi dùng ít RAM, nhưng ngay khi chạm tới địa chỉ lỗi sẽ gây Kernel Panic hoặc đơ cứng tức thì mà không kịp ghi log xuống đĩa.

- **Triệu chứng:** Máy đơ cứng ngẫu nhiên không theo quy luật (lúc đang lướt web nhẹ, lúc đang code), kèm lỗi `general protection fault`, `BUG: unable to handle page fault`, hoặc ứng dụng tự crash với `Segmentation fault`.
- **Cách xác nhận:**
  ```bash
  journalctl -k -b -1 | grep -i -E 'mce|hardware error|page fault|general protection|EDAC'
  ```
- **Cách khắc phục:**
  - [ ] Khởi động lại máy, giữ phím `Shift` (BIOS/Legacy) hoặc nhấn `Esc` (UEFI) để vào menu GRUB → chọn **Memory test (memtest86+)**.
  - [ ] Nếu máy boot UEFI (memtest86+ đi kèm Ubuntu 20.04 không chạy được trên UEFI thuần), tạo USB boot **MemTest86** bản UEFI chính thức và chạy tối thiểu 2-4 pass. Nếu có bất kỳ dòng đỏ nào: tháo lau chân RAM, tắt XMP/DOCP trong BIOS, hoặc thay thanh RAM lỗi.

---

### 1.9. Kernel 5.4 (GA) quá cũ so với phần cứng mới
Ubuntu 20.04 ban đầu đi kèm kernel **5.4** (General Availability). Nếu máy dùng CPU Intel Gen 11/12+, AMD Ryzen 5000+ hoặc SSD/Wi-Fi đời mới, kernel 5.4 thiếu nhiều bản vá quản lý điện năng và driver, dễ gây freeze ngẫu nhiên.

- **Triệu chứng:** `uname -r` trả về `5.4.0-xxx-generic` trên phần cứng sản xuất từ 2020 trở về sau; hay gặp lỗi Wi-Fi rớt, đồ họa giật, hoặc CPU lockup.
- **Cách xác nhận:**
  ```bash
  uname -r
  dpkg -l | grep linux-generic
  ```
- **Cách khắc phục:**
  - [ ] Cài đặt nhánh kernel **HWE (Hardware Enablement)** để nâng kernel lên **5.15** (cùng nhánh với Ubuntu 22.04):
    ```bash
    sudo apt update
    sudo apt install --install-recommends linux-generic-hwe-20.04
    ```

---

### 1.10. Ubuntu 20.04 đã hết hạn Standard Support (04/2025)
Ubuntu 20.04 LTS (Focal Fossa) đã kết thúc 5 năm hỗ trợ tiêu chuẩn vào **tháng 04/2025**. Nếu không bật ESM, máy sẽ không còn nhận được các bản vá lỗi kernel và bảo mật mới.

- **Triệu chứng:** `apt update` không còn nhận được bản cập nhật kernel/driver mới cho các lỗi phần cứng hoặc bảo mật phát sinh sau 04/2025.
- **Cách xác nhận:**
  ```bash
  lsb_release -a
  pro status   # Công cụ từ gói ubuntu-advantage-tools
  ```
- **Cách khắc phục:**
  - [ ] **Phương án 1 (Khuyến nghị):** Sao lưu dữ liệu và nâng cấp lên **Ubuntu 22.04 LTS** hoặc **24.04 LTS** (`sudo do-release-upgrade`) để có kernel 6.x, `systemd-oomd`, driver GPU/NVMe mới và hỗ trợ lâu dài.
  - [ ] **Phương án 2 (Nếu bắt buộc giữ 20.04):** Đăng ký **Ubuntu Pro (ESM - Expanded Security Maintenance)** (miễn phí cho tối đa 5 máy cá nhân) để tiếp tục nhận bản vá kernel đến năm 2030:
    ```bash
    sudo pro attach <TOKEN_CỦA_BẠN>
    ```

---

## 2. Xử lý ngay khi máy đang đơ (Trước khi bấm nút nguồn)

Khi màn hình đứng cứng, **đừng vội giữ nút nguồn 5 giây** vì sẽ làm mất dữ liệu trong page cache, dễ hỏng filesystem `ext4` và làm mất log kernel cuối cùng. Hãy thử lần lượt từ nhẹ đến nặng (chờ 5-10 giây ở mỗi bước):

### Bước 1: Thử thoát khỏi giao diện đồ họa (TTY hoặc SSH)
1. Nhấn **`Ctrl + Alt + F3`** (hoặc `F2` đến `F6`) để chuyển sang màn hình console TTY3.
   - Nếu vào được TTY: đăng nhập rồi chạy `htop` hoặc `killall -9 chrome` / `sudo systemctl restart gdm3`.
2. Nếu có điện thoại hoặc máy tính thứ hai cùng mạng LAN, thử **SSH** vào máy đang đơ:
   ```bash
   ssh user@<IP_MAY_UBUNTU>
   ```
   *(Nếu SSH vẫn vào được: chỉ có Xorg/Wayland hoặc GPU bị treo, kernel vẫn sống — hãy chạy `dmesg -T | tail -n 50` để bắt ngay lỗi GPU).*

### Bước 2: Dùng phím cứu hộ Magic SysRq (Trực tiếp tới Kernel)
Kể cả khi Xorg treo hoàn toàn, miễn là kernel chưa panic cứng, tổ hợp phím **Magic SysRq** vẫn hoạt động.

> **Chuẩn bị trước (chạy 1 lần khi máy đang bình thường):**
> Ubuntu mặc định giới hạn một số phím SysRq (`176`). Hãy bật đầy đủ (`1`) bằng cách tạo file `/etc/sysctl.d/99-sysrq.conf`:
> ```bash
> echo "kernel.sysrq = 1" | sudo tee /etc/sysctl.d/99-sysrq.conf
> sudo sysctl --system
> ```
> *(Lưu ý: phím `SysRq` thường là phím **`PrtSc` / `Print Screen`**; trên một số laptop cần giữ thêm phím `Fn`).*

- **Cứu máy bị đơ do tràn RAM (không cần khởi động lại):**
  Nhấn tổ hợp **`Alt + SysRq + F`** → gọi trực tiếp in-kernel **OOM Killer** để diệt tiến trình ngốn nhiều RAM nhất. Chờ 5-10 giây xem chuột có sống lại không.
- **Khởi động lại an toàn (`R E I S U B`) khi không cứu được:**
  Giữ chặt **`Alt + SysRq`**, rồi bấm lần lượt từng phím dưới đây, **mỗi phím cách nhau 2-3 giây** (mẹo nhớ: *"**R**eboot **E**ven **I**f **S**ystem **U**tterly **B**roken"*):
  - **`R`** (*unRaw*): Lấy lại quyền điều khiển bàn phím từ Xorg.
  - **`E`** (*tErminate*): Gửi tín hiệu `SIGTERM` cho mọi tiến trình (trừ `init`) để tự đóng sạch.
  - **`I`** (*kIll*): Gửi tín hiệu `SIGKILL` buộc đóng các tiến trình còn ngoan cố.
  - **`S`** (*Sync*): Đồng bộ (flush) toàn bộ dữ liệu đang nằm trong cache RAM xuống ổ SSD (**rất quan trọng** để tránh lỗi `ext4` và giữ lại log).
  - **`U`** (*Unmount*): Remount mọi filesystem sang chế độ `read-only` an toàn.
  - **`B`** (*reBoot*): Khởi động lại máy ngay lập tức.

---

## 3. Điều tra sau khi phải Reset cứng (Post-mortem)

### 3.1. Đảm bảo Systemd Journal lưu bền vững trên ổ cứng
Trước khi điều tra, hãy kiểm tra xem `systemd-journald` đã lưu log của các lần boot trước xuống đĩa chưa:
```bash
journalctl --list-boots
```
Nếu chỉ thấy đúng 1 dòng (`0 ...` của lần boot hiện tại), hãy bật lưu trữ bền vững (`persistent`):
```bash
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
# Hoặc đặt Storage=persistent trong /etc/systemd/journald.conf
sudo sed -i 's/^#\?Storage=.*/Storage=persistent/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
```

### 3.2. Các lệnh kiểm tra chuẩn sau khi vừa khởi động lại
Chạy lần lượt 4 lệnh sau để xem điều gì đã xảy ra ngay trước giây phút máy đơ ở lần boot trước (`-b -1`):

1. **Chạy báo cáo hộp đen của `pc-health-watch`:**
   ```bash
   python3 -m healthwatch report
   # Hoặc nếu đã cài entry point:
   healthwatch report
   ```
   *(Lệnh này đọc các bản ghi `sample`, `event`, `alert` đã được `fsync` liên tục trong `~/.local/state/pc-health-watch/blackbox-YYYY-MM-DD.jsonl` của `boot_id` bị đứt quãng không có bản ghi `stop`).*
2. **Xem các dòng log cuối cùng của toàn hệ thống ở lần boot trước:**
   ```bash
   journalctl -b -1 -e
   ```
3. **Lọc riêng cảnh báo và lỗi từ Kernel (`warning` trở lên) ở lần boot trước:**
   ```bash
   journalctl -k -b -1 -p warning
   ```
4. **Kiểm tra lịch sử crash / reboot bất thường:**
   ```bash
   last -x | head -n 20
   ```
   *(Nếu dòng `reboot` đè lên phiên `still logged in` hoặc `crash` mà không có `shutdown` đứng trước, đó là lần reset cứng).*

### 3.3. Bảng tra cứu nhanh: "Thấy gì → Nghi gì"

| Dấu hiệu trong `journalctl` / `healthwatch report` | Nghi ngờ nguyên nhân chính | Xem mục |
| :--- | :--- | :---: |
| `memory.available_mb` < 300MB, `swap_used_percent` > 90%, `pressure.memory.full_avg10` cao liên tục; log hoàn toàn im lặng trước khi đứt | **Thrashing do hết RAM** (kernel bận swap đến mức không ghi được log mới) | [1.1](#11-hết-ram-dẫn-đến-thrashing-swap-nghẽn-thiếu-userspace-oom-killer) |
| `Out of memory: Killed process ...` hoặc `page allocation failure` | **Hết RAM + Swap** (OOM Killer đã chạy nhưng quá muộn) | [1.1](#11-hết-ram-dẫn-đến-thrashing-swap-nghẽn-thiếu-userspace-oom-killer) |
| `NVRM: Xid (PCI:0000:01:00): ...` hoặc `NVRM: GPU at ... has fallen off the bus` | **Lỗi Driver hoặc nguồn/nhiệt GPU NVIDIA** | [1.2](#12-lỗi-driver-gpu-nvidia-proprietary--nouveau--amd--intel-i915) |
| `[drm:amdgpu_job_timedout]`, `i915 ... Resetting chip for stopped heartbeat` | **Treo Driver GPU AMD / Intel i915** (cần lên kernel HWE 5.15) | [1.2](#12-lỗi-driver-gpu-nvidia-proprietary--nouveau--amd--intel-i915), [1.9](#19-kernel-54-ga-quá-cũ-so-với-phần-cứng-mới) |
| `nvme nvme0: I/O ... timeout, aborting` hoặc `controller is down` | **SSD NVMe treo do tiết kiệm điện APST** hoặc lỗi firmware | [1.3](#13-lỗi-ssd-nvme-apst-sata-alpm-firmware-hoặc-cápkhe-cắm) |
| `ata1.00: exception Emask ... timeout`, `DRDY ERR`, `blk_update_request: I/O error` | **SSD SATA lỗi cáp, khe cắm hoặc ALPM** | [1.3](#13-lỗi-ssd-nvme-apst-sata-alpm-firmware-hoặc-cápkhe-cắm) |
| `EXT4-fs error ... Remounting filesystem read-only` hoặc `disks[*].readonly = true` | **Hỏng cấu trúc ext4** (cần `fsck` từ Live USB) hoặc SSD rớt kết nối | [1.4](#14-lỗi-filesystem-ext4-remount-read-only) |
| `thermal[*].temp_c` ≥ 95°C, hoặc `CPUx: Package temperature above threshold` | **Quá nhiệt CPU**, thiếu `thermald` hoặc khô keo/bụi quạt | [1.5](#15-quá-nhiệt-overheating-và-thermal-throttling) |
| `watchdog: BUG: soft lockup - CPU#x stuck for 22s!` hoặc `rcu_sched self-detected stall` | **Xung đột Kernel / Driver / C-states** hoặc I/O chờ quá lâu | [1.9](#19-kernel-54-ga-quá-cũ-so-với-phần-cứng-mới), [4](#4-các-tham-số-kernel-grub-hữu-ích-để-thử-nghiệm) |
| `mce: [Hardware Error]: CPU ... Machine Check` hoặc `general protection fault` ngẫu nhiên | **Lỗi phần cứng RAM** hoặc CPU không ổn định | [1.8](#18-lỗi-phần-cứng-ram-bad-ram) |
| Log dừng ngay sau `PM: suspend entry (s2idle)` hoặc `(deep)` | **Lỗi Suspend / Resume** khi gập hoặc mở máy | [1.7](#17-lỗi-suspend--resume-đơ-khi-gậpmở-nắp-máy) |

---

## 4. Các tham số Kernel (GRUB) hữu ích để thử nghiệm

Nếu nghi ngờ lỗi tiết kiệm điện phần cứng (NVMe, PCIe, CPU C-states, GPU), bạn có thể thêm tham số boot vào dòng `GRUB_CMDLINE_LINUX_DEFAULT` trong `/etc/default/grub`.

> **⚠️ CẢNH BÁO QUAN TRỌNG:**
> - **Chỉ thử TỪNG THAM SỐ MỘT** và theo dõi vài ngày. Nếu thêm nhiều tham số cùng lúc, bạn sẽ không biết tham số nào thực sự giải quyết vấn đề và có thể làm máy tốn pin/nóng hơn không cần thiết.
> - Nếu một tham số không giúp hết đơ, hãy **xóa nó đi** trước khi thử tham số tiếp theo.
> - Để thử nhanh cho **1 lần boot duy nhất** mà không sửa file: tại menu GRUB, nhấn phím `e`, tìm dòng bắt đầu bằng `linux`, viết thêm tham số vào cuối dòng rồi nhấn `Ctrl + X` để boot.

### Các tham số hay dùng theo từng triệu chứng

1. **`nvme_core.default_ps_max_latency_us=0`**
   - *Khi nào dùng:* Máy dùng SSD NVMe thỉnh thoảng treo I/O, báo `nvme timeout` hoặc mất ổ sau khi idle.
   - *Tác dụng:* Tắt hoàn toàn tính năng tiết kiệm điện tự động APST của NVMe.
2. **`mem_sleep_default=deep`** *(hoặc `mem_sleep_default=s2idle`)*
   - *Khi nào dùng:* Máy bị đơ màn hình đen khi đánh thức từ chế độ Sleep/Suspend.
   - *Tác dụng:* Ép kernel dùng chế độ ngủ S3 truyền thống (`deep`) thay vì Modern Standby (`s2idle`), hoặc ngược lại.
3. **`pcie_aspm=off`**
   - *Khi nào dùng:* Lỗi `PCIe Bus Error: severity=Corrected`, GPU hoặc NVMe hay bị rớt khỏi bus PCIe.
   - *Tác dụng:* Tắt Active State Power Management trên bus PCIe.
4. **`i915.enable_psr=0`** *(Chỉ dành cho GPU tích hợp Intel)*
   - *Khi nào dùng:* Màn hình laptop dùng iGPU Intel tự nhiên đứng yên hoặc giật chớp khi hiển thị ảnh tĩnh, dù hệ thống bên dưới vẫn chạy.
   - *Tác dụng:* Tắt tính năng Panel Self Refresh (PSR) của driver `i915`.
5. **`intel_idle.max_cstate=1`** *(Intel)* hoặc **`processor.max_cstate=1`** / **`idle=nomwait`** *(AMD Ryzen đời đầu)*
   - *Khi nào dùng:* Máy đơ cứng hoàn toàn khi đang để không (idle) hoặc tải rất nhẹ, không để lại bất kỳ dòng log nào.
   - *Tác dụng:* Ngăn CPU đi vào trạng thái tiết kiệm điện sâu (deep C-states) bị lỗi trên một số dòng CPU/BIOS (lưu ý: sẽ làm máy hao điện hơn khi idle).
6. **`nomodeset`** *(Chỉ dùng tạm thời để cứu hộ)*
   - *Khi nào dùng:* Máy treo ngay khi vừa qua màn hình GRUB, trước khi vào được màn hình đăng nhập.
   - *Tác dụng:* Tắt Kernel Mode Setting để boot vào giao diện đồ họa cơ bản và cài lại driver GPU; **không** dùng lâu dài vì sẽ mất tăng tốc phần cứng đồ họa.

### Cách áp dụng vĩnh viễn vào GRUB

1. Mở file cấu hình GRUB:
   ```bash
   sudo nano /etc/default/grub
   ```
2. Tìm dòng `GRUB_CMDLINE_LINUX_DEFAULT` và thêm **một** tham số cần thử vào trong dấu ngoặc kép, ví dụ:
   ```ini
   GRUB_CMDLINE_LINUX_DEFAULT="quiet splash nvme_core.default_ps_max_latency_us=0"
   ```
3. Cập nhật GRUB và khởi động lại:
   ```bash
   sudo update-grub
   ```
4. Sau khi reboot, kiểm tra chắc chắn tham số đã có hiệu lực:
   ```bash
   cat /proc/cmdline
   ```
