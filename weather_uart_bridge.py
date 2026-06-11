#!/usr/bin/env python3
"""
weather_uart_bridge.py
----------------------
1. Reads a ZIP code from Arduino over UART
2. Looks up lat/lon from a local CSV (zip_codes.csv)
3. Fetches today's weather from Open-Meteo (no API key required)
4. Sends a structured multi-field packet back to Arduino over UART

Serial protocol
  RX (from Arduino):  "<ZIPCODE>\n"
                      e.g. "92101\n"

  TX (to Arduino):    One field per line, terminated by "END\n"
                      HI:<temp_hi_F>
                      HITIME:<HH:MM>
                      LO:<temp_lo_F>
                      LOTIME:<HH:MM>
                      WIND:<mph>
                      SUNRISE:<HH:MM>
                      SUNSET:<HH:MM>
                      RAIN:<percent>
                      CLOUD:<percent>
                      HUMID:<percent>
                      PRESS:<hPa>
                      END

  On error:           "ERR:<reason>\n"

All times are local to the queried location (Open-Meteo returns them in
the location's timezone when timezone=auto).
"""

import csv
import sys
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime

import serial          # pip install pyserial
import requests        # pip install requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_PORT    = "/dev/ttyUSB0"
DEFAULT_BAUD    = 9600
DEFAULT_CSV     = "zip_codes.csv"
OPEN_METEO_URL  = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 10
RETRY_DELAY     = 2
MAX_RETRIES     = 3

CSV_ZIP_COL = "zip"
CSV_LAT_COL = "lat"
CSV_LON_COL = "lng"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ZIP database
# ---------------------------------------------------------------------------
def load_zip_db(csv_path: str) -> dict:
    path = Path(csv_path)
    if not path.exists():
        log.error("ZIP CSV not found: %s", csv_path)
        sys.exit(1)

    db = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                db[row[CSV_ZIP_COL].strip()] = (
                    float(row[CSV_LAT_COL]),
                    float(row[CSV_LON_COL]),
                )
            except (KeyError, ValueError):
                pass

    log.info("Loaded %d ZIP codes from %s", len(db), csv_path)
    return db


def lookup_zip(db: dict, zipcode: str):
    return db.get(zipcode.strip())


