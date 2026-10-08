"""Window for D2R XP Pulse.

Double-click or run with no arguments. Finds the gold bar, shows the fill,
and pulses a toy. Screen capture only. Does not read the game process.

The CLI in d2r_xp_pulse.py still works. This file is the easy path.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import ttk

import cv2
import mss
import numpy as np
import websockets

CONFIG = Path("xp_pulse.json")
GOLD_LO = (12, 70, 110)
GOLD_HI = (42, 255, 255)


def load_config() -> dict:
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_config(data: dict) -> None:
    CONFIG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def gold_mask(bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, GOLD_LO, GOLD_HI)


def fill_of(bgr: np.ndarray) -> float:
    mask = gold_mask(bgr)
    cols = mask.shape[1]
    if cols == 0:
        return 0.0
    need = max(1, int(mask.shape[0] * 0.3))
    hits = np.where((mask > 0).sum(axis=0) >= need)[0]
    if hits.size == 0:
        return float((mask > 0).mean())
    return float((int(hits.max()) + 1) / cols)


def find_bar(sct, monitor: int) -> dict | None:
    mon = sct.monitors[monitor]
    frame = np.array(sct.grab(mon))
    bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    h, w = bgr.shape[:2]
    band = bgr[int(h * 0.72) :, :]
    mask = gold_mask(band)
    row_hits = (mask > 0).sum(axis=1)
    if row_hits.max() < 40:
        return None
    y0 = int(np.argmax(row_hits))
    ys = np.where(row_hits > row_hits.max() * 0.45)[0]
    top = int(ys.min())
    bottom = int(ys.max())
    col_hits = (mask[top : bottom + 1] > 0).sum(axis=0)
    xs = np.where(col_hits > 0)[0]
    if xs.size < 20:
        return None
    pad = 4
    return {
        "left": max(0, int(xs.min()) - pad),
        "top": int(h * 0.72) + max(0, top - pad),
        "width": min(w, int(xs.max() - xs.min()) + pad * 2),
        "height": max(8, bottom - top + pad * 2),
    }


def grab(sct, monitor: int, crop: dict) -> np.ndarray:
    origin = sct.monitors[monitor]
    region = {
        "left": origin["left"] + int(crop["left"]),
        "top": origin["top"] + int(crop["top"]),
        "width": int(crop["width"]),
        "height": int(crop["height"]),
    }
    frame = np.array(sct.grab(region))
    return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)


class Intiface:
    def __init__(self, url: str) -> None:
        self.url = url
        self.ws = None
        self.msg_id = 1
        self.devices: dict[int, dict] = {}

    def next_id(self) -> int:
        self.msg_id += 1
        return self.msg_id

    async def connect(self) -> str:
        self.ws = await websockets.connect(self.url)
        await self.ws.send(json.dumps([{"RequestServerInfo": {"Id": self.next_id(), "ClientName": "d2r-xp-pulse", "MessageVersion": 3}}]))
        await self._drain(0.4)
        await self.ws.send(json.dumps([{"StartScanning": {"Id": self.next_id()}}]))
        await self._drain(2.0)
        await self.ws.send(json.dumps([{"RequestDeviceList": {"Id": self.next_id()}}]))
        await self._drain(0.4)
        if not self.devices:
            return "Intiface up, no toy yet"
        return "Toys: " + ", ".join(d["name"] for d in self.devices.values())

    async def _drain(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            for msg in json.loads(raw):
                if "DeviceAdded" in msg:
                    self._remember(msg["DeviceAdded"])
                elif "DeviceList" in msg:
                    for dev in msg["DeviceList"].get("Devices", []):
                        self._remember(dev)

    def _remember(self, dev: dict) -> None:
        msgs = dev.get("DeviceMessages", {})
        self.devices[int(dev["DeviceIndex"])] = {
            "name": dev.get("DeviceName", "toy"),
            "scalar": "ScalarCmd" in msgs,
        }

    async def level(self, strength: float) -> None:
        strength = max(0.0, min(1.0, strength))
        for idx, dev in self.devices.items():
            if dev["scalar"]:
                body = {"ScalarCmd": {"Id": self.next_id(), "DeviceIndex": idx, "Scalars": [{"Index": 0, "Scalar": strength, "ActuatorType": "Vibrate"}]}}
            else:
                body = {"VibrateCmd": {"Id": self.next_id(), "DeviceIndex": idx, "Speeds": [{"Index": 0, "Speed": strength}]}}
            await self.ws.send(json.dumps([body]))

    async def stop(self) -> None:
        for idx in self.devices:
            await self.ws.send(json.dumps([{"StopDeviceCmd": {"Id": self.next_id(), "DeviceIndex": idx}}]))


def lovense(url: str, strength: float) -> None:
    level = int(round(max(0.0, min(1.0, strength)) * 20))
    target = url.rstrip("/") + "/Vibrate?" + urllib.parse.urlencode({"v": level})
    with urllib.request.urlopen(target, timeout=1.5) as resp:
        resp.read()


class Engine:
    def __init__(self, app: "App") -> None:
        self.app = app
        self.running = False
        self.toy = None
        self.loop = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._thread, daemon=True).start()

    def stop(self) -> None:
        self.running = False

    def _thread(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._watch())

    async def _watch(self) -> None:
        cfg = self.app.snapshot()
        self.toy = None
        if not cfg["dry"] and cfg["backend"] == "intiface":
            self.toy = Intiface(cfg["intiface"])
            try:
                self.app.log(await self.toy.connect())
            except Exception as exc:
                self.app.log(f"Intiface: {exc}")
                self.toy = None
        last = None
        last_fire = 0.0
        samples: list[float] = []
        with mss.mss() as sct:
            while self.running:
                cfg = self.app.snapshot()
                try:
                    frame = grab(sct, cfg["monitor"], cfg["crop"])
                except Exception as exc:
                    self.app.log(f"Crop: {exc}")
                    await asyncio.sleep(0.4)
                    continue
                fill = fill_of(frame)
                samples.append(fill)
                samples = samples[-5:]
                smooth = float(np.median(samples))
                self.app.set_fill(smooth)
                if last is None:
                    last = smooth
                    await asyncio.sleep(0.05)
                    continue
                delta = smooth - last
                now = time.monotonic()
                if delta < -0.05:
                    self.app.log(f"level-up {delta:.3f}, ignored")
                    last = smooth
                elif delta >= cfg["boss_delta"] and now - last_fire >= cfg["cooldown"]:
                    self.app.log(f"boss +{delta:.3f}")
                    await self.pattern(cfg, cfg["boss_strength"], cfg["boss_seconds"], True)
                    last_fire = time.monotonic()
                    last = fill_of(grab(sct, cfg["monitor"], cfg["crop"]))
                    samples = [last]
                elif delta >= cfg["min_delta"] and now - last_fire >= cfg["cooldown"]:
                    self.app.log(f"trash +{delta:.3f}")
                    await self.pattern(cfg, cfg["trash_strength"], cfg["trash_seconds"], False)
                    last_fire = time.monotonic()
                    last = fill_of(grab(sct, cfg["monitor"], cfg["crop"]))
                    samples = [last]
                await asyncio.sleep(0.05)
        await self.halt()

    async def pattern(self, cfg: dict, strength: float, seconds: float, boss: bool) -> None:
        if cfg["dry"]:
            return
        if boss:
            await self._set(cfg, min(1.0, strength + 0.1))
            await asyncio.sleep(0.16)
            await self._set(cfg, strength * 0.3)
            await asyncio.sleep(0.1)
        await self._set(cfg, strength)
        await asyncio.sleep(seconds)
        await self.halt()

    async def _set(self, cfg: dict, strength: float) -> None:
        if self.toy:
            await self.toy.level(strength)
        elif cfg["backend"] == "lovense" and cfg["lovense"]:
            await asyncio.to_thread(lovense, cfg["lovense"], strength)

    async def halt(self) -> None:
        cfg = self.app.snapshot()
        if self.toy:
            await self.toy.stop()
        elif cfg["backend"] == "lovense" and cfg["lovense"]:
            try:
                await asyncio.to_thread(lovense, cfg["lovense"], 0)
            except Exception:
                pass


class App:
    def __init__(self) -> None:
        cfg = load_config()
        self.root = tk.Tk()
        self.root.title("D2R XP Pulse")
        self.root.configure(bg="#141414")
        self.root.geometry("460x640")
        self.engine = Engine(self)
        self.fill_var = tk.DoubleVar(value=0)
        crop = cfg.get("crop") or {"left": 700, "top": 1040, "width": 500, "height": 12}
        self.left = tk.IntVar(value=int(crop.get("left", 700)))
        self.top = tk.IntVar(value=int(crop.get("top", 1040)))
        self.width = tk.IntVar(value=int(crop.get("width", 500)))
        self.height = tk.IntVar(value=int(crop.get("height", 12)))
        self.monitor = tk.IntVar(value=int(cfg.get("monitor", 1)))
        self.backend = tk.StringVar(value=cfg.get("backend", "intiface"))
        self.intiface = tk.StringVar(value=cfg.get("intiface", "ws://127.0.0.1:12345"))
        self.lovense = tk.StringVar(value=cfg.get("lovense", "http://127.0.0.1:20010"))
        self.trash_s = tk.DoubleVar(value=float(cfg.get("trash_strength", 0.45)))
        self.trash_t = tk.DoubleVar(value=float(cfg.get("trash_seconds", 0.22)))
        self.boss_s = tk.DoubleVar(value=float(cfg.get("boss_strength", 0.7)))
        self.boss_t = tk.DoubleVar(value=float(cfg.get("boss_seconds", 1.4)))
        self.min_delta = tk.DoubleVar(value=float(cfg.get("min_delta", 0.004)))
        self.boss_delta = tk.DoubleVar(value=float(cfg.get("boss_delta", 0.02)))
        self.cooldown = tk.DoubleVar(value=float(cfg.get("cooldown", 0.12)))
        self.dry = tk.BooleanVar(value=bool(cfg.get("dry", True)))
        self._build()

    def _build(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#141414", foreground="#eee")
        style.configure("TButton", padding=6)
        style.configure("TCheckbutton", background="#141414", foreground="#eee")
        pad = {"padx": 10, "pady": 4}
        ttk.Label(self.root, text="D2R XP Pulse", font=("Segoe UI", 16)).pack(anchor="w", **pad)
        ttk.Label(self.root, text="Be in game, bar visible. Find bar, then Start.").pack(anchor="w", padx=10)
        self.status = ttk.Label(self.root, text="Idle. Dry run is on until you uncheck it.")
        self.status.pack(anchor="w", **pad)
        ttk.Progressbar(self.root, variable=self.fill_var, maximum=1).pack(fill="x", padx=10, pady=6)
        row = ttk.Frame(self.root)
        row.pack(fill="x", padx=10)
        ttk.Button(row, text="Find bar", command=self.find).pack(side="left", padx=2)
        ttk.Button(row, text="Preview", command=self.preview).pack(side="left", padx=2)
        ttk.Button(row, text="Test trash", command=lambda: self.test(False)).pack(side="left", padx=2)
        ttk.Button(row, text="Test boss", command=lambda: self.test(True)).pack(side="left", padx=2)
        crop = ttk.Frame(self.root)
        crop.pack(fill="x", padx=10, pady=6)
        for label, var in (("X", self.left), ("Y", self.top), ("W", self.width), ("H", self.height), ("Monitor", self.monitor)):
            ttk.Label(crop, text=label).pack(side="left")
            ttk.Entry(crop, textvariable=var, width=6).pack(side="left", padx=4)
        self._slider("Trash strength", self.trash_s, 0, 1)
        self._slider("Trash seconds", self.trash_t, 0.05, 1.5)
        self._slider("Boss strength", self.boss_s, 0, 1)
        self._slider("Boss seconds", self.boss_t, 0.2, 4)
        ttk.Label(self.root, text="Backend").pack(anchor="w", padx=10)
        ttk.Combobox(self.root, textvariable=self.backend, values=("intiface", "lovense"), width=12).pack(anchor="w", padx=10)
        ttk.Entry(self.root, textvariable=self.intiface).pack(fill="x", padx=10, pady=2)
        ttk.Entry(self.root, textvariable=self.lovense).pack(fill="x", padx=10, pady=2)
        ttk.Checkbutton(self.root, text="Dry run (print only)", variable=self.dry).pack(anchor="w", padx=10, pady=4)
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", padx=10, pady=6)
        ttk.Button(actions, text="Start", command=self.start).pack(side="left", padx=2)
        ttk.Button(actions, text="Stop motor", command=self.stop_motor).pack(side="left", padx=2)
        ttk.Button(actions, text="Save", command=self.save).pack(side="left", padx=2)
        self.log_box = tk.Text(self.root, height=10, bg="#1c1c1c", fg="#d8d2c6", insertbackground="#d8d2c6")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=8)

    def _slider(self, label: str, var: tk.DoubleVar, lo: float, hi: float) -> None:
        ttk.Label(self.root, text=label).pack(anchor="w", padx=10)
        ttk.Scale(self.root, from_=lo, to=hi, variable=var).pack(fill="x", padx=10)

    def log(self, text: str) -> None:
        def write() -> None:
            self.log_box.insert("end", text + "\n")
            self.log_box.see("end")
            self.status.configure(text=text)
        self.root.after(0, write)

    def set_fill(self, value: float) -> None:
        self.root.after(0, lambda: self.fill_var.set(max(0, min(1, value))))

    def crop(self) -> dict:
        return {"left": self.left.get(), "top": self.top.get(), "width": self.width.get(), "height": self.height.get()}

    def snapshot(self) -> dict:
        return {
            "crop": self.crop(),
            "monitor": self.monitor.get(),
            "backend": self.backend.get(),
            "intiface": self.intiface.get(),
            "lovense": self.lovense.get(),
            "trash_strength": self.trash_s.get(),
            "trash_seconds": self.trash_t.get(),
            "boss_strength": self.boss_s.get(),
            "boss_seconds": self.boss_t.get(),
            "min_delta": self.min_delta.get(),
            "boss_delta": self.boss_delta.get(),
            "cooldown": self.cooldown.get(),
            "dry": self.dry.get(),
        }

    def save(self) -> None:
        save_config(self.snapshot())
        self.log("Saved xp_pulse.json")

    def find(self) -> None:
        with mss.mss() as sct:
            found = find_bar(sct, self.monitor.get())
        if not found:
            self.log("No gold bar in the bottom of the screen. Be in game and try again.")
            return
        self.left.set(found["left"])
        self.top.set(found["top"])
        self.width.set(found["width"])
        self.height.set(found["height"])
        self.save()
        self.preview()
        self.log(f"Found bar at {found['left']},{found['top']} {found['width']}x{found['height']}")

    def preview(self) -> None:
        with mss.mss() as sct:
            frame = grab(sct, self.monitor.get(), self.crop())
        cv2.imwrite("xp_crop.png", frame)
        self.log(f"Wrote xp_crop.png  fill {fill_of(frame):.3f}")

    def test(self, boss: bool) -> None:
        cfg = self.snapshot()

        def go() -> None:
            loop = asyncio.new_event_loop()
            engine = Engine(self)
            engine.loop = loop
            if not cfg["dry"] and cfg["backend"] == "intiface":
                engine.toy = Intiface(cfg["intiface"])
                loop.run_until_complete(engine.toy.connect())
            strength = cfg["boss_strength"] if boss else cfg["trash_strength"]
            seconds = cfg["boss_seconds"] if boss else cfg["trash_seconds"]
            loop.run_until_complete(engine.pattern(cfg, strength, seconds, boss))
            self.log("test " + ("boss" if boss else "trash"))

        threading.Thread(target=go, daemon=True).start()

    def start(self) -> None:
        self.save()
        self.engine.start()
        self.log("Watching. Leave this window open.")

    def stop_motor(self) -> None:
        self.engine.stop()

        def go() -> None:
            loop = asyncio.new_event_loop()
            engine = Engine(self)
            cfg = self.snapshot()
            if cfg["backend"] == "lovense" and cfg["lovense"]:
                try:
                    lovense(cfg["lovense"], 0)
                except Exception:
                    pass
            self.log("Stopped")
            loop.close()

        threading.Thread(target=go, daemon=True).start()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
