#!/usr/bin/env bash
# Cài pc-health-watch cho người dùng hiện tại (không cần root cho phần chính).
#   ./install.sh                  cài/cập nhật + bật daemon (systemd --user)
#   ./install.sh --smart-sudoers  cho phép đọc SMART không cần mật khẩu (cần sudo)
#   ./install.sh --setup-system   bật journal lưu bền + SysRq + cài earlyoom/smartmontools (cần sudo)
#   ./install.sh --watchdog       cài freeze watchdog (root): tự tắt máy an toàn khi đơ quá lâu (cần sudo)
#   ./install.sh --uninstall      gỡ (giữ lại dữ liệu hộp đen)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/pc-health-watch"
BIN="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT=pc-health-watch.service

say() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
ask() {
    local reply
    read -r -p "$1 [y/N] " reply
    [[ $reply =~ ^[YyCc]$ ]]
}

install_user() {
    command -v python3 >/dev/null || {
        echo "Cần python3" >&2
        exit 1
    }
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || {
        echo "Cần Python 3.8+" >&2
        exit 1
    }
    say "Chép mã vào $DEST"
    mkdir -p "$DEST" "$BIN" "$UNIT_DIR"
    rm -rf "$DEST/healthwatch"
    cp -r "$SRC/healthwatch" "$DEST/"
    cp "$SRC/README.md" "$DEST/" 2>/dev/null || true
    [[ -d $SRC/docs ]] && cp -r "$SRC/docs" "$DEST/"
    find "$DEST" -name __pycache__ -prune -exec rm -rf {} +

    say "Tạo lệnh $BIN/healthwatch"
    cat >"$BIN/healthwatch" <<EOF
#!/bin/sh
PYTHONPATH="$DEST\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m healthwatch "\$@"
EOF
    chmod +x "$BIN/healthwatch"

    say "Cài dịch vụ systemd --user"
    cp "$SRC/systemd/$UNIT" "$UNIT_DIR/$UNIT"
    systemctl --user daemon-reload
    systemctl --user enable "$UNIT" >/dev/null
    systemctl --user restart "$UNIT"
    sleep 1
    systemctl --user --no-pager --lines=3 status "$UNIT" || true

    case ":$PATH:" in
        *":$BIN:"*) ;;
        *) say "Thêm $BIN vào PATH (đăng nhập lại hoặc: export PATH=\"$BIN:\$PATH\")" ;;
    esac
    say "Xong. Thử: healthwatch status | healthwatch check | healthwatch test-alert --critical"
}

smart_sudoers() {
    local smartctl
    smartctl="$(command -v smartctl || true)"
    if [[ -z $smartctl ]]; then
        say "Cài smartmontools"
        sudo apt-get install -y smartmontools
        smartctl="$(command -v smartctl)"
    fi
    local rule="$USER ALL=(root) NOPASSWD: $smartctl -j -a /dev/sd[a-z], $smartctl -j -a /dev/nvme[0-9]n[0-9], $smartctl --version"
    local tmp
    tmp="$(mktemp)"
    echo "$rule" >"$tmp"
    if sudo visudo -cf "$tmp" >/dev/null; then
        sudo install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/pc-health-watch
        say "Đã cho phép: $rule"
    else
        echo "Quy tắc sudoers không hợp lệ, bỏ qua" >&2
    fi
    rm -f "$tmp"
}

setup_system() {
    say "Các thay đổi hệ thống sau giúp chẩn đoán và cứu máy khi đơ. Mỗi mục sẽ hỏi trước."
    if [[ ! -d /var/log/journal ]] && ask "Bật journal lưu bền (giữ log qua lần reset)?"; then
        sudo mkdir -p /var/log/journal
        sudo systemd-tmpfiles --create --prefix /var/log/journal
        sudo systemctl restart systemd-journald
    fi
    if [[ "$(cat /proc/sys/kernel/sysrq)" != 1 ]] && ask "Bật Magic SysRq (Alt+SysRq+REISUB khi máy đơ)?"; then
        echo 'kernel.sysrq=1' | sudo tee /etc/sysctl.d/99-sysrq.conf >/dev/null
        sudo sysctl -q --system
    fi
    if ! systemctl is-active --quiet earlyoom && ask "Cài earlyoom (tự đóng app ngốn RAM trước khi máy đơ)?"; then
        sudo apt-get install -y earlyoom
        sudo systemctl enable --now earlyoom
    fi
    if ! id -nG | grep -qwE 'adm|systemd-journal' && ask "Thêm $USER vào nhóm adm để đọc kernel log?"; then
        sudo usermod -aG adm "$USER"
        say "Cần đăng xuất/đăng nhập lại để có hiệu lực"
    fi
    if ! command -v smartctl >/dev/null && ask "Cài smartmontools và cho phép đọc SMART?"; then
        smart_sudoers
    fi
}