# ---------------------------------------------------------------------------
# Open-Meteo fetch
# ---------------------------------------------------------------------------
def fetch_weather(lat: float, lon: float) -> dict:
    """
    Fetch today's forecast from Open-Meteo.

    Hourly variables used to derive each field:
      temperature_2m              → hi/lo + times
      precipitation_probability   → rain chance (max of today)
      cloudcover                  → cloudiness (at current hour)
      relative_humidity_2m        → humidity (at current hour)
      surface_pressure            → pressure (at current hour)
      windspeed_10m               → wind speed (at current hour)

    Daily variables:
      sunrise, sunset             → today's values

    Returns a flat dict with all parsed values.
    Raises requests.RequestException on network failure.
    """
    params = {
        "latitude":         lat,
        "longitude":        lon,
        "hourly":           ",".join([
                                "temperature_2m",
                                "relative_humidity_2m",
                                "precipitation_probability",
                                "cloudcover",
                                "surface_pressure",
                                "windspeed_10m",
                            ]),
        "daily":            "sunrise,sunset",
        "temperature_unit": "fahrenheit",
        "windspeed_unit":   "mph",
        "forecast_days":    1,
        "timezone":         "auto",
    }

    resp = requests.get(OPEN_METEO_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    hourly  = data["hourly"]
    daily   = data["daily"]
    times   = hourly["time"]          # list of "YYYY-MM-DDTHH:MM" strings

    # Current hour index — match against wall-clock hour
    now_str  = datetime.now().strftime("%Y-%m-%dT%H:00")
    try:
        cur_idx = times.index(now_str)
    except ValueError:
        cur_idx = 0   # fallback to first slot if tz mismatch

    temps = hourly["temperature_2m"]

    # Hi / Lo over today's 24-hour window
    hi_temp  = max(temps)
    lo_temp  = min(temps)
    hi_time  = _fmt_time(times[temps.index(hi_temp)])
    lo_time  = _fmt_time(times[temps.index(lo_temp)])

    # Current-hour values
    wind_mph = round(hourly["windspeed_10m"][cur_idx], 1)
    cloud    = int(hourly["cloudcover"][cur_idx])
    humidity = int(hourly["relative_humidity_2m"][cur_idx])
    pressure = round(hourly["surface_pressure"][cur_idx], 1)

    # Max rain chance over the day
    rain_pct = max(hourly["precipitation_probability"])

    # Sunrise / sunset (daily returns lists; index 0 = today)
    sunrise  = _fmt_time(daily["sunrise"][0])
    sunset   = _fmt_time(daily["sunset"][0])

    return {
        "hi_f":      round(hi_temp, 1),
        "hi_time":   hi_time,
        "lo_f":      round(lo_temp, 1),
        "lo_time":   lo_time,
        "wind_mph":  wind_mph,
        "sunrise":   sunrise,
        "sunset":    sunset,
        "rain_pct":  rain_pct,
        "cloud_pct": cloud,
        "humid_pct": humidity,
        "press_hpa": pressure,
    }


def _fmt_time(iso: str) -> str:
    """Extract HH:MM from an ISO datetime string like '2024-06-10T06:23'."""
    try:
        return iso.split("T")[1][:5]
    except (IndexError, AttributeError):
        return "??:??"


# ---------------------------------------------------------------------------
# Packet formatter
# ---------------------------------------------------------------------------
def format_packet(w: dict) -> bytes:
    """
    Build the multi-line packet terminated with END.
    Each line ends with \\r\\n for maximum Arduino Serial compatibility.
    """
    lines = [
        f"HI:{w['hi_f']}",
        f"HITIME:{w['hi_time']}",
        f"LO:{w['lo_f']}",
        f"LOTIME:{w['lo_time']}",
        f"WIND:{w['wind_mph']}",
        f"SUNRISE:{w['sunrise']}",
        f"SUNSET:{w['sunset']}",
        f"RAIN:{w['rain_pct']}",
        f"CLOUD:{w['cloud_pct']}",
        f"HUMID:{w['humid_pct']}",
        f"PRESS:{w['press_hpa']}",
        "END",
    ]
    return "\r\n".join(lines).encode() + b"\r\n"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run(port: str, baud: int, csv_path: str):
    log.info("Loading ZIP database…")
    zip_db = load_zip_db(csv_path)

    log.info("Opening serial port %s @ %d baud…", port, baud)
    try:
        ser = serial.Serial(port, baud, timeout=5)
    except serial.SerialException as e:
        log.error("Cannot open serial port: %s", e)
        sys.exit(1)

    time.sleep(2)
    ser.reset_input_buffer()
    log.info("Ready — waiting for ZIP codes from Arduino…")

    try:
        while True:
            raw = ser.readline()
            if not raw:
                continue

            zipcode = raw.decode("ascii", errors="ignore").strip()
            if not zipcode:
                continue

            log.info("Received ZIP: %s", zipcode)

            coords = lookup_zip(zip_db, zipcode)
            if coords is None:
                err = f"ERR:UNKNOWN_ZIP:{zipcode}\r\n"
                log.warning("ZIP not found: %s", zipcode)
                ser.write(err.encode())
                continue

            lat, lon = coords
            log.info("  → lat=%.4f lon=%.4f", lat, lon)

            weather = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    weather = fetch_weather(lat, lon)
                    break
                except requests.RequestException as e:
                    log.warning("Attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY)

            if weather is None:
                ser.write(b"ERR:NETWORK\r\n")
                log.error("All retries exhausted for ZIP %s", zipcode)
                continue

            packet = format_packet(weather)
            ser.write(packet)

            log.info("Sent weather packet:")
            for line in packet.decode().splitlines():
                log.info("  %s", line)

    except KeyboardInterrupt:
        log.info("Interrupted — closing serial port.")
    finally:
        ser.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Weather UART bridge: reads ZIP from Arduino, returns weather."
    )
    parser.add_argument("--port", default=DEFAULT_PORT,
                        help=f"Serial port (default: {DEFAULT_PORT})")
    parser.add_argument("--baud", default=DEFAULT_BAUD, type=int,
                        help=f"Baud rate (default: {DEFAULT_BAUD})")
    parser.add_argument("--csv",  default=DEFAULT_CSV,
                        help=f"ZIP CSV file (default: {DEFAULT_CSV})")
    args = parser.parse_args()

    run(args.port, args.baud, args.csv)


if __name__ == "__main__":
    main()
