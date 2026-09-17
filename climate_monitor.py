#!/usr/bin/env python3
"""
Reads temperature/humidity from an Adafruit SHT41 (STEMMA QT) once,
then:
  1. Displays the reading on an Adafruit OLED FeatherWing (SH1107,
     128x64, STEMMA QT version), and
  2. Appends the reading to a monthly CSV file on OneDrive, via rclone.

Intended to be run hourly via cron:
    0 * * * * /home/andocoyote/Code/ClimateMonitor/.venv/bin/python3 \
        /home/andocoyote/Code/ClimateMonitor/climate_monitor.py \
        >> /home/andocoyote/Code/ClimateMonitor/climate_monitor.log 2>&1

Requires:
    pip install adafruit-circuitpython-sht4x
    pip install adafruit-circuitpython-displayio-sh1107
    pip install adafruit-circuitpython-display-text
    (adafruit-blinka-displayio installs automatically as a dependency)
    rclone installed and configured with a remote named "onedrive"

Wiring: SHT41 connects to the OLED FeatherWing's STEMMA QT port
(daisy-chained I2C). The FeatherWing itself connects to the Pi's
GPIO header via jumper wires:
    FeatherWing SDA -> Pi GPIO2 / physical pin 3
    FeatherWing SCL -> Pi GPIO3 / physical pin 5
    FeatherWing 3V  -> Pi 3.3V  / physical pin 1
    FeatherWing GND -> Pi GND   / physical pin 6
No reset wire needed — this FeatherWing has a built-in auto-reset
circuit.

Note: this is written as a one-shot script (reads once, draws once,
uploads once, exits) to match the hourly cron schedule. The OLED
holds whatever was last drawn on its own, so nothing needs to keep
running between hourly executions.
"""

import csv
import datetime
import os
import subprocess
import sys

import board
import displayio
import terminalio
from adafruit_display_text import bitmap_label as label
from i2cdisplaybus import I2CDisplayBus
import adafruit_displayio_sh1107
import adafruit_sht4x

# --- Configuration ---
OLED_WIDTH = 128
OLED_HEIGHT = 64
OLED_BORDER = 2
OLED_I2C_ADDRESS = 0x3C

RCLONE_REMOTE = "onedrive"
REMOTE_FOLDER = "PiLogs"
LOCAL_SCRATCH_DIR = "/tmp"  # RAM-backed once overlay filesystem is enabled
CSV_HEADER = ["timestamp", "temperature_f", "humidity_percent"]


# --- Sensor ---

def read_sensor(i2c) -> tuple[float, float]:
    sht = adafruit_sht4x.SHT4x(i2c)
    temperature_c, humidity_pct = sht.measurements
    temperature_f = temperature_c * 9 / 5 + 32

    return temperature_f, humidity_pct


# --- OLED display ---

def build_display(i2c) -> adafruit_displayio_sh1107.SH1107:
    displayio.release_displays()
    display_bus = I2CDisplayBus(i2c, device_address=OLED_I2C_ADDRESS)
    display = adafruit_displayio_sh1107.SH1107(display_bus, width=OLED_WIDTH, height=OLED_HEIGHT)
    # Avoid a race with Blinka's background auto-refresh thread while
    # we're still building the group below — without this, a partial
    # frame can get pushed mid-construction and corrupt what reaches
    # the display (shows up as scrambled/noisy pixels).
    display.auto_refresh = False
    return display


def show_reading(display, temperature_f: float, humidity_pct: float) -> None:
    splash = displayio.Group()

    # White background
    color_bitmap = displayio.Bitmap(OLED_WIDTH, OLED_HEIGHT, 1)
    color_palette = displayio.Palette(1)
    color_palette[0] = 0xFFFFFF
    bg_sprite = displayio.TileGrid(color_bitmap, pixel_shader=color_palette, x=0, y=0)
    splash.append(bg_sprite)

    # Smaller black inner rectangle (border effect)
    inner_bitmap = displayio.Bitmap(OLED_WIDTH - OLED_BORDER * 2, OLED_HEIGHT - OLED_BORDER * 2, 1)
    inner_palette = displayio.Palette(1)
    inner_palette[0] = 0x000000
    inner_sprite = displayio.TileGrid(inner_bitmap, pixel_shader=inner_palette, x=OLED_BORDER, y=OLED_BORDER)
    splash.append(inner_sprite)

    temperature_c = (temperature_f - 32) * 5 / 9
    line1 = f"Temp: {temperature_c:.1f}C / {temperature_f:.1f}F"
    line2 = f"Humidity: {humidity_pct:.1f}%"

    text_area1 = label.Label(terminalio.FONT, text=line1, color=0xFFFFFF, x=8, y=20)
    splash.append(text_area1)

    text_area2 = label.Label(terminalio.FONT, text=line2, color=0xFFFFFF, x=8, y=40)
    splash.append(text_area2)

    # Only now, with the group fully built, attach it and push one clean frame
    display.root_group = splash
    display.refresh()


