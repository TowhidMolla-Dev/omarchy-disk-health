#!/usr/bin/env python3
"""Emit disk SMART health JSON for the Omarchy NVMe Health bar widget.

Reads NVMe/ATA SMART through UDisks2 over the system bus — no root, no
smartctl, no sudoers. Requires udisks2 (ships with Omarchy).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Callable, TypeVar

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402


UDISKS = "org.freedesktop.UDisks2"
IFACE_DRIVE = "org.freedesktop.UDisks2.Drive"
IFACE_NVME = "org.freedesktop.UDisks2.NVMe.Controller"
IFACE_ATA = "org.freedesktop.UDisks2.Drive.Ata"
IFACE_BLOCK = "org.freedesktop.UDisks2.Block"
IFACE_PARTITION = "org.freedesktop.UDisks2.Partition"

# Hard ceilings: UDisks GetManagedObjects can be large; keep parsing and
# the document we emit bounded so the shell/QML cannot be flooded.
MAX_MANAGED_OBJECTS = 512
MAX_DISKS = 32
MAX_BLOCK_CANDIDATES = 8
MAX_ATTR_ROWS = 256
MAX_FIELD_LEN = 128
MAX_PATH_LEN = 256
MAX_DEVICE_ARG_LEN = 64
MAX_MESSAGE_LEN = 256
MAX_JSON_BYTES = 32 * 1024
DBUS_TIMEOUT_MS = 10_000
SETUP_TIMEOUT_SEC = 10
# Wall clock for the whole status.py run (QML watchdog should match).
PROCESS_DEADLINE_SEC = 45

NVME_ATTR_KEYS = (
  "percent_used",
  "avail_spare",
  "media_errors",
  "total_data_written",
  "power_cycles",
  "unsafe_shutdowns",
  "wctemp",
  "cctemp",
  "temp_sensors",
)

# Attribute 9 raw on some HDDs is a packed/minutes counter that decodes to an
# absurd number of hours. Anything above this is a vendor encoding, not hours.
MAX_PLAUSIBLE_HOURS = 500_000

# --- Verdict / alert / history tuning ---------------------------------------
# Alerts fire on threshold *crossing* only, so a sick disk does not notify on
# every poll. Each alert is remembered for NOTIFY_REPEAT_SEC.
NOTIFY_REPEAT_SEC = 6 * 3600
MIN_NOTIFY_GAP_SEC = 900
MAX_REASON_LEN = 120

# History is a bounded ring of [epoch, value] pairs per series per device.
HISTORY_MAX_POINTS = 720  # ~2 days at the 300 s default cadence
HISTORY_MAX_SERIES = 4
HISTORY_MAX_DEVICES = 16
# Sample no more often than this; a fast refresh must not fill the ring in
# minutes and starve the longer baseline.
HISTORY_MIN_INTERVAL_SEC = 240
# Points handed to the UI. A sparkline does not get more legible past ~60,
# and the payload must stay well inside MAX_JSON_BYTES.
TREND_POINTS_MAX = 60
# Drives that carry trend data, worst-first. Bounds the payload on machines
# with many disks; the rest keep their counters but no sparkline.
TREND_MAX_DISKS = 6
# Require a real baseline before quoting a wear projection, otherwise the
# first two samples (minutes apart) imply absurd rates.
PROJECTION_MIN_SPAN_SEC = 7 * 24 * 3600
# Cap derived monthly rates. A real SSD burns 1-3%/month even under heavy
# abuse, so 400 is generous; anything faster is a mis-decoded counter or a
# counter that reset mid-window, and would print a nonsense life estimate.
MAX_PROJECTED_PER_MONTH = 400.0

# Default alert thresholds. Overridable from the panel settings, which pass
# them in as --key=value so there is a single source of truth.
DEFAULT_THRESHOLDS = {
  "tempWarnC": 55,
  "tempCritC": 70,
  "healthWarnPct": 20,
  "healthCritPct": 10,
  "reallocWarn": 0,
  "badBlocksWarn": 0,
  "spareWarnPct": 10,
}

T = TypeVar("T")

_bus: Gio.DBusConnection | None = None
_executor = ThreadPoolExecutor(max_workers=1)


def clamp_str(value: Any, max_len: int = MAX_FIELD_LEN) -> str:
  if value is None:
    return ""
  text = str(value).replace("\x00", "")
  # Drop other C0 controls except tab/newline so UI text stays printable.
  text = "".join(ch for ch in text if ch >= " " or ch in "\t\n")
  text = text.strip()
  if len(text) > max_len:
    return text[:max_len]
  return text


def safe_dbus_path(value: Any) -> str | None:
  """Sanitize an object path; reject if missing or over MAX_PATH_LEN."""
  text = clamp_str(value, MAX_PATH_LEN + 1)
  if not text or len(text) > MAX_PATH_LEN:
    return None
  if not text.startswith("/"):
    return None
  return text


def bytes_to_path(value: Any) -> str:
  raw = ""
  if isinstance(value, (bytes, bytearray)):
    raw = bytes(value).split(b"\x00", 1)[0].decode("utf-8", "replace")
  elif isinstance(value, str):
    raw = value.split("\x00", 1)[0]
  elif isinstance(value, (list, tuple)):
    try:
      raw = bytes(int(x) & 0xFF for x in value).split(b"\x00", 1)[0].decode("utf-8", "replace")
    except (TypeError, ValueError):
      return ""
  else:
    return ""
  return clamp_str(raw, MAX_FIELD_LEN)


def as_int(value: Any) -> int | None:
  try:
    if value is None:
      return None
    return int(value)
  except (TypeError, ValueError):
    return None


def bytes_to_tib(num_bytes: int | None) -> float | None:
  if num_bytes is None or num_bytes < 0:
    return None
  return round(num_bytes / (1024**4), 2)


def state_dir() -> str:
  """Per-user state directory; falls back to a temp dir when HOME is unset."""
  base = os.environ.get("XDG_STATE_HOME") or os.path.join(
    os.path.expanduser("~"), ".local", "state"
  )
  if not base or base.startswith("~"):
    base = os.path.join(tempfile.gettempdir(), "disk-health-" + str(os.getuid()))
  return os.path.join(base, "disk-health")


def parse_thresholds(argv: list[str]) -> dict[str, Any]:
  """Read --key=value overrides, clamped so a typo cannot break the shell."""
  out = dict(DEFAULT_THRESHOLDS)
  for arg in argv:
    if not arg.startswith("--") or "=" not in arg:
      continue
    key, _, raw = arg[2:].partition("=")
    if key not in out:
      continue
    try:
      value = float(raw)
    except ValueError:
      continue
    if key in ("tempWarnC", "tempCritC"):
      value = max(0.0, min(120.0, value))
    elif key in ("healthWarnPct", "healthCritPct", "spareWarnPct"):
      value = max(0.0, min(100.0, value))
    else:
      value = max(0.0, min(1_000_000.0, value))
    out[key] = int(value)
  # A warn floor above its critical ceiling would make the drive look fine.
  if out["tempCritC"] < out["tempWarnC"]:
    out["tempCritC"] = out["tempWarnC"]
  if out["healthCritPct"] > out["healthWarnPct"]:
    out["healthCritPct"] = out["healthWarnPct"]
  return out


def run_timed(timeout_sec: float, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
  """Run a blocking Gio setup call with a wall-clock timeout."""
  fut = _executor.submit(fn, *args, **kwargs)
  try:
    return fut.result(timeout=timeout_sec)
  except FuturesTimeout as exc:
    raise TimeoutError(f"timed out after {timeout_sec:.0f}s") from exc


def emit(payload: dict[str, Any]) -> None:
  """Print one JSON line, refusing to exceed MAX_JSON_BYTES (incl. newline)."""
  encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
  if len(encoded) + 1 <= MAX_JSON_BYTES:
    sys.stdout.buffer.write(encoded + b"\n")
    return
  fallback = {
    "ok": False,
    "error": "output_too_large",
    "message": "SMART status payload exceeded size limit.",
    "needsSetup": False,
    "devices": [],
    "disk": None,
  }
  sys.stdout.buffer.write(
    json.dumps(fallback, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
  )


def get_bus() -> Gio.DBusConnection:
  global _bus
  if _bus is None:
    _bus = run_timed(SETUP_TIMEOUT_SEC, Gio.bus_get_sync, Gio.BusType.SYSTEM, None)
  return _bus


def slim_managed_objects(objs: dict[str, Any]) -> dict[str, dict[str, Any]]:
  """Keep only drive/block interfaces and the few properties we read."""
  if len(objs) > MAX_MANAGED_OBJECTS:
    raise RuntimeError(f"UDisks object count {len(objs)} exceeds limit {MAX_MANAGED_OBJECTS}")

  slim: dict[str, dict[str, Any]] = {}
  for path, ifaces in objs.items():
    if len(slim) >= MAX_MANAGED_OBJECTS:
      break
    safe_path = safe_dbus_path(path)
    if not safe_path:
      continue
    if not isinstance(ifaces, dict):
      continue
    has_drive = IFACE_DRIVE in ifaces
    has_block = IFACE_BLOCK in ifaces
    if not has_drive and not has_block:
      continue

    entry: dict[str, Any] = {}
    if has_drive:
      drive = ifaces.get(IFACE_DRIVE) or {}
      entry[IFACE_DRIVE] = {
        "Model": drive.get("Model"),
        "Optical": drive.get("Optical"),
        "MediaRemovable": drive.get("MediaRemovable"),
        "MediaAvailable": drive.get("MediaAvailable"),
      }
      if IFACE_NVME in ifaces:
        nvme = ifaces.get(IFACE_NVME) or {}
        crit = nvme.get("SmartCriticalWarning") or []
        if isinstance(crit, (list, tuple)):
          crit = list(crit)[:32]
        else:
          crit = []
        entry[IFACE_NVME] = {
          "SmartPowerOnHours": nvme.get("SmartPowerOnHours"),
          "SmartCriticalWarning": crit,
          "SmartTemperature": nvme.get("SmartTemperature"),
        }
      if IFACE_ATA in ifaces:
        ata = ifaces.get(IFACE_ATA) or {}
        # SmartTemperature arrives as a double (313.0), so keep it float-safe.
        temp = ata.get("SmartTemperature")
        entry[IFACE_ATA] = {
          "SmartFailing": ata.get("SmartFailing"),
          "SmartPowerOnSeconds": ata.get("SmartPowerOnSeconds"),
          "SmartTemperature": float(temp) if isinstance(temp, (int, float)) else None,
          "SmartNumBadSectors": ata.get("SmartNumBadSectors"),
          "SmartNumAttributesFailing": ata.get("SmartNumAttributesFailing"),
        }
    if has_block:
      block = ifaces.get(IFACE_BLOCK) or {}
      drive_ref = safe_dbus_path(block.get("Drive")) or ""
      entry[IFACE_BLOCK] = {
        "Drive": drive_ref,
        "Device": block.get("Device"),
        "PreferredDevice": block.get("PreferredDevice"),
      }
      if IFACE_PARTITION in ifaces:
        entry[IFACE_PARTITION] = True

    if entry:
      slim[safe_path] = entry
  return slim


def get_managed_objects() -> dict[str, dict[str, Any]]:
  bus = get_bus()
  om = run_timed(
    SETUP_TIMEOUT_SEC,
    Gio.DBusProxy.new_sync,
    bus,
    Gio.DBusProxyFlags.NONE,
    None,
    UDISKS,
    "/org/freedesktop/UDisks2",
    "org.freedesktop.DBus.ObjectManager",
    None,
  )
  result = om.call_sync("GetManagedObjects", None, Gio.DBusCallFlags.NONE, DBUS_TIMEOUT_MS, None)
  objs = result.unpack()
  if isinstance(objs, tuple):
    objs = objs[0]
  if not isinstance(objs, dict):
    return {}
  slim = slim_managed_objects(objs)
  objs.clear()
  return slim


def call_method(path: str, iface: str, method: str) -> Any:
  bus = get_bus()
  proxy = run_timed(
    SETUP_TIMEOUT_SEC,
    Gio.DBusProxy.new_sync,
    bus,
    Gio.DBusProxyFlags.NONE,
    None,
    UDISKS,
    path,
    iface,
    None,
  )
  result = proxy.call_sync(
    method,
    GLib.Variant("(a{sv})", ([],)),
    Gio.DBusCallFlags.NONE,
    DBUS_TIMEOUT_MS,
    None,
  )
  return result.unpack()


def block_paths_for_drive(objects: dict[str, Any], drive_path: str) -> list[str]:
  names: list[str] = []
  for _path, ifaces in objects.items():
    if len(names) >= MAX_BLOCK_CANDIDATES:
      break
    block = ifaces.get(IFACE_BLOCK)
    if not block:
      continue
    if str(block.get("Drive") or "") != drive_path:
      continue
    # Skip partitions: they also point at the same Drive in some setups via
    # the parent disk; prefer whole-disk nodes (no Partition iface).
    if IFACE_PARTITION in ifaces:
      continue
    device = bytes_to_path(block.get("Device") or block.get("PreferredDevice"))
    if device:
      names.append(device)
  return names


def device_matches(requested: str, candidates: list[str], drive_id: str) -> str | None:
  """Return 'exact', 'prefix', or None."""
  if not requested:
    return None
  req = clamp_str(requested, MAX_DEVICE_ARG_LEN)
  if len(req) < 5 or req in ("/dev", "/dev/"):
    return None
  if not (req.startswith("/dev/") or req.startswith("/org/freedesktop/UDisks2/")):
    return None
  if req == drive_id or req in candidates:
    return "exact"
  for name in candidates:
    if name == req:
      return "exact"
    # /dev/nvme0 matches /dev/nvme0n1; /dev/sda matches /dev/sda
    if name.startswith(req) or req.startswith(name):
      return "prefix"
  return None


def slim_nvme_attrs(attrs: Any) -> dict[str, Any]:
  if isinstance(attrs, tuple) and len(attrs) == 1:
    attrs = attrs[0]
  if not isinstance(attrs, dict):
    return {}
  out: dict[str, Any] = {}
  for key in NVME_ATTR_KEYS:
    if key in attrs:
      out[key] = attrs.get(key)
  return out


def kelvin_to_celsius(value: Any) -> int | None:
  """Convert a UDisks2 SmartTemperature reading to Celsius, or None.

  UDisks2 reports SmartTemperature in whole degrees Kelvin for both NVMe and
  ATA, not tenths of Celsius. Verified on this machine: the NVMe controller
  reported 320 K while smartctl reported 48 C, and the ATA drive reported
  313.15 K while smartctl reported 40 C. The wctemp/cctemp NVMe attributes
  (356/358) are vendor-specific and are deliberately ignored.
  """
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return None
  celsius = round(float(value) - 273.15)
  return celsius if 0 <= celsius <= 150 else None


def nvme_temp_c(nvme_props: dict[str, Any]) -> int | None:
  """NVMe temperature in Celsius, or None when the drive does not report one."""
  return kelvin_to_celsius(nvme_props.get("SmartTemperature"))


# UDisks2 returns ATA attributes as a(ysqiiqia{sv}) structs:
#   (id, name, flags, value, worst, threshold, raw, pretty_unit, extra)
ATA_ROW_NAME = 1
ATA_ROW_VALUE = 3
ATA_ROW_THRESHOLD = 5
ATA_ROW_RAW = 6
ATA_ROW_MIN_LEN = 7

# UDisks2 names attributes in kebab-case; older code used snake_case.
ATA_REALLOCATED = ("reallocated-sector-count",)
ATA_PENDING = ("current-pending-sector",)
ATA_OFFLINE_UNCORRECT = ("offline-uncorrectable",)
ATA_REPORTED_UNCORRECT = ("reported-uncorrect",)
ATA_UDMA_CRC = ("udma-crc-error-count",)
ATA_BAD_BLOCKS = ("runtime-bad-block-total", "bad-block-total")
ATA_TEMPERATURE = ("airflow-temperature-celsius", "temperature-celsius", "current-temperature-celsius")
ATA_POWER_CYCLES = ("power-cycle-count",)
ATA_LOAD_CYCLES = ("load-cycle-count",)
ATA_LBAS_WRITTEN = ("total-lbas-written",)
ATA_LBAS_READ = ("total-lbas-read",)
ATA_LIFE = (
  "percent-lifetime-remain",
  "percent-lifetime-remain-indicator",
  "media-wearout-indicator",
  "available-spare",
  "percent-used",
)

# UDisks2 hands back Seagate's 48-bit total-LBA counters (SMART 0xF1/0xF2)
# pre-multiplied by 2**25 / 10**6. Confirmed empirically against smartctl:
# a 512 MiB write advanced smartctl's Total_LBAs_Written by exactly 1,048,576
# while UDisks2's advanced by 35,187,593 (ratio 33.5575), and both the
# written and read absolute values hold the same 33.554432 ratio to 12
# significant figures. Undo the scale so the numbers agree with smartctl.
ATA_LBA_SCALE_UP = 33554432.0 / 1000000.0


def norm_attr_name(name: Any) -> str:
  text = name.decode("utf-8", "replace") if isinstance(name, (bytes, bytearray)) else str(name or "")
  return text.strip().lower().replace("_", "-")


def ata_attr_rows(attrs: Any) -> list[dict[str, Any]]:
  """Normalize UDisks2 ATA attributes into uniform dicts.

  Accepts the struct form UDisks2 actually returns as well as the
  id-keyed dict form some builds hand back.
  """
  if isinstance(attrs, tuple) and len(attrs) == 1:
    attrs = attrs[0]
  if isinstance(attrs, (list, tuple)):
    rows: list[Any] = list(attrs)[:MAX_ATTR_ROWS]
  elif isinstance(attrs, dict):
    rows = list(attrs.values())[:MAX_ATTR_ROWS]
  else:
    return []

  out: list[dict[str, Any]] = []
  for row in rows:
    if isinstance(row, (list, tuple)) and len(row) >= ATA_ROW_MIN_LEN:
      out.append(
        {
          "name": norm_attr_name(row[ATA_ROW_NAME]),
          "value": as_int(row[ATA_ROW_VALUE]) if len(row) > ATA_ROW_VALUE else None,
          "threshold": as_int(row[ATA_ROW_THRESHOLD]) if len(row) > ATA_ROW_THRESHOLD else None,
          "raw": as_int(row[ATA_ROW_RAW]),
        }
      )
    elif isinstance(row, dict):
      out.append(
        {
          "name": norm_attr_name(row.get("name") or row.get("Name")),
          "value": as_int(row.get("value") if "value" in row else row.get("Value")),
          "threshold": as_int(row.get("threshold") if "threshold" in row else row.get("Threshold")),
          "raw": as_int(row.get("raw") if "raw" in row else row.get("Raw")),
        }
      )
  return out


def ata_attr(rows: list[dict[str, Any]], *names: str) -> dict[str, Any] | None:
  wanted = {norm_attr_name(n) for n in names}
  for row in rows:
    if row.get("name") in wanted:
      return row
  return None


def ata_attr_raw(rows: list[dict[str, Any]], *names: str) -> int | None:
  row = ata_attr(rows, *names)
  return row.get("raw") if row else None


def ata_attr_percent(rows: list[dict[str, Any]], *names: str) -> int | None:
  """Normalized percent (0-100) for an attribute, preferring value over raw."""
  row = ata_attr(rows, *names)
  if not row:
    return None
  for key in ("value", "raw"):
    v = row.get(key)
    if v is not None and 0 <= v <= 100:
      return v
  return None


def ata_attr_temp(rows: list[dict[str, Any]], *names: str) -> int | None:
  """HDD temperature in Celsius, or None.

  Deliberately conservative. UDisks2 hands back the vendor RAW_VALUE, and on
  Seagate drives that field is a packed min/max/current encoding rather than
  the reading (raw 313150 while smartctl reports 40 C). Guessing a byte out of
  it produces confidently wrong temperatures, so only accept raw values that
  are already a plain in-range Celsius number and report None otherwise.
  """
  row = ata_attr(rows, *names)
  if not row:
    return None
  raw = row.get("raw")
  if raw is None:
    return None
  return raw if 0 <= raw <= 100 else None


def ata_lba_bytes(rows: list[dict[str, Any]], *names: str) -> int | None:
  """Bytes described by a total-LBA counter, or None when unsupported.

  These attributes are vendor-specific (Seagate only) and UDisks2 reports them
  inflated by ATA_LBA_SCALE_UP, so undo that before converting LBAs to bytes.
  """
  raw = ata_attr_raw(rows, *names)
  if raw is None or raw <= 0:
    return None
  lbas = raw / ATA_LBA_SCALE_UP
  if lbas < 1:
    return None
  return int(round(lbas)) * 512


def ata_health_percent(rows: list[dict[str, Any]]) -> int | None:
  """Worst normalized value across real wear/failure attributes.

  Only attributes with a non-zero threshold are meaningful; threshold 0
  means "not applicable" in SMART and would otherwise skew the result.
  """
  groups = (ATA_REALLOCATED, ATA_PENDING, ATA_OFFLINE_UNCORRECT, ATA_REPORTED_UNCORRECT, ATA_BAD_BLOCKS, ATA_LIFE)
  worst: int | None = None
  for names in groups:
    row = ata_attr(rows, *names)
    if not row:
      continue
    threshold = row.get("threshold")
    value = row.get("value")
    if threshold is None or threshold <= 0 or value is None:
      continue
    if worst is None or value < worst:
      worst = value
  return worst


def summarize_nvme(drive: dict[str, Any], nvme_props: dict[str, Any], attrs: dict[str, Any], device: str) -> dict[str, Any]:
  percent_used = as_int(attrs.get("percent_used"))
  life_remaining = None if percent_used is None else max(0, 100 - percent_used)
  spare = as_int(attrs.get("avail_spare"))
  media_errors = as_int(attrs.get("media_errors"))
  written = as_int(attrs.get("total_data_written"))
  hours = as_int(nvme_props.get("SmartPowerOnHours"))
  critical = nvme_props.get("SmartCriticalWarning") or []
  warning = False
  if isinstance(critical, (list, tuple)) and len(critical) > 0:
    warning = True
  if media_errors is not None and media_errors > 0:
    warning = True
  if spare is not None and spare < 10:
    warning = True
  if life_remaining is not None and life_remaining <= 10:
    warning = True

  return {
    "device": clamp_str(device),
    "type": "nvme",
    "model": clamp_str(drive.get("Model")),
    "protocol": "nvme",
    "passed": None if warning else True,
    "warning": warning,
    "powerOnHours": hours,
    "reallocatedSectors": None,
    "mediaErrors": media_errors,
    "availableSparePercent": spare,
    "percentageUsed": percent_used,
    "lifeRemainingPercent": life_remaining,
    "pendingSectors": None,
    "offlineUncorrectable": None,
    "reportedUncorrect": None,
    "badBlocks": None,
    "udmaCrcErrors": None,
    "temperatureC": nvme_temp_c(nvme_props),
    "powerCycles": as_int(attrs.get("power_cycles")),
    "loadCycles": None,
    "healthPercent": life_remaining,
    "tbwTiB": bytes_to_tib(written),
    "criticalWarning": len(critical) if isinstance(critical, (list, tuple)) else 0,
  }


def summarize_ata(drive: dict[str, Any], ata_props: dict[str, Any], attrs: Any, device: str) -> dict[str, Any]:
  rows = ata_attr_rows(attrs)

  hours = ata_attr_raw(rows, "power-on-hours")
  if hours is None or hours > MAX_PLAUSIBLE_HOURS:
    # Attribute 9 raw is vendor-encoded (minutes on some drives, a packed
    # counter on others). The UDisks2 seconds property is authoritative.
    seconds = as_int(ata_props.get("SmartPowerOnSeconds"))
    hours = seconds // 3600 if seconds is not None else None

  reallocated = ata_attr_raw(rows, *ATA_REALLOCATED)
  pending = ata_attr_raw(rows, *ATA_PENDING)
  offline_uncorrect = ata_attr_raw(rows, *ATA_OFFLINE_UNCORRECT)
  reported_uncorrect = ata_attr_raw(rows, *ATA_REPORTED_UNCORRECT)
  bad_blocks = ata_attr_raw(rows, *ATA_BAD_BLOCKS)
  udma_crc = ata_attr_raw(rows, *ATA_UDMA_CRC)

  # Prefer UDisks2's SmartTemperature (whole degrees Kelvin) over the vendor
  # RAW_VALUE attribute, which is a packed min/max/current encoding on Seagate
  # drives (raw 313150 for a 40 C disk). Fall back to the attribute only when
  # the property is missing or unreadable.
  temperature = kelvin_to_celsius(ata_props.get("SmartTemperature"))
  if temperature is None:
    temperature = ata_attr_temp(rows, *ATA_TEMPERATURE)

  data_written = ata_lba_bytes(rows, *ATA_LBAS_WRITTEN)

  life = ata_attr_percent(rows, *ATA_LIFE)
  if life is not None and life <= 100 and ATA_LIFE[0] == "percent-used":
    life = max(0, 100 - life)
  health = ata_health_percent(rows)

  failing = bool(ata_props.get("SmartFailing"))
  counts = (reallocated, pending, offline_uncorrect, reported_uncorrect, bad_blocks)
  warning = failing or any(c is not None and c > 0 for c in counts)
  if life is not None and life <= 10:
    warning = True

  return {
    "device": clamp_str(device),
    "type": "ata",
    "model": clamp_str(drive.get("Model")),
    "protocol": "ata",
    "passed": (not failing) if ata_props.get("SmartFailing") is not None else None,
    "warning": warning,
    "powerOnHours": hours,
    "reallocatedSectors": reallocated,
    "pendingSectors": pending,
    "offlineUncorrectable": offline_uncorrect,
    "reportedUncorrect": reported_uncorrect,
    "badBlocks": bad_blocks,
    "udmaCrcErrors": udma_crc,
    "temperatureC": temperature,
    "powerCycles": ata_attr_raw(rows, *ATA_POWER_CYCLES),
    "loadCycles": ata_attr_raw(rows, *ATA_LOAD_CYCLES),
    "mediaErrors": None,
    "availableSparePercent": None,
    "percentageUsed": None if life is None else max(0, 100 - life),
    "lifeRemainingPercent": life,
    "healthPercent": health,
    "tbwTiB": bytes_to_tib(data_written),
    "criticalWarning": 0,
  }


def enumerate_drives(objects: dict[str, Any], deadline: float) -> list[dict[str, Any]]:
  """List NVMe/ATA drives without issuing SMART method calls."""
  disks: list[dict[str, Any]] = []
  for path, ifaces in objects.items():
    if time.monotonic() > deadline:
      break
    if len(disks) >= MAX_DISKS:
      break
    drive = ifaces.get(IFACE_DRIVE)
    if not drive:
      continue
    if drive.get("Optical") or drive.get("MediaRemovable"):
      continue
    if drive.get("MediaAvailable") is False:
      continue

    is_nvme = IFACE_NVME in ifaces
    is_ata = IFACE_ATA in ifaces
    if not is_nvme and not is_ata:
      continue

    blocks = block_paths_for_drive(objects, path)
    device = blocks[0] if blocks else path
    disks.append(
      {
        "path": path,
        "device": clamp_str(device),
        "candidates": blocks,
        "protocol": "nvme" if is_nvme else "ata",
        "model": clamp_str(drive.get("Model")),
        "drive": drive,
        "ctrl_props": ifaces.get(IFACE_NVME if is_nvme else IFACE_ATA) or {},
      }
    )
  return disks


def pick_disk(disks: list[dict[str, Any]], requested: str) -> dict[str, Any] | None:
  """Resolve an explicit device/path, or None to mean 'every drive'."""
  if not disks:
    return None
  if not requested:
    return None
  exact: list[dict[str, Any]] = []
  prefix: list[dict[str, Any]] = []
  for d in disks:
    kind = device_matches(requested, d.get("candidates") or [d.get("device") or ""], d.get("path") or "")
    if kind == "exact":
      exact.append(d)
    elif kind == "prefix":
      prefix.append(d)
  if exact:
    return exact[0]
  # Prefix only when unambiguous (avoids /dev/nvme0 matching several namespaces).
  if len(prefix) == 1:
    return prefix[0]
  return None


def fetch_smart(disk: dict[str, Any]) -> dict[str, Any]:
  path = disk["path"]
  is_nvme = disk.get("protocol") == "nvme"
  iface = IFACE_NVME if is_nvme else IFACE_ATA
  device = disk.get("device") or path
  try:
    call_method(path, iface, "SmartUpdate")
  except Exception:
    pass
  try:
    attrs_pack = call_method(path, iface, "SmartGetAttributes")
  except Exception as exc:
    return {
      "ok": False,
      "device": clamp_str(device),
      "error": clamp_str(exc, MAX_MESSAGE_LEN),
      "protocol": disk.get("protocol"),
      "model": disk.get("model") or "",
    }

  attrs = attrs_pack[0] if isinstance(attrs_pack, tuple) else attrs_pack
  drive = disk.get("drive") or {}
  ctrl = disk.get("ctrl_props") or {}
  if is_nvme:
    return summarize_nvme(drive, ctrl, slim_nvme_attrs(attrs), device)
  return summarize_ata(drive, ctrl, attrs, device)


def _reason(text: str) -> str:
  return clamp_str(text, MAX_REASON_LEN)


def judge(disk: dict[str, Any], th: dict[str, Any]) -> dict[str, Any]:
  """Plain-English verdict plus the specific reasons behind it.

  Severity is ordered worst-first so the panel can pick one headline. The
  reasons exist so a bare percentage is never shown on its own.
  """
  critical: list[str] = []
  warn: list[str] = []

  if disk.get("passed") is False:
    critical.append(_reason("SMART reports failure"))
  if disk.get("warning") and not critical:
    warn.append(_reason("drive reports a problem"))

  health = disk.get("lifeRemainingPercent")
  if health is None:
    health = disk.get("healthPercent")
  if isinstance(health, int):
    if health <= th["healthCritPct"]:
      critical.append(_reason(f"health at {health}%"))
    elif health <= th["healthWarnPct"]:
      warn.append(_reason(f"health at {health}%"))

  temp = disk.get("temperatureC")
  if isinstance(temp, int):
    if temp >= th["tempCritC"]:
      critical.append(_reason(f"{temp} °C is over {th['tempCritC']} °C"))
    elif temp >= th["tempWarnC"]:
      warn.append(_reason(f"{temp} °C is over {th['tempWarnC']} °C"))

  # Error counters: any growth at all is worth flagging on an HDD.
  floor = th["reallocWarn"]
  for key, label, limit in (
    ("reallocatedSectors", "reallocated sector", floor),
    ("pendingSectors", "pending sector", floor),
    ("offlineUncorrectable", "uncorrectable sector", floor),
    ("reportedUncorrect", "reported uncorrect", floor),
    ("badBlocks", "bad block", th["badBlocksWarn"]),
    ("mediaErrors", "media error", 0),
  ):
    count = disk.get(key)
    if isinstance(count, int) and count > limit:
      warn.append(_reason(f"{count:,} {label}{'s' if count != 1 else ''}"))

  spare = disk.get("availableSparePercent")
  if isinstance(spare, int) and spare < th["spareWarnPct"]:
    warn.append(_reason(f"spare at {spare}%"))

  used = disk.get("percentageUsed")
  if isinstance(used, int) and used >= 100:
    critical.append(_reason(f"{used}% of write endurance used"))

  if critical:
    severity, reasons = "critical", critical + warn
  elif warn:
    severity, reasons = "warn", warn
  else:
    severity, reasons = "ok", []

  if severity == "ok":
    # Distinguish "checked and fine" from "nothing was readable", so an
    # unsupported metric never reads as a clean bill of health.
    readable = isinstance(health, int) or isinstance(temp, int) or disk.get("passed") is not None
    if not readable:
      headline = "Not enough data"
    elif isinstance(health, int) and health >= 90:
      headline = "Healthy"
    else:
      headline = "No problems detected"
  else:
    headline = reasons[0]

  return {
    "severity": severity,
    "headline": clamp_str(headline, MAX_REASON_LEN),
    "reasons": reasons[:6],
  }


# --- History ----------------------------------------------------------------

def load_history() -> dict[str, Any]:
  """Load the history store, ignoring anything malformed rather than failing."""
  path = os.path.join(state_dir(), "history.json")
  try:
    with open(path, "r", encoding="utf-8") as handle:
      data = json.load(handle)
  except (OSError, ValueError):
    return {}
  return data if isinstance(data, dict) else {}


def write_history(data: dict[str, Any]) -> None:
  """Atomically persist history so a killed run cannot truncate it."""
  directory = state_dir()
  try:
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, ".history.json.%d.tmp" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
      json.dump(data, handle, separators=(",", ":"))
    os.replace(tmp, os.path.join(directory, "history.json"))
  except OSError:
    pass  # A read-only or full home dir must not break the widget.


def record_history(
  store: dict[str, Any], disks: list[dict[str, Any]], raised: list[dict[str, Any]]
) -> None:
  """Append this poll to the per-device series and prune to the ring size."""
  now = int(time.time())
  series = store.setdefault("disks", {})
  if not isinstance(series, dict):
    store["disks"] = series = {}
  if not isinstance(store.get("notified"), dict):
    store["notified"] = {}

  fresh: list[str] = []
  for disk in disks:
    device = disk.get("device") or ""
    if not device:
      continue
    entry = series.get(device)
    if not isinstance(entry, dict):
      entry = series[device] = {"series": {}}
    bucket = entry.get("series")
    if not isinstance(bucket, dict):
      bucket = entry["series"] = {}

    stamp = entry.get("last")
    if isinstance(stamp, int) and now - stamp < HISTORY_MIN_INTERVAL_SEC:
      fresh.append(device)
      continue  # Too soon; do not spend a ring slot.
    entry["last"] = now

    for name, key in (
      ("tempC", "temperatureC"),
      ("health", "lifeRemainingPercent"),
      ("used", "percentageUsed"),
      ("written", "tbwTiB"),
    ):
      value = disk.get(key)
      if isinstance(value, (int, float)):
        points = bucket.get(name)
        if not isinstance(points, list):
          points = bucket[name] = []
        points.append([now, round(float(value), 2)])
        if len(points) > HISTORY_MAX_POINTS:
          del points[: len(points) - HISTORY_MAX_POINTS]

  # Forget drives that have been unplugged for a long time, so the file cannot
  # grow without bound.
  for device in list(series):
    if len(series) > HISTORY_MAX_DEVICES and device not in fresh:
      last = series[device].get("last") if isinstance(series[device], dict) else None
      if isinstance(last, int) and now - last > 30 * 24 * 3600:
        del series[device]

  # `raised` is already stamped into store["notified"] by fire_alerts(); it is
  # kept in the signature so callers cannot forget to record alerts at all.
  del raised


def downsample(points: list[list[float]], limit: int = TREND_POINTS_MAX) -> list[list[float]]:
  """Reduce a series to at most `limit` points for display.

  Keeps the first and last sample and strides through the rest. Rates are
  always computed from the full-resolution series, never from this.
  """
  n = len(points)
  if n <= limit:
    return [[int(p[0]), round(float(p[1]), 2)] for p in points]
  step = (n - 1) / (limit - 1)
  out: list[list[float]] = []
  seen: set[int] = set()
  for i in range(limit):
    idx = int(round(i * step))
    if idx >= n:
      idx = n - 1
    if idx in seen:
      continue
    seen.add(idx)
    out.append([int(points[idx][0]), round(float(points[idx][1]), 2)])
  return out


def trend_for(disk: dict[str, Any]) -> dict[str, Any]:
  """Derive sparkline points and a wear projection from recorded history.

  The projection needs a week of baseline; without one, per-month rates are
  dominated by counter jitter and would be worse than showing nothing.
  """
  store = load_history()
  entry = (store.get("disks") or {}).get(disk.get("device") or "")
  if not isinstance(entry, dict):
    return {}
  bucket = entry.get("series")
  if not isinstance(bucket, dict):
    return {}

  def points(name: str) -> list[list[float]]:
    raw = bucket.get(name)
    if not isinstance(raw, list):
      return []
    return [p for p in raw if isinstance(p, list) and len(p) == 2 and isinstance(p[1], (int, float))]

  out: dict[str, Any] = {}
  for name in ("tempC", "health", "used", "written"):
    pts = points(name)
    if len(pts) >= 2:
      out[name] = downsample(pts)

  temp = points("tempC")
  if len(temp) >= 2:
    out["tempRange"] = [min(p[1] for p in temp), max(p[1] for p in temp)]

  used = points("used")
  if len(used) >= 2:
    span = used[-1][0] - used[0][0]
    delta = used[-1][1] - used[0][1]
    if span >= PROJECTION_MIN_SPAN_SEC and delta > 0:
      per_month = delta / span * 30 * 24 * 3600
      if 0 < per_month <= MAX_PROJECTED_PER_MONTH:
        remaining = 100 - used[-1][1]
        if remaining > 0:
          out["monthsLeft"] = round(remaining / per_month, 1)
        out["wearPerMonth"] = round(per_month, 2)

  written = points("written")
  if len(written) >= 2:
    span = written[-1][0] - written[0][0]
    delta = written[-1][1] - written[0][1]
    if span >= 3600 and delta > 0:
      out["writeTiBPerMonth"] = round(delta / span * 30 * 24 * 3600, 2)

  return out


# --- Notifications ----------------------------------------------------------

def notify(title: str, body: str, urgent: bool) -> None:
  """Fire a desktop notification without blocking the poll."""
  argv = ["notify-send", "-a", "Disk Health"]
  if urgent:
    argv += ["-u", "critical"]
  argv += ["-i", "drive-harddisk-symbolic", title, body]
  try:
    subprocess.Popen(
      argv,
      stdin=subprocess.DEVNULL,
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
      start_new_session=True,
    )
  except OSError:
    pass  # No notification daemon is not a reason to fail the poll.


def fire_alerts(
  disks: list[dict[str, Any]],
  th: dict[str, Any],
  enabled: bool,
  store: dict[str, Any],
) -> list[dict[str, Any]]:
  """Notify on threshold crossing, with per-alert repeat suppression.

  Stamps `store` in place; the caller persists it once together with the new
  history samples so alert state and samples can never drift apart.
  """
  notified = store.get("notified")
  if not isinstance(notified, dict):
    notified = {}
    store["notified"] = notified
  now = int(time.time())
  raised: list[dict[str, Any]] = []

  for disk in disks:
    verdict = disk.get("verdict") or {}
    severity = verdict.get("severity") if isinstance(verdict, dict) else None
    if severity == "ok":
      continue
    reasons = verdict.get("reasons") if isinstance(verdict, dict) else []
    if not isinstance(reasons, list) or not reasons:
      continue
    device = disk.get("device") or "drive"
    # Key on device+severity so a drive worsening from warn to critical
    # notifies immediately instead of waiting out the repeat window.
    key = "%s|%s" % (device, severity)
    last = notified.get(key)
    if isinstance(last, int):
      if now - last < MIN_NOTIFY_GAP_SEC:
        continue
      if now - last < NOTIFY_REPEAT_SEC and severity == "warn":
        continue

    headline = verdict.get("headline") or reasons[0]
    detail = "; ".join(str(r) for r in reasons[:4])
    model = disk.get("model") or device
    title = "Disk %s: %s" % (model, headline)
    body = "%s — %s" % (device, detail)
    if enabled:
      notify(title, body, severity == "critical")
    raised.append({"key": key, "severity": severity})
    # Stamp regardless of whether it was actually sent, so a disabled alert
    # cannot bank a notification to fire later when re-enabled.
    notified[key] = now

  return raised


def disk_rank(disk: dict[str, Any]) -> tuple[int, int, int, int]:
  """Sort key: most urgent drive first."""
  life = disk.get("lifeRemainingPercent")
  health = disk.get("healthPercent")
  return (
    0 if disk.get("warning") else 1,
    0 if disk.get("passed") is False else 1,
    life if isinstance(life, int) else 101,
    health if isinstance(health, int) else 101,
  )


def fetch_all(
  drives: list[dict[str, Any]], requested: str, th: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
  """Query SMART for every enumerated drive (or just the requested one).

  Returns (summaries, failed) where summaries is sorted worst-first.
  """
  chosen = pick_disk(drives, requested) if requested else None
  targets = [chosen] if chosen is not None else list(drives)
  summaries: list[dict[str, Any]] = []
  failed: list[dict[str, Any]] = []
  for disk in targets:
    if disk is None:
      continue
    if len(summaries) >= MAX_DISKS:
      break
    result = fetch_smart(disk)
    if result.get("ok") is False or "lifeRemainingPercent" not in result:
      failed.append(result)
    else:
      summaries.append(result)
  for disk in summaries:
    disk["verdict"] = judge(disk, th)
  summaries.sort(key=disk_rank)
  return summaries, failed


def main() -> int:
  raw_arg = sys.argv[1] if len(sys.argv) > 1 else ""
  requested = "" if raw_arg.startswith("--") else clamp_str(raw_arg, MAX_DEVICE_ARG_LEN)
  thresholds = parse_thresholds(sys.argv[1:])
  alerts_enabled = "noalerts" not in sys.argv[1:]
  deadline = time.monotonic() + PROCESS_DEADLINE_SEC

  try:
    objects = get_managed_objects()
  except Exception as exc:
    emit(
      {
        "ok": False,
        "error": "udisks_unavailable",
        "message": clamp_str(f"Could not talk to UDisks2: {exc}", MAX_MESSAGE_LEN),
        "needsSetup": False,
        "devices": [],
        "disk": None,
      }
    )
    return 0

  disks = enumerate_drives(objects, deadline)
  # Drop the managed-object tree as soon as the light inventory exists.
  del objects

  devices = [
    {
      "name": clamp_str(d.get("device") or d.get("path")),
      "type": clamp_str(d.get("protocol") or "", 16),
      "info": clamp_str(d.get("model") or d.get("device") or ""),
    }
    for d in disks
  ]

  if not disks or (requested and pick_disk(disks, requested) is None):
    emit(
      {
        "ok": False,
        "error": "no_devices",
        "message": (
          "No matching NVMe/ATA drive was found."
          if requested
          else "No NVMe/ATA drives with SMART data were found."
        ),
        "needsSetup": False,
        "devices": devices,
        "disks": [],
        "disk": None,
      }
    )
    return 0

  if time.monotonic() > deadline:
    emit(
      {
        "ok": False,
        "error": "timeout",
        "message": "SMART status collection timed out.",
        "needsSetup": False,
        "devices": devices,
        "disks": [],
        "disk": None,
      }
    )
    return 0

  summaries, failed = fetch_all(disks, requested, thresholds)
  if not summaries:
    first_error = failed[0].get("error") if failed else ""
    emit(
      {
        "ok": False,
        "error": "smart_unavailable",
        "message": clamp_str(first_error or "Could not read SMART attributes.", MAX_MESSAGE_LEN),
        "needsSetup": False,
        "devices": devices,
        "disks": [],
        "disk": None,
      }
    )
    return 0

  for index, disk in enumerate(summaries):
    # summaries are worst-first, so the drives that matter most keep trends.
    disk["trend"] = trend_for(disk) if index < TREND_MAX_DISKS else {}

  # One load/modify/write for both alert state and history samples.
  store = load_history()
  try:
    raised = fire_alerts(summaries, thresholds, alerts_enabled, store)
  except Exception:
    raised = []
  try:
    record_history(store, summaries, raised)
    write_history(store)
  except Exception:
    pass  # History is a nice-to-have; never let it break the widget.

  emit(
    {
      "ok": True,
      "error": "",
      "message": "",
      "needsSetup": False,
      "devices": devices,
      # Worst drive first, so "disk" is the one that matters most.
      "disks": summaries,
      "disk": summaries[0],
    }
  )
  return 0


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except SystemExit:
    raise
  except Exception as exc:
    # Never dump tracebacks to stderr (QML no longer collects it).
    emit(
      {
        "ok": False,
        "error": "internal_error",
        "message": clamp_str(exc, MAX_MESSAGE_LEN),
        "needsSetup": False,
        "devices": [],
        "disks": [],
        "disk": None,
      }
    )
    raise SystemExit(0)
