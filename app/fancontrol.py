#!/usr/bin/env python3
"""
X11 Fan Control - fan controller for Supermicro X11 boards (e.g. X11SCA-F)
running TrueNAS SCALE, over IPMI.

- Zone 0 (FAN1..FAN5): CPU / system
- Zone 1 (FANA):       peripheral, ideal for drive cage fans

Reads the CPU temperature from the BMC and the drive temperatures with
smartctl (without waking drives in standby), computes the duty cycle of each
zone from configurable curves and applies it with Supermicro raw commands.

Safety:
- starts in read-only mode (dry_run) until you turn control on;
- if readings fail repeatedly, fans are driven to full speed;
- when the container stops, the original BMC fan mode is restored.

https://github.com/pulquegc/x11-fancontrol - MIT License
"""
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else APP_DIR)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
UI_PATH = os.path.join(APP_DIR, "ui.html")
PORT = int(os.environ.get("PORT", "8765"))
UI_TOKEN = os.environ.get("UI_TOKEN", "")  # optional: required to save settings from the UI

# IPMI access: local (/dev/ipmi0) by default, or remote over LAN
IPMI_HOST = os.environ.get("IPMI_HOST", "")
IPMI_USER = os.environ.get("IPMI_USER", "")
IPMI_PASS = os.environ.get("IPMI_PASS", "")

FAN_MODES = {0: "Standard", 1: "Full", 2: "Optimal", 4: "Heavy IO"}

DEFAULT_CONFIG = {
    "dry_run": True,
    "interval": 10,
    "disk_interval": 60,
    "hysteresis": 2,
    "max_step_down": 5,
    "failsafe_duty": 100,
    "failsafe_after_errors": 3,
    "restore_mode_on_exit": 2,
    "cpu_sensor": "CPU Temp",
    "disks_exclude": [],
    "fan_lower_thresholds": None,
    "zones": {
        "0": {"name": "CPU / system (FAN1-FAN5)", "sources": ["cpu", "hdd"],
              "min_duty": 25, "max_duty": 100},
        "1": {"name": "Peripheral (FANA)", "sources": ["hdd"],
              "min_duty": 25, "max_duty": 100},
    },
    "curves": {
        "cpu":  [[40, 25], [55, 35], [65, 50], [75, 75], [85, 100]],
        "hdd":  [[32, 25], [36, 35], [40, 55], [43, 80], [46, 100]],
        "ssd":  [[45, 25], [55, 40], [65, 70], [70, 100]],
        "nvme": [[50, 25], [60, 40], [70, 70], [78, 100]],
    },
}

log = logging.getLogger("fancontrol")


# ---------------------------------------------------------------- helpers

def run(cmd, timeout=20):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ipmi(*args, timeout=20):
    cmd = ["ipmitool"]
    if IPMI_HOST:
        cmd += ["-I", "lanplus", "-H", IPMI_HOST, "-U", IPMI_USER, "-P", IPMI_PASS]
    cmd += [str(a) for a in args]
    r = run(cmd, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "ipmitool error").strip())
    return r.stdout


def interpolate(curve, temp):
    """Linear interpolation on a [[temp, duty], ...] curve."""
    pts = sorted(curve)
    if temp <= pts[0][0]:
        return pts[0][1]
    if temp >= pts[-1][0]:
        return pts[-1][1]
    for (t1, d1), (t2, d2) in zip(pts, pts[1:]):
        if t1 <= temp <= t2:
            if t2 == t1:
                return d2
            return d1 + (d2 - d1) * (temp - t1) / (t2 - t1)
    return pts[-1][1]


