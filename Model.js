// Pure formatting helpers for NVMe Health (no Qt).

// Keep in sync with status.py MAX_JSON_BYTES (defense in depth before parse).
var MAX_STATUS_CHARS = 32768

function asInt(value, fallback) {
  var n = Number(value)
  if (!isFinite(n)) return fallback
  return Math.round(n)
}

function clampStatusText(text) {
  var s = String(text || "")
  if (s.length > MAX_STATUS_CHARS)
    return s.substring(0, MAX_STATUS_CHARS)
  return s
}

function formatHours(hours) {
  if (hours === null || hours === undefined || !isFinite(Number(hours))) return "—"
  var h = Math.max(0, Math.round(Number(hours)))
  if (h < 24) return h + "h"
  var days = Math.floor(h / 24)
  var rem = h % 24
  return days + "d " + rem + "h"
}

function formatTiB(value) {
  if (value === null || value === undefined || !isFinite(Number(value))) return "—"
  var n = Number(value)
  if (n < 0.1) return n.toFixed(2) + " TiB"
  if (n < 10) return n.toFixed(1) + " TiB"
  return Math.round(n) + " TiB"
}

function formatPercent(value) {
  if (value === null || value === undefined || !isFinite(Number(value))) return "—"
  return Math.round(Number(value)) + "%"
}

function healthyPercent(disk) {
  if (!disk) return null
  var life = disk.lifeRemainingPercent
  if (life !== null && life !== undefined && isFinite(Number(life))) return Math.round(Number(life))
  var health = disk.healthPercent
  if (health !== null && health !== undefined && isFinite(Number(health))) return Math.round(Number(health))
  return null
}

function worstDisk(disks) {
  if (!disks || !disks.length) return null
  var worst = null
  var worstRank = null
  for (var i = 0; i < disks.length; i++) {
    var d = disks[i]
    if (!d) continue
    var rank = [d.warning === true ? 0 : 1, d.passed === false ? 0 : 1, healthyPercent(d)]
    if (rank[2] === null) rank[2] = 101
    if (worstRank === null) {
      worst = d
      worstRank = rank
      continue
    }
    for (var k = 0; k < 3; k++) {
      if (rank[k] === worstRank[k]) continue
      if (rank[k] < worstRank[k]) {
        worst = d
        worstRank = rank
      }
      break
    }
  }
  return worst
}

function diskList(status) {
  if (!status || typeof status !== "object") return []
  var list = status.disks
  if (Array.isArray(list) && list.length) return list
  return status.disk ? [status.disk] : []
}

function barLabel(disk, _needsSetup, unavailable) {
  if (unavailable && !disk) return "?"
  if (!disk) return "—"
  if (disk.warning) return "!"
  var pct = healthyPercent(disk)
  return pct === null ? "OK" : pct + "%"
}

function barLabelForStatus(status) {
  return barLabel(worstDisk(diskList(status)), false, !status || status.ok === false)
}

function formatTemp(value) {
  if (value === null || value === undefined || !isFinite(Number(value))) return "—"
  return Math.round(Number(value)) + " °C"
}

function formatCount(value) {
  if (value === null || value === undefined || !isFinite(Number(value))) return "—"
  return Number(value).toLocaleString("en-US")
}

function isSsd(disk) {
  if (!disk) return true
  return disk.protocol === "nvme"
}

// Severity for a drive: "critical" > "warn" > "ok". Returns "unknown" when
// the payload has no verdict (older status.py or an unreadable drive).
function severity(disk) {
  if (!disk || typeof disk !== "object") return "unknown"
  var v = disk.verdict
  if (!v || typeof v !== "object") return "unknown"
  var s = String(v.severity || "")
  return s === "critical" || s === "warn" || s === "ok" ? s : "unknown"
}

function verdictHeadline(disk) {
  if (!disk || typeof disk.verdict !== "object") return ""
  return String(disk.verdict.headline || "")
}

// Reasons beyond the headline, so the panel does not print it twice.
function verdictReasons(disk) {
  if (!disk || typeof disk.verdict !== "object") return []
  var all = disk.verdict.reasons
  if (!Array.isArray(all)) return []
  var out = []
  for (var i = 0; i < all.length; i++) {
    var text = String(all[i] || "")
    if (!text) continue
    if (i === 0 && text === verdictHeadline(disk)) continue
    out.push(text)
  }
  return out
}

function trend(disk, name) {
  if (!disk || typeof disk.trend !== "object") return []
  var pts = disk.trend[name]
  if (!Array.isArray(pts) || pts.length < 2) return []
  var out = []
  for (var i = 0; i < pts.length; i++) {
    var p = pts[i]
    if (!Array.isArray(p) || p.length !== 2) continue
    var value = Number(p[1])
    if (!isFinite(value)) continue
    out.push(value)
  }
  return out.length >= 2 ? out : []
}

// Map values to 0..1 for drawing. When every sample is identical (a flat
// line) return a centred 0.5 so the sparkline draws mid-height instead of
// collapsing onto an edge.
function sparkPoints(values, width, height, padding) {
  var pts = []
  if (!Array.isArray(values) || values.length < 2) return pts
  var pad = padding === undefined ? 1 : padding
  var min = Math.min.apply(null, values)
  var max = Math.max.apply(null, values)
  var span = max - min
  var innerW = Math.max(1, width - pad * 2)
  var innerH = Math.max(1, height - pad * 2)
  var last = values.length - 1
  for (var i = 0; i < values.length; i++) {
    var fx = pad + (i / last) * innerW
    var fy
    if (span <= 0) {
      fy = pad + innerH / 2
    } else {
      fy = pad + innerH * (1 - (values[i] - min) / span)
    }
    pts.push(fx, fy)
  }
  return pts
}

function formatMonths(value) {
  if (value === null || value === undefined || !isFinite(Number(value))) return "—"
  var months = Number(value)
  if (months < 1) return "< 1 month"
  if (months < 24) return Math.round(months) + " months"
  var years = months / 12
  return (years < 10 ? years.toFixed(1) : Math.round(years)) + " years"
}

function formatRate(value) {
  if (value === null || value === undefined || !isFinite(Number(value))) return "—"
  var n = Number(value)
  if (n < 1) return n.toFixed(2) + " TiB/mo"
  if (n < 10) return n.toFixed(1) + " TiB/mo"
  return Math.round(n) + " TiB/mo"
}

function parseStatus(text) {
  try {
    var data = JSON.parse(clampStatusText(text))
    if (!data || typeof data !== "object" || Array.isArray(data)) return null
    return data
  } catch (e) {
    return null
  }
}

if (typeof module !== "undefined") {
  module.exports = {
    asInt: asInt,
    clampStatusText: clampStatusText,
    formatHours: formatHours,
    formatTiB: formatTiB,
    formatPercent: formatPercent,
    formatTemp: formatTemp,
    formatCount: formatCount,
    isSsd: isSsd,
    severity: severity,
    verdictHeadline: verdictHeadline,
    verdictReasons: verdictReasons,
    trend: trend,
    sparkPoints: sparkPoints,
    formatMonths: formatMonths,
    formatRate: formatRate,
    healthyPercent: healthyPercent,
    worstDisk: worstDisk,
    diskList: diskList,
    barLabel: barLabel,
    barLabelForStatus: barLabelForStatus,
    parseStatus: parseStatus,
    MAX_STATUS_CHARS: MAX_STATUS_CHARS
  }
}
