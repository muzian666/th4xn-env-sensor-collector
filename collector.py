#!/usr/bin/env python3
"""
TH4xN Environment Sensor Data Collector + Dashboard
Intercepts UDP data from YiWeiLian TH4xN sensors and stores locally.
Provides a web dashboard at port 8080.

Protocol format:
  Frame: 0x7e <payload> <checksum:2> 0x0d
  Payload: <device_type:1> <device_id:5> <cmd:1> <seq:1> <data_length:1> <data:N>
  Temperature: 1 byte at body[1], in 0.1 degC
  Humidity: 2 bytes big-endian at body[2:4], in 0.1 %RH
"""

import socket
import sqlite3
import struct
import logging
import signal
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

import os

LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "6666"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent / "sensor_data.db")))
LOG_LEVEL = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)

# Response templates captured from the sensor's default cloud server (120.79.239.247:6666)
# Replace the hex payloads below with ones captured from your own sensors.
# The device ID portion (bytes 2-6) is patched at runtime by build_response().
RESPONSE_CMD_01 = bytes.fromhex(
    "7ec0XXXXXXXXXXXX010001000" "01b00001a040810"
    "291f0001000100040001" "5e00000001032000640"
    "00198a90d"
)
RESPONSE_CMD_02 = bytes.fromhex(
    "7ec0XXXXXXXXXXXX0200010000000bf00d"
)

# Global DB lock for thread safety
_db_lock = threading.Lock()