def deep_merge(base, extra):
    out = json.loads(json.dumps(base))
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "curves":
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def validate_config(cfg):
    cfg = deep_merge(DEFAULT_CONFIG, cfg)
    cfg["dry_run"] = bool(cfg["dry_run"])
    cfg["interval"] = max(3, int(cfg["interval"]))
    cfg["disk_interval"] = max(cfg["interval"], int(cfg["disk_interval"]))
    cfg["hysteresis"] = max(0.0, float(cfg["hysteresis"]))
    cfg["max_step_down"] = max(1, int(cfg["max_step_down"]))
    cfg["failsafe_duty"] = min(100, max(50, int(cfg["failsafe_duty"])))
    cfg["failsafe_after_errors"] = max(1, int(cfg["failsafe_after_errors"]))
    if int(cfg["restore_mode_on_exit"]) not in FAN_MODES:
        raise ValueError("restore_mode_on_exit must be 0, 1, 2 or 4")
    for name, curve in cfg["curves"].items():
        if not isinstance(curve, list) or len(curve) < 2:
            raise ValueError(f"Curve '{name}' needs at least 2 points")
        clean = []
        for p in curve:
            t, d = float(p[0]), int(p[1])
            if not 0 <= d <= 100:
                raise ValueError(f"Curve '{name}': duty {d} is outside 0-100")
            clean.append([t, d])
        cfg["curves"][name] = sorted(clean)
    for zid, z in cfg["zones"].items():
        if zid not in ("0", "1"):
            raise ValueError("Valid zones are '0' and '1'")
        z["min_duty"] = min(100, max(0, int(z.get("min_duty", 25))))
        z["max_duty"] = min(100, max(z["min_duty"], int(z.get("max_duty", 100))))
        z["sources"] = [s for s in z.get("sources", []) if s in cfg["curves"]]
    th = cfg.get("fan_lower_thresholds")
    if th is not None:
        th = [int(x) for x in th]
        if len(th) != 3 or not th[0] < th[1] < th[2]:
            raise ValueError("fan_lower_thresholds: three increasing values, e.g. [100, 200, 300]")
        cfg["fan_lower_thresholds"] = th
    return cfg


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return validate_config(json.load(f))
    cfg = validate_config({})
    save_config(cfg)
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------- sensors

SDR_VALUE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(degrees C|RPM)")


def read_sdr(sensor_type):
    """Return {name: value} for sensors of one type ('Temperature' or 'Fan')."""
    out = {}
    for line in ipmi("sdr", "type", sensor_type).splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        m = SDR_VALUE.search(parts[4])
        out[parts[0]] = float(m.group(1)) if m else None
    return out


def scan_disks(exclude):
    r = run(["smartctl", "--scan"])
    disks = []
    for line in r.stdout.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        tok = line.split("#")[0].split()
        dev = tok[0]
        if any(x and x in dev for x in exclude):
            continue
        disks.append((dev, tok[1:]))  # tok[1:] e.g. ['-d', 'sat']
    return disks


def read_disk(dev, dtype_args):
    """Return a dict with kind/temp/model, or None if unreadable."""
    r = run(["smartctl", "-i", "-A", "-n", "standby", "-j", *dtype_args, dev], timeout=30)
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    pm = (data.get("power_mode") or "").upper()
    if "STANDBY" in pm or "SLEEP" in pm:
        return {"dev": dev, "kind": None, "temp": None, "standby": True,
                "model": data.get("model_name", "")}
    temp = (data.get("temperature") or {}).get("current")
    if temp is None:
        return None
    dtype = (data.get("device") or {}).get("type", "")
    if dtype == "nvme" or "nvme" in dev:
        kind = "nvme"
    elif data.get("rotation_rate") == 0:
        kind = "ssd"
    else:
        kind = "hdd"
    return {"dev": dev, "kind": kind, "temp": float(temp), "standby": False,
            "model": data.get("model_name", "")}


# ---------------------------------------------------------------- BMC

def get_fan_mode():
    out = ipmi("raw", "0x30", "0x45", "0x00").split()
    return int(out[0], 16)


def set_fan_mode(mode):
    ipmi("raw", "0x30", "0x45", "0x01", f"0x{mode:02x}")


def set_zone_duty(zone, duty):
    ipmi("raw", "0x30", "0x70", "0x66", "0x01", f"0x{int(zone):02x}", f"0x{int(duty):02x}")


