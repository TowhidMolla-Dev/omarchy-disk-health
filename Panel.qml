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
    command: root.configuredDevice !== ""
      ? ["python3", root.pluginDir + "/status.py", root.configuredDevice]
      : ["python3", root.pluginDir + "/status.py"]
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
              text: (driveRow.drive && driveRow.drive.device ? String(driveRow.drive.device) : "")
                + "  ·  " + smartStatusLine.verdict
                + (driveRow.drive && driveRow.drive.warning === true ? "  ·  needs attention" : "")
              color: driveRow.drive && (driveRow.drive.passed === false || driveRow.drive.warning === true)
                ? (root.bar ? root.bar.urgent : Color.urgent)
                : root.contentDim
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
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