# --- OneDrive logging (via rclone) ---

def get_monthly_filename() -> str:
    """One file per calendar month, e.g. readings-2026-08.csv"""
    return f"readings-{datetime.date.today().strftime('%Y-%m')}.csv"


def run_rclone(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["rclone"] + args, capture_output=True, text=True)


def remote_file_exists(filename: str) -> bool:
    """Check whether this month's file actually exists on OneDrive,
    independent of whether we can successfully download it right now."""
    remote_dir = f"{RCLONE_REMOTE}:{REMOTE_FOLDER}"
    result = run_rclone(["lsf", remote_dir, "--include", filename])
    return result.returncode == 0 and filename in result.stdout


def download_current_month(filename: str, local_path: str) -> bool:
    """
    Pull down this month's file from OneDrive.

    Returns True if it's safe to proceed (either the download
    succeeded, or the file genuinely doesn't exist yet and a fresh
    one was started). Returns False if the file exists remotely but
    we failed to download it for some other reason (network blip,
    auth hiccup, etc.) — in that case the caller must NOT proceed to
    upload, since doing so would overwrite real data with an
    incomplete local copy. This is the fix for a real incident where
    a transient download failure was misread as "new month" and the
    resulting near-empty file was uploaded over two weeks of history.
    """
    remote_path = f"{RCLONE_REMOTE}:{REMOTE_FOLDER}/{filename}"
    result = run_rclone(["copyto", remote_path, local_path])

    if result.returncode == 0:
        return True

    if remote_file_exists(filename):
        print(
            f"ERROR: {filename} exists on OneDrive but download failed "
            f"(not proceeding, to avoid overwriting existing data): {result.stderr}",
            file=sys.stderr,
        )
        return False

    # Genuinely doesn't exist yet — safe to start fresh.
    print(f"No existing remote file found ({filename}); starting a new one.")
    with open(local_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
    return True


def append_reading_to_csv(local_path: str, temperature_f: float, humidity_pct: float) -> None:
    timestamp = datetime.datetime.now().isoformat(timespec="seconds")
    with open(local_path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([timestamp, f"{temperature_f:.2f}", f"{humidity_pct:.2f}"])


def upload_current_month(filename: str, local_path: str) -> bool:
    """Push the updated local file back to OneDrive, overwriting the remote copy."""
    remote_path = f"{RCLONE_REMOTE}:{REMOTE_FOLDER}/{filename}"
    result = run_rclone(["copyto", local_path, remote_path])
    if result.returncode != 0:
        print(f"ERROR: rclone upload failed: {result.stderr}", file=sys.stderr)
        return False
    return True


# --- Main ---

def main() -> int:
    i2c = board.I2C()  # uses board.SCL and board.SDA, shared by both devices

    try:
        temperature_f, humidity_pct = read_sensor(i2c)
    except Exception as e:
        print(f"ERROR: failed to read sensor: {e}", file=sys.stderr)
        return 1

    # Update the OLED. A display problem shouldn't stop the OneDrive
    # upload, so this is non-fatal if it fails.
    try:
        display = build_display(i2c)
        show_reading(display, temperature_f, humidity_pct)
        print(f"Displayed {temperature_f:.1f}F / {humidity_pct:.1f}% on OLED")
    except Exception as e:
        print(f"WARNING: failed to update OLED: {e}", file=sys.stderr)

    # Log to OneDrive
    filename = get_monthly_filename()
    local_path = os.path.join(LOCAL_SCRATCH_DIR, filename)

    if not download_current_month(filename, local_path):
        # File exists remotely but we couldn't download it — do NOT
        # proceed to upload, since local_path doesn't reflect the real
        # remote history and uploading it would overwrite real data.
        # This hour's reading is lost, which is the acceptable
        # trade-off; the alternative (uploading anyway) risks losing
        # everything, which is not.
        print("ERROR: skipping this hour's upload to avoid data loss.", file=sys.stderr)
        return 1

    append_reading_to_csv(local_path, temperature_f, humidity_pct)

    if not upload_current_month(filename, local_path):
        # Reading is still in the local scratch file; next hour's run
        # will pick it up and re-append on top before re-uploading, so
        # nothing is permanently lost even if this hour's upload failed.
        return 1

    print(f"Logged {temperature_f:.2f}F / {humidity_pct:.2f}% to {filename}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