# ── Database ───────────────────────────────────────────────────
def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            device_id TEXT NOT NULL,
            cmd INTEGER NOT NULL,
            temperature_f REAL,
            temperature_c REAL,
            humidity REAL,
            raw_hex TEXT NOT NULL,
            payload_hex TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_packets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            direction TEXT NOT NULL,
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            raw_hex TEXT NOT NULL,
            length INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_config (
            device_id TEXT PRIMARY KEY,
            temp_low REAL DEFAULT 15.0,
            temp_high REAL DEFAULT 35.0,
            humidity_low REAL DEFAULT 20.0,
            humidity_high REAL DEFAULT 80.0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS device_names (
            device_id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


# ── Protocol Parser ────────────────────────────────────────────
def parse_packet(data: bytes) -> dict | None:
    """Parse a TH4xN protocol packet."""
    if len(data) < 10 or data[0] != 0x7E or data[-1] != 0x0D:
        return None

    payload = data[1:-1]  # strip 0x7e and 0x0d
    if len(payload) < 10:
        return None

    device_type = payload[0]
    device_id = payload[1:6].hex().upper()
    cmd = payload[6]
    seq = payload[7]
    data_len = payload[8]
    body = payload[9:9 + data_len]
    checksum = payload[9 + data_len:9 + data_len + 2]

    result = {
        "device_type": device_type,
        "device_id": device_id,
        "cmd": cmd,
        "seq": seq,
        "data_len": data_len,
        "body": body,
        "body_hex": body.hex(),
        "checksum": checksum.hex(),
        "raw_hex": data.hex(),
        "payload_hex": payload.hex(),
    }

    # Parse sensor data from both cmd types
    if len(body) >= 4:
        result.update(_parse_sensor_data(body, cmd))

    return result


def _parse_sensor_data(body: bytes, cmd: int) -> dict:
    """Extract temperature and humidity from sensor data body.

    Body format (fixed offsets):
      [0]    = flags/channel
      [1]    = temperature, 0.1 degC (1 byte unsigned)
      [2:4]  = humidity, big-endian, 0.1 %RH
    """
    result = {"temperature_f": None, "temperature_c": None, "humidity": None}

    if len(body) >= 2:
        temp_raw = body[1]
        temp_c = round(temp_raw / 10, 1)
        result["temperature_c"] = temp_c
        result["temperature_f"] = round(temp_c * 9 / 5 + 32, 1)

    if len(body) >= 4:
        hum_raw = struct.unpack(">H", body[2:4])[0]
        if 0 < hum_raw <= 1000:
            result["humidity"] = round(hum_raw / 10, 1)

    return result


# ── Response Builder ───────────────────────────────────────────
def build_response(parsed: dict) -> bytes | None:
    """Build a response packet to keep the sensor happy."""
    device_id_bytes = bytes.fromhex(parsed["device_id"])
    cmd = parsed["cmd"]

    if cmd == 0x02:
        # Heartbeat ACK
        resp = bytearray(RESPONSE_CMD_02)
        # Patch device ID (bytes 2-6)
        resp[2:7] = device_id_bytes
        return bytes(resp)

    if cmd == 0x01:
        # Data report ACK
        resp = bytearray(RESPONSE_CMD_01)
        resp[2:7] = device_id_bytes
        return bytes(resp)

    return None


# ── FastAPI Web Server ─────────────────────────────────────────
def create_app(conn: sqlite3.Connection):
    from fastapi import FastAPI, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel

    app = FastAPI(title="Sensor Dashboard")

    # Serve dashboard HTML
    dashboard_path = Path(__file__).parent / "dashboard.html"

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return dashboard_path.read_text(encoding="utf-8")

    @app.get("/api/devices")
    async def list_devices():
        with _db_lock:
            rows = conn.execute(
                "SELECT DISTINCT device_id FROM sensor_readings ORDER BY device_id"
            ).fetchall()
            names = conn.execute("SELECT device_id, name FROM device_names").fetchall()
            name_map = {n["device_id"]: n["name"] for n in names}
        return [{"device_id": r["device_id"], "name": name_map.get(r["device_id"], "")} for r in rows]

    @app.get("/api/latest")
    async def latest_readings():
        with _db_lock:
            rows = conn.execute("""
                SELECT r.* FROM sensor_readings r
                INNER JOIN (
                    SELECT device_id, MAX(id) as max_id
                    FROM sensor_readings
                    WHERE temperature_c IS NOT NULL
                    GROUP BY device_id
                ) latest ON r.id = latest.max_id
            """).fetchall()

            alerts = conn.execute("SELECT * FROM alert_config").fetchall()
            alert_map = {a["device_id"]: dict(a) for a in alerts}

            names = conn.execute("SELECT device_id, name FROM device_names").fetchall()
            name_map = {n["device_id"]: n["name"] for n in names}

        result = []
        for r in rows:
            reading = dict(r)
            reading["device_name"] = name_map.get(reading["device_id"], "")
            # Check alert status
            cfg = alert_map.get(reading["device_id"])
            alert_status = "normal"
            if cfg:
                tc = reading.get("temperature_c")
                hum = reading.get("humidity")
                if tc is not None and (tc < cfg["temp_low"] or tc > cfg["temp_high"]):
                    alert_status = "temperature_alert"
                elif hum is not None and (hum < cfg["humidity_low"] or hum > cfg["humidity_high"]):
                    alert_status = "humidity_alert"
            reading["alert_status"] = alert_status
            result.append(reading)
        return result

    @app.get("/api/history")
    async def history(
        device_id: str = Query(...),
        hours: int = Query(24, ge=1, le=720),
    ):
        with _db_lock:
            rows = conn.execute(
                """SELECT timestamp, temperature_c, temperature_f, humidity
                   FROM sensor_readings
                   WHERE device_id = ? AND temperature_c IS NOT NULL
                     AND timestamp >= datetime('now', 'localtime', ? || ' hours')
                   ORDER BY timestamp ASC""",
                (device_id, f"-{hours}"),
            ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/stats")
    async def stats(
        device_id: str = Query(...),
        hours: int = Query(24, ge=1, le=720),
    ):
        with _db_lock:
            row = conn.execute(
                """SELECT
                     COUNT(*) as count,
                     ROUND(AVG(temperature_c), 1) as avg_temp,
                     ROUND(MIN(temperature_c), 1) as min_temp,
                     ROUND(MAX(temperature_c), 1) as max_temp,
                     ROUND(AVG(humidity), 1) as avg_humidity,
                     ROUND(MIN(humidity), 1) as min_humidity,
                     ROUND(MAX(humidity), 1) as max_humidity
                   FROM sensor_readings
                   WHERE device_id = ? AND temperature_c IS NOT NULL
                     AND timestamp >= datetime('now', 'localtime', ? || ' hours')""",
                (device_id, f"-{hours}"),
            ).fetchone()
        return dict(row)

    @app.get("/api/alerts")
    async def get_alerts():
        with _db_lock:
            rows = conn.execute("SELECT * FROM alert_config").fetchall()
        return [dict(r) for r in rows]

    class AlertConfig(BaseModel):
        device_id: str
        temp_low: float = 15.0
        temp_high: float = 35.0
        humidity_low: float = 20.0
        humidity_high: float = 80.0

    @app.post("/api/alerts")
    async def set_alert(cfg: AlertConfig):
        with _db_lock:
            conn.execute(
                """INSERT INTO alert_config (device_id, temp_low, temp_high, humidity_low, humidity_high)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET
                     temp_low=excluded.temp_low, temp_high=excluded.temp_high,
                     humidity_low=excluded.humidity_low, humidity_high=excluded.humidity_high""",
                (cfg.device_id, cfg.temp_low, cfg.temp_high, cfg.humidity_low, cfg.humidity_high),
            )
            conn.commit()
        return {"status": "ok"}

    class DeviceName(BaseModel):
        device_id: str
        name: str

    @app.post("/api/device-name")
    async def set_device_name(cfg: DeviceName):
        with _db_lock:
            conn.execute(
                "INSERT INTO device_names (device_id, name) VALUES (?, ?) ON CONFLICT(device_id) DO UPDATE SET name=excluded.name",
                (cfg.device_id, cfg.name.strip()),
            )
            conn.commit()
        return {"status": "ok"}

    return app


def run_web_server(conn: sqlite3.Connection):
    """Run FastAPI in a background thread."""
    import uvicorn
    app = create_app(conn)
    log = logging.getLogger("collector")
    log.info(f"Web dashboard starting on port {HTTP_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT, log_level="warning")


# ── Main Loop ──────────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("collector")

    conn = init_db(DB_PATH)
    log.info(f"Database: {DB_PATH}")

    # Start web server in background thread
    web_thread = threading.Thread(target=run_web_server, args=(conn,), daemon=True)
    web_thread.start()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LISTEN_HOST, LISTEN_PORT))
    log.info(f"Listening on UDP {LISTEN_HOST}:{LISTEN_PORT}")

    running = True

    def _signal_handler(sig, frame):
        nonlocal running
        log.info("Shutting down...")
        running = False

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    while running:
        try:
            sock.settimeout(1.0)
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break

        src = f"{addr[0]}:{addr[1]}"

        # Store raw packet
        with _db_lock:
            conn.execute(
                "INSERT INTO raw_packets (direction, src, dst, raw_hex, length) VALUES (?,?,?,?,?)",
                ("in", src, f"{LISTEN_HOST}:{LISTEN_PORT}", data.hex(), len(data)),
            )
            conn.commit()

        # Parse
        parsed = parse_packet(data)
        if parsed is None:
            log.warning(f"Unparseable packet from {src}: {data.hex()}")
            continue

        cmd = parsed["cmd"]
        dev = parsed["device_id"]
        temp_f = parsed.get("temperature_f")
        temp_c = parsed.get("temperature_c")
        hum = parsed.get("humidity")

        if temp_c is not None:
            log.info(
                f"[{dev}] cmd={cmd:02d} temp={temp_f:.1f}F / {temp_c:.1f}C"
                + (f"  hum={hum:.1f}%" if hum is not None else "")
                + f"  body={parsed['body_hex']}"
            )
        else:
            log.info(f"[{dev}] cmd={cmd:02d} (heartbeat) body={parsed['body_hex']}")

        # Store parsed reading
        with _db_lock:
            conn.execute(
                """INSERT INTO sensor_readings
                   (device_id, cmd, temperature_f, temperature_c, humidity, raw_hex, payload_hex)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    dev,
                    cmd,
                    temp_f,
                    temp_c,
                    hum,
                    parsed["raw_hex"],
                    parsed["payload_hex"],
                ),
            )
            conn.commit()

        # Send response back to sensor
        resp = build_response(parsed)
        if resp:
            try:
                sock.sendto(resp, addr)
                with _db_lock:
                    conn.execute(
                        "INSERT INTO raw_packets (direction, src, dst, raw_hex, length) VALUES (?,?,?,?,?)",
                        ("out", f"{LISTEN_HOST}:{LISTEN_PORT}", src, resp.hex(), len(resp)),
                    )
                    conn.commit()
                log.debug(f"  -> sent response ({len(resp)} bytes) to {src}")
            except OSError as e:
                log.error(f"  -> failed to send response: {e}")

    sock.close()
    conn.close()
    log.info("Stopped.")


if __name__ == "__main__":
    main()
