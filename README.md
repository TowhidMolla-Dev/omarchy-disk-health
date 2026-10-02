# Disk Health

![Disk Health panel on Omarchy](preview.png)

SMART health for every SSD **and** HDD in the Omarchy bar: remaining life, power-on hours, temperature, and the error counters that actually predict failure (media errors, reallocated/pending sectors, uncorrectable blocks, CRC errors).

Reads SMART through **UDisks2** (already on Omarchy). No `smartctl`, no root, no sudoers.

This is a fork of [qadram/omarchy-nvme-health](https://github.com/qadram/omarchy-nvme-health), extended to cover SATA/SAS hard drives and to report all drives instead of only the first.

## What it shows

Each drive gets its own block in the panel, with the metrics that apply to its type.

**SSDs (NVMe)**
- Health (remaining life), power-on hours, temperature, media/data integrity errors
- Data written (TBW), available spare, wear used, power cycles

**HDDs (SATA/SAS)**
- Health, power-on hours, temperature
- Data written, where the drive exposes Seagate's total-LBA counter (SMART 0xF1)
- Reallocated sectors, pending sectors, offline-uncorrectable sectors
- Reported uncorrect, bad blocks, UDMA CRC errors, power cycles

Each drive leads with a **verdict** rather than a bare percentage — "Healthy",
"12 reallocated sectors", "78 °C is over 70 °C" — followed by the specific
reasons behind it, so a number is never shown without its explanation.

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
- `alertsEnabled` — desktop notifications on threshold crossings (default `true`)
- `tempWarnC` / `tempCritC` — temperature limits (default `55` / `70`)
- `healthWarnPct` / `healthCritPct` — remaining-life limits (default `20` / `10`)
- `spareWarnPct` — NVMe spare limit (default `10`)

## Alerts

When a drive crosses a limit the plugin posts one desktop notification and
then stays quiet, so a failing disk does not notify on every poll. It speaks up
again if the drive gets worse, after 6 hours, or if you refresh far enough in
the future. Critical alerts use the urgent desktop hint.

## Trend history

Each poll is appended to `~/.local/state/disk-health/history.json`, one sample
per 4 minutes, ~2 days of retention. The panel draws a sparkline per drive and
adds two derived figures:

- **Write rate** (TiB/month) once there is more than an hour of baseline
- **Projected life left**, only after a full week of samples

The week-long minimum is deliberate. Two samples taken minutes apart imply
wildly wrong rates, so the plugin would rather show nothing than a confident
guess. Wear rates above 400%/month are also rejected as mis-decoded counters.

The file is written atomically, is capped in size, and forgetting it costs
nothing but the history. Delete it to reset trends:

```sh
rm ~/.local/state/disk-health/history.json
```

## Notes on drive coverage

Drives are discovered through UDisks2, so anything the desktop stack can see is reported: NVMe, SATA, and USB-attached disks. Optical and removable-media devices are skipped.

A metric shows `—` when the drive does not expose it. That is deliberate: some fields (notably HDD data written, which needs Seagate's SMART 0xF1 counter) use vendor-specific encodings, so the plugin prefers reporting nothing over reporting a confidently wrong number.

Two encodings are corrected so the numbers line up with `smartctl`:

- **Temperature** — UDisks2 reports `SmartTemperature` in whole degrees Kelvin on both NVMe and ATA, not tenths of Celsius. The plugin converts it rather than reading the vendor `RAW_VALUE`, which packs min/max/current on Seagate drives.
- **HDD data written** — UDisks2 returns Seagate's 48-bit total-LBA counters pre-multiplied by `2^25 / 10^6`. The plugin divides that back out, which puts the result within 0.01% of `smartctl`.

## Remove

```sh
omarchy plugin remove io.github.TowhidMolla-Dev.disk-health
```

## License

MIT