def set_fan_thresholds(fan_names, th):
    for name in fan_names:
        try:
            ipmi("sensor", "thresh", name, "lower", *th)
            log.info("Lower thresholds of %s set to %s RPM", name, th)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not set thresholds of %s: %s", name, e)


# ---------------------------------------------------------------- controller

class Controller:
    def __init__(self):
        self.lock = threading.Lock()
        self.cfg = load_config()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.active = False           # are we driving the fans?
        self.original_mode = None
        self.duty = {}                # zone -> current duty
        self.disks = []
        self.last_disk_read = 0.0
        self.errors = 0
        self.history = deque(maxlen=720)
        self.status = {"started": time.time(), "message": "starting"}

    # -- fan mode handling

    def take_control(self, fans):
        mode = get_fan_mode()
        self.original_mode = mode
        log.info("Original BMC fan mode: %s", FAN_MODES.get(mode, mode))
        th = self.cfg.get("fan_lower_thresholds")
        if th:
            set_fan_thresholds([f for f in fans], th)
        if mode != 1:
            set_fan_mode(1)  # Full: the BMC stops regulating and accepts duty cycles
            time.sleep(2)
        self.duty = {}
        self.active = True
        log.info("Fan control ACTIVE")

    def release_control(self):
        if not self.active:
            return
        mode = int(self.cfg.get("restore_mode_on_exit", 2))
        if self.original_mode is not None and self.original_mode != 1:
            mode = self.original_mode
        try:
            set_fan_mode(mode)
            log.info("Restored BMC fan mode: %s", FAN_MODES.get(mode, mode))
        except Exception as e:  # noqa: BLE001
            log.error("Could not restore BMC fan mode: %s", e)
        self.active = False

    # -- loop

    def read_disks_if_due(self, force=False):
        now = time.time()
        if not force and now - self.last_disk_read < self.cfg["disk_interval"]:
            return
        self.last_disk_read = now
        found = []
        for dev, args in scan_disks(self.cfg.get("disks_exclude", [])):
            try:
                d = read_disk(dev, args)
            except Exception as e:  # noqa: BLE001
                log.warning("Reading %s failed: %s", dev, e)
                d = None
            if d:
                found.append(d)
        self.disks = found

    def compute(self, temps):
        cfg = self.cfg
        result = {}
        for zid, z in cfg["zones"].items():
            up, down, why = z["min_duty"], z["min_duty"], "minimum"
            for src in z["sources"]:
                t = temps.get(src)
                if t is None:
                    continue
                c = cfg["curves"][src]
                u = interpolate(c, t)
                d = interpolate(c, t + cfg["hysteresis"])
                if u > up:
                    up, why = u, f"{src} {t:.0f}°C"
                down = max(down, d)
            up = min(z["max_duty"], max(z["min_duty"], round(up)))
            down = min(z["max_duty"], max(z["min_duty"], round(down)))
            cur = self.duty.get(zid)
            if cur is None or up > cur:
                new = up
            elif down < cur:
                new = max(down, cur - cfg["max_step_down"])
            else:
                new = cur
            result[zid] = (new, why)
        return result

    def tick(self):
        with self.lock:
            cfg = self.cfg
        temps_raw = read_sdr("Temperature")
        fans = read_sdr("Fan")
        cpu = temps_raw.get(cfg["cpu_sensor"])
        if cpu is None:
            raise RuntimeError(f"Sensor '{cfg['cpu_sensor']}' not found or has no reading")
        self.read_disks_if_due()

        temps = {"cpu": cpu}
        for kind in ("hdd", "ssd", "nvme"):
            vals = [d["temp"] for d in self.disks if d["kind"] == kind and d["temp"] is not None]
            temps[kind] = max(vals) if vals else None

        # handle dry_run toggled at runtime
        if not cfg["dry_run"] and not self.active:
            self.take_control([n for n in fans if n.upper().startswith("FAN")])
        elif cfg["dry_run"] and self.active:
            self.release_control()

        targets = self.compute(temps)
        for zid, (duty, _) in targets.items():
            if self.active and self.duty.get(zid) != duty:
                set_zone_duty(zid, duty)
                log.info("Zone %s -> %d%%", zid, duty)
            self.duty[zid] = duty

        self.errors = 0
        snap = {
            "t": int(time.time()),
            "cpu": cpu,
            "hdd": temps["hdd"], "ssd": temps["ssd"], "nvme": temps["nvme"],
            "z0": targets.get("0", (None,))[0], "z1": targets.get("1", (None,))[0],
        }
        self.history.append(snap)
        with self.lock:
            self.status = {
                "started": self.status["started"],
                "updated": snap["t"],
                "dry_run": cfg["dry_run"],
                "active": self.active,
                "bmc_mode": None,
                "temps": temps,
                "temps_raw": temps_raw,
                "fans": fans,
                "disks": self.disks,
                "zones": {z: {"duty": d, "reason": w, "name": cfg["zones"][z]["name"]}
                          for z, (d, w) in targets.items()},
                "errors": 0,
                "message": "ok",
            }

    def failsafe(self, err):
        self.errors += 1
        log.error("Error (%d/%d): %s", self.errors, self.cfg["failsafe_after_errors"], err)
        with self.lock:
            self.status["message"] = f"error: {err}"
            self.status["errors"] = self.errors
        if self.errors >= self.cfg["failsafe_after_errors"] and self.active:
            fs = self.cfg["failsafe_duty"]
            log.error("FAILSAFE: fans at %d%%", fs)
            for zid in self.cfg["zones"]:
                try:
                    set_zone_duty(zid, fs)
                    self.duty[zid] = fs
                except Exception as e:  # noqa: BLE001
                    log.error("Failsafe for zone %s failed: %s", zid, e)
            if self.errors >= self.cfg["failsafe_after_errors"] * 3:
                # still failing: hand control back to the BMC
                self.release_control()

    def loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self.failsafe(e)
            self.wake.wait(self.cfg["interval"])
            self.wake.clear()
        self.release_control()

    def update_config(self, new):
        cfg = validate_config(new)
        with self.lock:
            self.cfg = cfg
            save_config(cfg)
        self.last_disk_read = 0
        self.wake.set()
        log.info("Configuration updated (dry_run=%s)", cfg["dry_run"])
        return cfg


