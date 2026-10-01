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
    healthyPercent: healthyPercent,
    worstDisk: worstDisk,
    diskList: diskList,
    barLabel: barLabel,
    barLabelForStatus: barLabelForStatus,
    parseStatus: parseStatus,
    MAX_STATUS_CHARS: MAX_STATUS_CHARS
  }
}
