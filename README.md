# Disk Health

![Disk Health panel on Omarchy](preview.png)

SMART health for every SSD **and** HDD in the Omarchy bar: remaining life, power-on hours, temperature, and the error counters that actually predict failure (media errors, reallocated/pending sectors, uncorrectable blocks, CRC errors).

Reads SMART through **UDisks2** (already on Omarchy). No `smartctl`, no root, no sudoers.

This is a fork of [qadram/omarchy-nvme-health](https://github.com/qadram/omarchy-nvme-health), extended to cover SATA/SAS hard drives and to report all drives instead of only the first.

## What it shows

Each drive gets its own block in the panel, with the metrics that apply to its type.

**SSDs (NVMe)**
- Health (remaining life), power-on hours, media/data integrity errors
- Data written (TBW), available spare, wear used, power cycles

**HDDs (SATA/SAS)**
- Health, power-on hours, temperature
- Reallocated sectors, pending sectors, offline-uncorrectable sectors
- Reported uncorrect, bad blocks, UDMA CRC errors, power cycles

The bar shows a single value for the **worst** drive: remaining life `%`, or `!` when any drive reports a problem.

## Install

```sh
omarchy plugin add https://github.com/TowhidMolla-Dev/omarchy-disk-health.git --enable
```

Or from a local checkout:

```sh
PLUGIN_ID=io.github.TowhidMolla-Dev.disk-health
PLUGIN_DIR="$HOME/.config/omarchy/plugins/$PLUGIN_ID"
mkdir -p "$PLUGIN_DIR"
cp -a manifest.json BarWidget.qml Panel.qml Model.js status.py "$PLUGIN_DIR/"
chmod +x "$PLUGIN_DIR/status.py"
omarchy plugin validate "$PLUGIN_DIR"
omarchy-shell shell rescanPlugins
omarchy plugin enable "$PLUGIN_ID" --section right
```

## Usage

- **Left click:** open the details panel
- **Middle click** (bar) or **R / Enter** (panel): refresh
- Bar label: worst drive's remaining life `%`, or `!` when something looks wrong

## Configure

```sh
omarchy bar move io.github.TowhidMolla-Dev.disk-health --section right
```

Optional settings on the widget entry in `~/.config/omarchy/shell.json`:

- `refreshIntervalSec` — 60–3600 (default `300`)
- `device` — e.g. `/dev/sda` (empty = monitor every drive)

## Notes on drive coverage

Drives are discovered through UDisks2, so anything the desktop stack can see is reported: NVMe, SATA, and USB-attached disks. Optical and removable-media devices are skipped.

A metric shows `—` when the drive does not expose it. That is deliberate: some fields (notably temperature) use vendor-specific encodings, so the plugin prefers reporting nothing over reporting a confidently wrong number.

## Remove

```sh
omarchy plugin remove io.github.TowhidMolla-Dev.disk-health
```

## License

MIT
