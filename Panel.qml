import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "io.github.TowhidMolla-Dev.disk-health"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null

  property var status: null
  property bool loading: false
  property string lastError: ""

  readonly property var disk: Model.worstDisk(Model.diskList(status))
  readonly property var disks: Model.diskList(status)
  readonly property bool warning: {
    if (disks.length) {
      for (var i = 0; i < disks.length; i++)
        if (disks[i] && disks[i].warning === true) return true
      return false
    }
    return status ? status.ok === false : false
  }
  readonly property string label: Model.barLabelForStatus(status)

  readonly property int refreshIntervalSec: {
    var n = Number(setting("refreshIntervalSec", 300))
    if (!isFinite(n)) return 300
    return Math.max(60, Math.min(3600, Math.round(n)))
  }
  readonly property string configuredDevice: {
    var s = String(setting("device", "") || "").trim()
    // Keep in sync with status.py MAX_DEVICE_ARG_LEN.
    if (s.length > 64) return s.substring(0, 64)
    return s
  }

  // Alert thresholds live in the panel settings and are passed to status.py,
  // so there is exactly one source of truth for both the checks and the UI.
  readonly property int tempWarnC: clampedInt(setting("tempWarnC", 55), 0, 120)
  readonly property int tempCritC: Math.max(tempWarnC, clampedInt(setting("tempCritC", 70), 0, 120))
  readonly property int healthWarnPct: clampedInt(setting("healthWarnPct", 20), 0, 100)
  readonly property int healthCritPct: Math.min(healthWarnPct, clampedInt(setting("healthCritPct", 10), 0, 100))
  readonly property int spareWarnPct: clampedInt(setting("spareWarnPct", 10), 0, 100)
  readonly property bool alertsEnabled: setting("alertsEnabled", true) !== false

  function clampedInt(value, min, max) {
    var n = Number(value)
    if (!isFinite(n)) return min
    return Math.max(min, Math.min(max, Math.round(n)))
  }

  // Worst severity across all drives, for colouring the panel accent.
  readonly property string worstSeverity: {
    var order = { critical: 0, warn: 1, ok: 2, unknown: 3 }
    var worst = "unknown"
    for (var i = 0; i < disks.length; i++) {
      var s = Model.severity(disks[i])
      if (order[s] < order[worst]) worst = s
    }
    return worst
  }

  readonly property color severityColor: {
    if (worstSeverity === "critical") return bar ? bar.urgent : Color.urgent
    if (worstSeverity === "warn") return Color.warning
    return contentForeground
  }

  function statusArgv() {
    var argv = ["python3", root.pluginDir + "/status.py"]
    if (configuredDevice !== "") argv.push(configuredDevice)
    if (!alertsEnabled) argv.push("--noalerts")
    argv.push("--tempWarnC=" + tempWarnC)
    argv.push("--tempCritC=" + tempCritC)
    argv.push("--healthWarnPct=" + healthWarnPct)
    argv.push("--healthCritPct=" + healthCritPct)
    argv.push("--spareWarnPct=" + spareWarnPct)
    return argv
  }

  readonly property color contentForeground: bar ? bar.foreground : Color.foreground
  readonly property color contentDim: Qt.darker(contentForeground, 1.5)
  readonly property string contentFontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string pluginDir: {
    var path = String(Qt.resolvedUrl("."))
    if (path.indexOf("file://") === 0) path = path.substring(7)
    if (path.length > 1 && path.charAt(path.length - 1) === "/")
      path = path.substring(0, path.length - 1)
    return path
  }

  function open() {
    root.controller.show()
    refresh()
  }

  function close() {
    root.controller.hide()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.hostWidget || root, direction)
    return false
  }

  function refresh() {
    if (proc.running) return
    loading = true
    lastError = ""
    procWatchdog.restart()
    proc.running = true
  }

  function applyStatus(text) {
    var capped = Model.clampStatusText(text)
    var parsed = Model.parseStatus(capped)
    if (!parsed) {
      lastError = "Could not parse status output"
      status = null
      return
    }
    status = parsed
    if (!parsed.ok && parsed.message)
      lastError = String(parsed.message).substring(0, 256)
  }

  function metricValue(target, key) {
    if (!target) return "—"
    if (key === "health") {
      var pct = Model.healthyPercent(target)
      return pct === null ? "—" : pct + "%"
    }
    if (key === "hours") return Model.formatHours(target.powerOnHours)
    if (key === "temp") return Model.formatTemp(target.temperatureC)
    if (key === "cycles") return Model.formatCount(target.powerCycles)
    if (key === "tbw") return Model.formatTiB(target.tbwTiB)
    if (key === "life") return Model.formatPercent(target.lifeRemainingPercent)
    if (key === "spare") return Model.formatPercent(target.availableSparePercent)
    if (key === "used") return Model.formatPercent(target.percentageUsed)
    // Media and data integrity errors for SSDs; sector reallocation for HDDs.
    if (key === "errors")
      return Model.formatCount(target.protocol === "nvme" ? target.mediaErrors : target.reallocatedSectors)
    if (key === "pending") return Model.formatCount(target.pendingSectors)
    if (key === "uncorrectable") return Model.formatCount(target.offlineUncorrectable)
    if (key === "crc") return Model.formatCount(target.udmaCrcErrors)
    if (key === "badblocks") return Model.formatCount(target.badBlocks)
    return "—"
  }

  // Metric rows per drive type, so an HDD never shows NVMe-only columns.
  function metricsFor(target) {
    if (!target) return []
    if (target.protocol === "nvme") {
      return [
        { label: "Health", key: "health" },
        { label: "Power-on", key: "hours" },
        { label: "Temperature", key: "temp" },
        { label: "Media errors", key: "errors" },
        { label: "Data written", key: "tbw" },
        { label: "Spare", key: "spare" },
        { label: "Wear used", key: "used" },
        { label: "Power cycles", key: "cycles" }
      ]
    }
    return [
      { label: "Health", key: "health" },
      { label: "Power-on", key: "hours" },
      { label: "Temperature", key: "temp" },
      { label: "Data written", key: "tbw" },
      { label: "Reallocated", key: "errors" },
      { label: "Pending", key: "pending" },
      { label: "Uncorrectable", key: "uncorrectable" },
      { label: "CRC errors", key: "crc" },
      { label: "Bad blocks", key: "badblocks" },
      { label: "Power cycles", key: "cycles" }
    ]
  }

  Timer {
    id: pollTimer
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  // Keep in sync with status.py PROCESS_DEADLINE_SEC (+ small grace).
  Timer {
    id: procWatchdog
    interval: 50000
    repeat: false
    onTriggered: {
      if (!proc.running) return
      proc.signal(15)
      procKillTimer.restart()
    }
  }

  Timer {
    id: procKillTimer
    interval: 2000
    repeat: false
    onTriggered: {
      if (!proc.running) return
      proc.signal(9)
      root.loading = false
      root.lastError = "status.py timed out"
      root.status = null
    }
  }

  Component.onCompleted: Qt.callLater(root.refresh)

  Process {
    id: proc
    command: root.statusArgv()
    stdout: StdioCollector {
      id: statusStdout
      waitForEnd: true
    }
    // Do not attach a stderr collector: StdioCollector has no byte cap.
    // status.py emits all errors as bounded JSON on stdout.
    stderr: null
    onExited: function(exitCode) {
      procWatchdog.stop()
      procKillTimer.stop()
      root.loading = false
      // Cap before parse/UI; status.py also refuses to emit > MAX_JSON_BYTES.
      var out = Model.clampStatusText(statusStdout.text || "")
      if (out.trim() !== "") {
        root.applyStatus(out)
        return
      }
      root.lastError = "status.py failed (" + exitCode + ")"
      root.status = null
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(320))
    contentHeight: panel.fittedContentHeight(content.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: root.refresh()
      onTextKey: function(t) {
        if (t === "r" || t === "R") root.refresh()
      }

      Column {
        id: content
        width: parent.width
        spacing: Style.space(12)

        Text {
          width: parent.width
          text: root.disks.length > 1
            ? root.disks.length + " drives"
            : (root.disk && root.disk.model ? root.disk.model : "Disk SMART")
          color: root.contentForeground
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
          wrapMode: Text.WordWrap
        }

        Text {
          width: parent.width
          visible: root.lastError !== "" || (status && status.ok === false)
          text: root.lastError !== ""
            ? root.lastError
            : (status && status.message
                ? String(status.message).substring(0, 256)
                : "Could not read disk SMART data.")
          color: root.warning ? (root.bar ? root.bar.urgent : Color.urgent) : root.contentDim
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }

        Repeater {
          model: root.disks

          delegate: Column {
            id: driveRow
            required property var modelData
            required property int index

            readonly property var drive: modelData

            width: content.width
            spacing: Style.space(4)

            Rectangle {
              width: parent.width
              height: 1
              color: root.contentDim
              opacity: 0.25
              visible: driveRow.index > 0
            }

            Row {
              width: parent.width
              spacing: Style.space(8)

              Text {
                width: Style.space(140)
                text: driveRow.drive && driveRow.drive.model
                  ? String(driveRow.drive.model)
                  : "Unknown drive"
                color: root.contentForeground
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.body
                font.bold: true
                elide: Text.ElideRight
              }

              Text {
                text: Model.isSsd(driveRow.drive) ? "SSD" : "HDD"
                color: root.contentDim
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.bodySmall
              }
            }

            Text {
              id: smartStatusLine
              width: parent.width
              readonly property string verdict: {
                if (!driveRow.drive) return ""
                if (driveRow.drive.passed === true) return "SMART PASSED"
                if (driveRow.drive.passed === false) return "SMART FAILED"
                return "SMART status unknown"
              }
              // Lead with the verdict, not the raw percentage, so the panel
              // says why a drive is unhealthy instead of just how much.
              text: (driveRow.drive && driveRow.drive.device ? String(driveRow.drive.device) : "")
                + "  ·  " + (Model.verdictHeadline(driveRow.drive) || smartStatusLine.verdict)
              color: {
                var s = Model.severity(driveRow.drive)
                if (s === "critical" || (driveRow.drive && driveRow.drive.passed === false))
                  return (root.bar ? root.bar.urgent : Color.urgent)
                if (s === "warn") return Color.warning
                return root.contentDim
              }
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
              font.bold: Model.severity(driveRow.drive) !== "ok"
              wrapMode: Text.WordWrap
            }

            // Additional reasons beyond the headline.
            Repeater {
              model: Model.verdictReasons(driveRow.drive)

              Text {
                required property var modelData
                width: driveRow.width
                text: "· " + String(modelData)
                color: Model.severity(driveRow.drive) === "critical"
                  ? (root.bar ? root.bar.urgent : Color.urgent)
                  : Color.warning
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            Repeater {
              model: root.metricsFor(driveRow.drive)

              Row {
                required property var modelData
                width: driveRow.width
                spacing: Style.space(12)

                Text {
                  width: Style.space(140)
                  text: modelData.label
                  color: root.contentDim
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.bodySmall
                }

                Text {
                  text: root.metricValue(driveRow.drive, modelData.key)
                  color: root.contentForeground
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.body
                  font.bold: true
                }
              }
            }

            // Trend row: sparklines plus the derived rates. Hidden entirely
            // until enough samples exist to be meaningful.
            Row {
              id: trendRow
              width: driveRow.width
              spacing: Style.space(12)
              visible: Model.trend(driveRow.drive, "used").length >= 2
                || Model.trend(driveRow.drive, "written").length >= 2
                || Model.trend(driveRow.drive, "tempC").length >= 2

              Text {
                width: Style.space(140)
                text: "Trend"
                color: root.contentDim
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.bodySmall
              }

              Column {
                width: Math.max(0, trendRow.width - Style.space(140) - Style.space(12))
                spacing: Style.space(4)

                Repeater {
                  model: [
                    { label: "Temperature", key: "tempC", unit: " °C" },
                    { label: "Life used", key: "used", unit: "%" },
                    { label: "Data written", key: "written", unit: " TiB" }
                  ]

                  Row {
                    required property var modelData
                    width: parent.width
                    spacing: Style.space(8)
                    visible: Model.trend(driveRow.drive, modelData.key).length >= 2

                    Text {
                      width: Style.space(80)
                      text: modelData.label
                      color: root.contentDim
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                    }

                    Canvas {
                      id: spark
                      // Bound directly; onSeriesChanged repaints whenever the
                      // underlying trend data is replaced.
                      property var series: Model.trend(driveRow.drive, modelData.key)
                      property color strokeColor: root.contentForeground
                      width: Style.space(90)
                      height: Style.space(14)
                      renderStrategy: Canvas.Immediate
                      onSeriesChanged: requestPaint()
                      onPaint: {
                        var ctx = getContext("2d")
                        ctx.reset()
                        var pts = Model.sparkPoints(series, width, height, 1)
                        if (pts.length < 4) return
                        ctx.strokeStyle = strokeColor
                        ctx.lineWidth = 1.5
                        ctx.lineJoin = "round"
                        ctx.beginPath()
                        ctx.moveTo(pts[0], pts[1])
                        for (var i = 2; i < pts.length; i += 2)
                          ctx.lineTo(pts[i], pts[i + 1])
                        ctx.stroke()
                      }
                    }

                    Text {
                      property var last: spark.series.length
                        ? spark.series[spark.series.length - 1]
                        : null
                      text: last === null
                        ? "—"
                        : (Math.abs(last) < 10 ? last.toFixed(1) : Math.round(last)) + modelData.unit
                      color: root.contentForeground
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                    }
                  }
                }

                // Derived rates: only shown when the baseline is long enough
                // for the estimate to mean anything (see status.py).
                Row {
                  width: parent.width
                  spacing: Style.space(8)
                  visible: driveRow.drive && driveRow.drive.trend
                    && (driveRow.drive.trend.monthsLeft !== undefined
                        || driveRow.drive.trend.writeTiBPerMonth !== undefined)

                  Text {
                    width: Style.space(80)
                    text: "Projection"
                    color: root.contentDim
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    width: parent.width - Style.space(88)
                    text: {
                      if (!driveRow.drive || !driveRow.drive.trend) return ""
                      var t = driveRow.drive.trend
                      var parts = []
                      if (t.monthsLeft !== undefined)
                        parts.push("~" + Model.formatMonths(t.monthsLeft) + " of life left")
                      if (t.writeTiBPerMonth !== undefined)
                        parts.push(Model.formatRate(t.writeTiBPerMonth) + " written")
                      return parts.join("  ·  ")
                    }
                    color: root.contentDim
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }
                }
              }
            }
          }
        }

        Text {
          width: parent.width
          text: root.loading ? "Refreshing…" : "Press R or Enter to refresh"
          color: root.contentDim
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
