"""Đọc thông tin sức khỏe ổ đĩa qua smartctl."""

import glob
import json
import os

from healthwatch import util


def list_disks(root="/"):
    result = []
    for path in sorted(glob.glob(util.proc_path(root, "sys/block/*"))):
        name = os.path.basename(path)
        if name.startswith(("loop", "ram", "zram", "dm-", "sr")):
            continue
        result.append("/dev/" + name)
    return result


def _attr(attrs, number):
    for item in attrs or []:
        if item.get("id") == number:
            value = item.get("raw", {}).get("value", item.get("value"))
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def parse_smart_json(data):
    nvme = data.get("nvme_smart_health_information_log", {})
    attrs = data.get("ata_smart_attributes", {}).get("table", [])
    passed = data.get("smart_status", {}).get("passed")
    temp = nvme.get("temperature")
    if temp is None:
        temp = data.get("temperature", {}).get("current")
    model = data.get("model_name") or data.get("product") or data.get("model_number") or ""
    sata_temp = _attr(attrs, 194)
    if temp is None:
        temp = sata_temp
    result = {"device": data.get("device", {}).get("name", ""), "model": str(model), "passed": passed,
              "temp_c": float(temp) if isinstance(temp, (int, float)) else None,
              "percent_used": nvme.get("percentage_used"), "media_errors": nvme.get("media_errors"),
              "critical_warning": nvme.get("critical_warning"), "reallocated": _attr(attrs, 5),
              "pending": _attr(attrs, 197), "uncorrectable": _attr(attrs, 198),
              "power_on_hours": nvme.get("power_on_hours") or _attr(attrs, 9),
              "unsafe_shutdowns": nvme.get("unsafe_shutdowns"), "problems": []}
    for key in ("percent_used", "media_errors", "critical_warning", "power_on_hours", "unsafe_shutdowns"):
        if result[key] is not None:
            try:
                result[key] = int(result[key])
            except (TypeError, ValueError):
                result[key] = None
    if passed is False:
        result["problems"].append("SMART báo lỗi")
    if (result["critical_warning"] or 0) != 0:
        result["problems"].append("NVMe có cảnh báo nghiêm trọng")
    if (result["media_errors"] or 0) > 0:
        result["problems"].append("NVMe có lỗi phương tiện")
    if (result["percent_used"] or 0) >= 90:
        result["problems"].append("Ổ đĩa đã sử dụng từ 90% độ bền")
    for key, label in (("reallocated", "sector đã được remap"), ("pending", "sector đang chờ xử lý"), ("uncorrectable", "sector không thể sửa")):
        if (result[key] or 0) > 0:
            result["problems"].append("Có " + label)
    return result


def read_smart(device, use_sudo=True, runner=util.run):
    command = ["smartctl", "-j", "-a", device]
    if use_sudo and hasattr(os, "geteuid") and os.geteuid() != 0:
        command = ["sudo", "-n"] + command
    code, output = runner(command)
    if not output:
        return None
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    # bit 0/1 của exit_status: lệnh sai / không mở được thiết bị (USB, thẻ nhớ...) → không có số liệu
    status = (data.get("smartctl") or {}).get("exit_status", 0)
    if status & 3 and "smart_status" not in data:
        return None
    return parse_smart_json(data)


def read_all(root="/", use_sudo=True, runner=util.run):
    return [item for item in (read_smart(device, use_sudo, runner) for device in list_disks(root)) if item is not None]
