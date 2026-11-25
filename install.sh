#!/usr/bin/env bash
# Cài pc-health-watch cho người dùng hiện tại (không cần root cho phần chính).
#   ./install.sh                  cài/cập nhật + bật daemon (systemd --user)
#   ./install.sh --smart-sudoers  cho phép đọc SMART không cần mật khẩu (cần sudo)
#   ./install.sh --setup-system   bật journal lưu bền + SysRq + cài earlyoom/smartmontools (cần sudo)
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

uninstall() {
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT" "$BIN/healthwatch"
    rm -rf "$DEST"
    systemctl --user daemon-reload
    [[ -f /etc/sudoers.d/pc-health-watch ]] && sudo rm -f /etc/sudoers.d/pc-health-watch
    say "Đã gỡ. Dữ liệu hộp đen vẫn ở ~/.local/state/pc-health-watch (xóa tay nếu muốn)."
}

case "${1:-}" in
    "") install_user ;;
    --smart-sudoers) smart_sudoers ;;
    --setup-system) setup_system ;;
    --uninstall) uninstall ;;
    -h | --help) sed -n '2,7p' "$0" ;;
    *)
        echo "Tham số không hợp lệ: $1" >&2
        exit 1
        ;;
esac