WD_DEST=/opt/pc-health-watch
WD_UNIT=pc-health-watch-watchdog.service
WD_CONF=/etc/pc-health-watch/watchdog.json

install_watchdog() {
    say "Cài freeze watchdog vào $WD_DEST (root-owned) — xem docs/FREEZE-WATCHDOG.md"
    sudo rm -rf "$WD_DEST/healthwatch"
    sudo install -d -m 0755 -o root -g root "$WD_DEST" /etc/pc-health-watch
    sudo cp -r "$SRC/healthwatch" "$WD_DEST/"
    [[ -d $SRC/docs ]] && sudo cp -r "$SRC/docs" "$WD_DEST/"
    sudo find "$WD_DEST" -name __pycache__ -prune -exec rm -rf {} +
    sudo chown -R root:root "$WD_DEST"
    sudo chmod -R go-w "$WD_DEST"
    sudo python3 -m compileall -q "$WD_DEST/healthwatch" >/dev/null || true

    if sudo test -f "$WD_CONF"; then
        say "Giữ cấu hình hiện có: $WD_CONF"
    else
        local mode=log action=poweroff
        echo "Chế độ: 'log' = chỉ ghi lại lúc lẽ ra đã tắt máy (nên chạy thử vài ngày trước),"
        echo "        'act' = tự tắt máy thật khi đơ quá 5 phút."
        if ask "Bật chế độ act (tự tắt máy thật) ngay bây giờ?"; then mode=act; fi
        if ask "Khởi động lại thay vì tắt hẳn (reboot thay cho poweroff)?"; then action=reboot; fi
        printf '{\n  "mode": "%s",\n  "action": "%s",\n  "action_after_seconds": 300,\n  "detect_seconds": 30,\n  "oom_kill_after_seconds": 60\n}\n' \
            "$mode" "$action" | sudo tee "$WD_CONF" >/dev/null
        sudo chmod 0644 "$WD_CONF"
    fi
    sudo cp "$SRC/systemd/$WD_UNIT" "/etc/systemd/system/$WD_UNIT"
    sudo systemctl daemon-reload
    sudo systemctl enable "$WD_UNIT" >/dev/null
    sudo systemctl restart "$WD_UNIT"
    sleep 1
    sudo systemctl --no-pager --lines=3 status "$WD_UNIT" || true
    say "Đổi chế độ: sửa $WD_CONF rồi: sudo systemctl restart $WD_UNIT"
    say "Tắt khẩn: sudo systemctl disable --now $WD_UNIT"
}

uninstall() {
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT" "$BIN/healthwatch"
    rm -rf "$DEST"
    systemctl --user daemon-reload
    [[ -f /etc/sudoers.d/pc-health-watch ]] && sudo rm -f /etc/sudoers.d/pc-health-watch
    if [[ -f /etc/systemd/system/$WD_UNIT ]]; then
        sudo systemctl disable --now "$WD_UNIT" || true
        sudo rm -f "/etc/systemd/system/$WD_UNIT"
        sudo rm -rf "$WD_DEST"
        sudo systemctl daemon-reload
        say "Đã gỡ watchdog (giữ $WD_CONF và báo cáo trong /var/lib/pc-health-watch)"
    fi
    say "Đã gỡ. Dữ liệu hộp đen vẫn ở ~/.local/state/pc-health-watch (xóa tay nếu muốn)."
}

case "${1:-}" in
    "") install_user ;;
    --smart-sudoers) smart_sudoers ;;
    --setup-system) setup_system ;;
    --watchdog) install_watchdog ;;
    --uninstall) uninstall ;;
    -h | --help) sed -n '2,8p' "$0" ;;
    *)
        echo "Tham số không hợp lệ: $1" >&2
        exit 1
        ;;
esac