# ---------------------------------------------------------------- web UI

def make_handler(ctrl):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                with open(UI_PATH, "rb") as f:
                    return self._send(200, f.read(), "text/html")
            if self.path in ("/icon.svg", "/icon.png"):
                ctype = "image/svg+xml" if self.path.endswith("svg") else "image/png"
                with open(os.path.join(APP_DIR, self.path[1:]), "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path == "/api/status":
                with ctrl.lock:
                    body = {"status": ctrl.status, "config": ctrl.cfg,
                            "history": list(ctrl.history),
                            "token_required": bool(UI_TOKEN)}
                return self._send(200, json.dumps(body))
            self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            if self.path != "/api/config":
                return self._send(404, json.dumps({"error": "not found"}))
            if UI_TOKEN and self.headers.get("X-Token") != UI_TOKEN:
                return self._send(403, json.dumps({"error": "invalid token"}))
            try:
                n = int(self.headers.get("Content-Length", "0"))
                cfg = ctrl.update_config(json.loads(self.rfile.read(n)))
                self._send(200, json.dumps({"ok": True, "config": cfg}))
            except Exception as e:  # noqa: BLE001
                self._send(400, json.dumps({"error": str(e)}))

    return Handler


# ---------------------------------------------------------------- main

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    os.makedirs(DATA_DIR, exist_ok=True)
    ctrl = Controller()
    log.info("X11 Fan Control started - dry_run=%s - UI on port %d", ctrl.cfg["dry_run"], PORT)
    if ctrl.cfg["dry_run"]:
        log.info("READ-ONLY mode: fans are not touched")

    def stop(*_):
        ctrl.stop.set()
        ctrl.wake.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(ctrl))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ctrl.loop()
    finally:
        ctrl.release_control()
        server.shutdown()
        log.info("Stopped")


if __name__ == "__main__":
    main()
