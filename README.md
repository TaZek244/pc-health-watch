# 🩺 pc-health-watch

Continuous Linux health monitoring with early alerts and a persistent black-box recorder for diagnosing freezes after a hard reset.

## Features

- Monitors RAM, swap, memory and I/O PSI, disk latency and space, temperatures, read-only mounts, kernel errors, and SMART health.
- Warning/critical thresholds require sustained conditions, use hysteresis, repeat ongoing critical alerts, and report recovery.
- Records JSONL snapshots with `fsync` so recent data can survive a reset; retention and total-size limits are enforced automatically.
- Sends desktop notifications; critical alerts can also use a GTK popup and sound.
- Optional root freeze watchdog can record a freeze, attempt an OOM kill during memory thrashing, and power off or reboot after a prolonged freeze.

## Requirements

- Linux with `systemd --user` and Python 3.8 or newer.
- Normal installation: Python 3 only. `notify-send` (`libnotify-bin`) enables desktop notifications; `python3-gi` and `gir1.2-gtk-3.0` enable the critical GTK popup.
- Optional SMART checks: `smartmontools`; passwordless sudo for the exact `smartctl -j -a` commands is configured by `./install.sh --smart-sudoers`.
- Optional system setup: `earlyoom` (or `systemd-oomd`), persistent journald logs, kernel SysRq, and membership in `adm` improve diagnostics and recovery.

## Installation

```bash
git clone https://github.com/TaZek244/pc-health-watch.git ~/pc-health-watch
cd ~/pc-health-watch
./install.sh
healthwatch check
```

The installer copies the application to `~/.local/share/pc-health-watch`, creates `~/.local/bin/healthwatch`, installs and starts the `pc-health-watch` user service, and copies the documentation. Add `~/.local/bin` to `PATH` if needed.

Optional setup steps ask before making system changes:

```bash
./install.sh --setup-system
./install.sh --smart-sudoers
```

To install the root freeze watchdog, which also asks whether to use logging or automatic action and poweroff or reboot:

```bash
./install.sh --watchdog
```

The watchdog is a small memory-locked root service that keeps running when the desktop is frozen. It watches kernel PSI every second plus a GUI heartbeat written by the user daemon:

| Time since freeze started | Action |
|---|---|
| 30 s | Confirm the freeze, log to the kernel log, start 1 s in-memory sampling |
| 60 s, 120 s | If memory-bound: SysRq-f (kernel OOM killer), which often recovers the system |
| 280 s | Write a report to `/var/lib/pc-health-watch/freeze-*.json` |
| 300 s | `act` mode: SysRq sync, remount read-only, then poweroff or reboot. `log` mode: record only |

The freeze is cancelled after 15 s without symptoms. Start in `log` mode for a few days, check `journalctl -u pc-health-watch-watchdog` for false positives, then set `"mode": "act"` in `/etc/pc-health-watch/watchdog.json`. Details: [docs/FREEZE-WATCHDOG.md](docs/FREEZE-WATCHDOG.md).

## Usage

The user daemon normally runs through systemd. The CLI can read its current status or collect a direct snapshot if the daemon is unavailable.

| Command | Purpose |
|---|---|
| `healthwatch status` | Current health status; add `--json` for JSON. |
| `healthwatch history` | Recent black-box data; `-m N`/`--minutes N` selects minutes (default: 15), and `--json` outputs JSON. |
| `healthwatch report` | Analyze the previous boot; `--boot N` selects a journal boot index (default: `-1`), and `--json` outputs JSON. |
| `healthwatch storage` | Show black-box storage; add `--prune` to compress and enforce limits immediately. |
| `healthwatch check` | Check Python, PSI, logs, permissions, OOM handling, SMART, notifications, daemon, and watchdog status. |
| `healthwatch test-alert` | Send a warning test alert; add `--critical` for a critical alert. |
| `healthwatch config` | Show the active configuration; add `--default` for all defaults. |
| `healthwatch daemon` | Run the daemon directly. |

Useful logs:

```bash
journalctl --user -u pc-health-watch -f
sudo journalctl -u pc-health-watch-watchdog -f
```

After a hard reset, run `healthwatch report` first, then consult [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md). The report uses the previous boot's journal and available black-box/freeze data to summarize peaks, processes, kernel errors, and likely causes. Persistent journald logs are required to retain kernel messages across resets.

## Configuration

The user configuration is `~/.config/pc-health-watch/config.json` (or `$XDG_CONFIG_HOME/pc-health-watch/config.json`). Only changed keys are needed; the daemon reloads the file when it changes. Inspect the complete default configuration with:

```bash
healthwatch config --default
```

Default alert thresholds are:

| Rule | Warning | Critical | Hold time |
|---|---:|---:|---:|
| RAM used | 90% | 96% | 20 s |
| Swap used | 70% | 90% | 30 s |
| Memory PSI `full avg10` | 10% | 30% | 10 s |
| I/O PSI `full avg10` | 30% | 60% | 20 s |
| I/O latency | 300 ms | 1500 ms | 15 s |
| Hottest temperature | 88°C | 97°C | 15 s |
| Disk free space (on partitions at least 90% full) | 5 GB | 1 GB | immediate |

Kernel-log and SMART rules are enabled by default; SMART is checked every 60 minutes. Notifications are enabled, critical alerts repeat every 10 minutes, and sound is enabled by default. The daemon samples every 5 seconds. The black box stores a compact record every 30 seconds when the system is healthy and a full record on every sample under load (RAM ≥ 80%, swap ≥ 50%, rising PSI, high iowait, ≥ 85°C, or an active alert), continuing for 120 seconds after conditions clear. Storage defaults to 14 days and 200 MB.

Example override:

```json
{
  "rules": {
    "memory_used": {"warning": 85, "critical": 95, "for_seconds": 15}
  },
  "notify": {"sound": false, "repeat_minutes": 5},
  "storage": {"retention_days": 7, "max_total_mb": 100}
}
```

The optional `actions.relieve_memory` safeguard is disabled by default. If enabled, memory PSI `full avg10` at least 40% for 15 seconds causes SIGTERM to the largest matching process owned by the current user, excluding `protect` names.

## Uninstall

```bash
./install.sh --uninstall
```

This removes the user installation, user service, generated command, SMART sudoers file, and installed watchdog files. Black-box data in `~/.local/state/pc-health-watch` and watchdog configuration/reports in `/etc/pc-health-watch` and `/var/lib/pc-health-watch` are retained; remove them manually if no longer needed.

## Project layout

```text
healthwatch/   Collector, daemon, rules, recorder, CLI, reports, and watchdog code
systemd/       User daemon and root watchdog unit files
docs/          Architecture, troubleshooting, and freeze-watchdog documentation
tests/         Unit tests
install.sh     Installer, optional system setup, watchdog setup, and uninstall
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for internals and [docs/FREEZE-WATCHDOG.md](docs/FREEZE-WATCHDOG.md) for watchdog behavior and limitations.

## License

MIT. See [LICENSE](LICENSE).
