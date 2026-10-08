"""Window for D2R XP Pulse.

Find the gold bar, see the crop in the window, count kills, pulse a toy.
Screen capture only. Does not read the game process.
"""

from __future__ import annotations

import asyncio
import base64
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
from pynput import keyboard

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
    bgr = cv2.cvtColor(np.array(sct.grab(mon)), cv2.COLOR_BGRA2BGR)
    h, w = bgr.shape[:2]
    y_off = int(h * 0.72)
    mask = gold_mask(bgr[y_off:, :])
    row_hits = (mask > 0).sum(axis=1)
    if row_hits.size == 0 or int(row_hits.max()) < 40:
        return None
    ys = np.where(row_hits > row_hits.max() * 0.45)[0]
    top, bottom = int(ys.min()), int(ys.max())
    xs = np.where((mask[top : bottom + 1] > 0).sum(axis=0) > 0)[0]
    if xs.size < 20:
        return None
    pad = 4
    return {
        "left": max(0, int(xs.min()) - pad),
        "top": y_off + max(0, top - pad),
        "width": min(w, int(xs.max() - xs.min()) + pad * 2),
        "height": max(8, bottom - top + pad * 2),
    }


def grab(sct, monitor: int, crop: dict) -> np.ndarray:
    origin = sct.monitors[monitor]
    region = {
        "left": origin["left"] + int(crop["left"]),
        "top": origin["top"] + int(crop["top"]),
        "width": max(8, int(crop["width"])),
        "height": max(4, int(crop["height"])),
    }
    return cv2.cvtColor(np.array(sct.grab(region)), cv2.COLOR_BGRA2BGR)


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
        self.devices[int(dev["DeviceIndex"])] = {"name": dev.get("DeviceName", "toy"), "scalar": "ScalarCmd" in msgs}

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
        self.stop_now = False

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.stop_now = False
        threading.Thread(target=self._thread, daemon=True).start()

    def stop(self) -> None:
        self.stop_now = True
        self.running = False

    def _thread(self) -> None:
        asyncio.run(self._watch())

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
                if self.stop_now:
                    break
                cfg = self.app.snapshot()
                try:
                    frame = grab(sct, cfg["monitor"], cfg["crop"])
                except Exception as exc:
                    self.app.log(f"Crop: {exc}")
                    await asyncio.sleep(0.4)
                    continue
                self.app.show_frame(frame)
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
                    self.app.count("boss")
                    self.app.log(f"boss +{delta:.3f}")
                    await self.pattern(cfg, cfg["boss_strength"], cfg["boss_seconds"], True)
                    last_fire = time.monotonic()
                    last = smooth
                    samples = [last]
                elif delta >= cfg["min_delta"] and now - last_fire >= cfg["cooldown"]:
                    self.app.count("trash")
                    self.app.log(f"trash +{delta:.3f}")
                    await self.pattern(cfg, cfg["trash_strength"], cfg["trash_seconds"], False)
                    last_fire = time.monotonic()
                    last = smooth
                    samples = [last]
                await asyncio.sleep(0.05)
        await self.halt()

    async def pattern(self, cfg: dict, strength: float, seconds: float, boss: bool) -> None:
        if cfg["dry"] or self.stop_now:
            return
        if boss:
            await self._set(cfg, min(1.0, strength + 0.08))
            await asyncio.sleep(0.15)
            await self._set(cfg, strength * 0.35)
            await asyncio.sleep(0.1)
        if self.stop_now:
            await self.halt()
            return
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
            try:
                await self.toy.stop()
            except Exception:
                pass
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
        self.root.geometry("480x760")
        self.engine = Engine(self)
        self.photo = None
        self.trash_n = 0
        self.boss_n = 0
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
        self.boss_s = tk.DoubleVar(value=float(cfg.get("boss_strength", 0.65)))
        self.boss_t = tk.DoubleVar(value=float(cfg.get("boss_seconds", 1.4)))
        self.min_delta = tk.DoubleVar(value=float(cfg.get("min_delta", 0.004)))
        self.boss_delta = tk.DoubleVar(value=float(cfg.get("boss_delta", 0.02)))
        self.cooldown = tk.DoubleVar(value=float(cfg.get("cooldown", 0.12)))
        self.dry = tk.BooleanVar(value=bool(cfg.get("dry", True)))
        self.ontop = tk.BooleanVar(value=False)
        self.fill_var = tk.DoubleVar(value=0)
        self._build()
        self.boss_s.trace_add("write", lambda *_: self.warn())
        keyboard.Listener(on_press=self._key).start()
        self.warn()

    def _key(self, key) -> None:
        if key == keyboard.Key.f10:
            self.root.after(0, self.stop_motor)

    def _build(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#141414", foreground="#eee")
        style.configure("TCheckbutton", background="#141414", foreground="#eee")
        ttk.Label(self.root, text="D2R XP Pulse", font=("Segoe UI", 16)).pack(anchor="w", padx=10, pady=6)
        self.status = ttk.Label(self.root, text="Dry run is on. Find the bar, then Start.")
        self.status.pack(anchor="w", padx=10)
        self.counts = ttk.Label(self.root, text="Trash 0    Boss 0")
        self.counts.pack(anchor="w", padx=10)
        self.preview = ttk.Label(self.root)
        self.preview.pack(padx=10, pady=6)
        ttk.Progressbar(self.root, variable=self.fill_var, maximum=1).pack(fill="x", padx=10)
        row = ttk.Frame(self.root)
        row.pack(fill="x", padx=10, pady=6)
        for text, cmd in (("Find bar", self.find), ("Preview", self.preview_now), ("Test trash", lambda: self.test(False)), ("Test boss", lambda: self.test(True))):
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=2)
        crop = ttk.Frame(self.root)
        crop.pack(fill="x", padx=10)
        for label, var in (("X", self.left), ("Y", self.top), ("W", self.width), ("H", self.height), ("Monitor", self.monitor)):
            ttk.Label(crop, text=label).pack(side="left")
            ttk.Entry(crop, textvariable=var, width=6).pack(side="left", padx=3)
        nudge = ttk.Frame(self.root)
        nudge.pack(fill="x", padx=10, pady=4)
        ttk.Button(nudge, text="Up", command=lambda: self.nudge(0, -4)).pack(side="left")
        ttk.Button(nudge, text="Down", command=lambda: self.nudge(0, 4)).pack(side="left")
        ttk.Button(nudge, text="Taller", command=lambda: self.nudge_h(4)).pack(side="left")
        self._slider("Trash strength", self.trash_s, 0, 1)
        self._slider("Trash seconds", self.trash_t, 0.05, 1.5)
        self._slider("Boss strength", self.boss_s, 0, 1)
        self._slider("Boss seconds", self.boss_t, 0.2, 4)
        self.warn_label = ttk.Label(self.root, text="")
        self.warn_label.pack(anchor="w", padx=10)
        ttk.Combobox(self.root, textvariable=self.backend, values=("intiface", "lovense"), width=12).pack(anchor="w", padx=10)
        ttk.Entry(self.root, textvariable=self.intiface).pack(fill="x", padx=10, pady=2)
        ttk.Entry(self.root, textvariable=self.lovense).pack(fill="x", padx=10, pady=2)
        ttk.Checkbutton(self.root, text="Dry run (print only)", variable=self.dry).pack(anchor="w", padx=10)
        ttk.Checkbutton(self.root, text="Keep window on top", variable=self.ontop, command=self.top).pack(anchor="w", padx=10)
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", padx=10, pady=6)
        ttk.Button(actions, text="Start", command=self.start).pack(side="left", padx=2)
        ttk.Button(actions, text="Stop (F10)", command=self.stop_motor).pack(side="left", padx=2)
        ttk.Button(actions, text="Save", command=self.save).pack(side="left", padx=2)
        self.log_box = tk.Text(self.root, height=8, bg="#1c1c1c", fg="#d8d2c6")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=8)

    def _slider(self, label: str, var: tk.DoubleVar, lo: float, hi: float) -> None:
        ttk.Label(self.root, text=label).pack(anchor="w", padx=10)
        ttk.Scale(self.root, from_=lo, to=hi, variable=var).pack(fill="x", padx=10)

    def warn(self) -> None:
        if self.boss_s.get() > 0.75:
            self.warn_label.configure(text="Boss strength is high for a plug. 0.65 is the safer start.")
        else:
            self.warn_label.configure(text="")

    def top(self) -> None:
        self.root.attributes("-topmost", self.ontop.get())

    def log(self, text: str) -> None:
        def write() -> None:
            self.log_box.insert("end", text + "\n")
            self.log_box.see("end")
            self.status.configure(text=text)
        self.root.after(0, write)

    def count(self, kind: str) -> None:
        if kind == "boss":
            self.boss_n += 1
        else:
            self.trash_n += 1
        self.root.after(0, lambda: self.counts.configure(text=f"Trash {self.trash_n}    Boss {self.boss_n}"))

    def set_fill(self, value: float) -> None:
        self.root.after(0, lambda: self.fill_var.set(max(0, min(1, value))))

    def show_frame(self, frame: np.ndarray) -> None:
        view = cv2.resize(frame, (440, 36), interpolation=cv2.INTER_NEAREST)
        ok, buf = cv2.imencode(".png", view)
        if not ok:
            return
        data = base64.b64encode(buf.tobytes()).decode("ascii")

        def paint() -> None:
            self.photo = tk.PhotoImage(data=data)
            self.preview.configure(image=self.photo)

        self.root.after(0, paint)

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

    def nudge(self, dx: int, dy: int) -> None:
        self.left.set(self.left.get() + dx)
        self.top.set(self.top.get() + dy)
        self.preview_now()

    def nudge_h(self, dh: int) -> None:
        self.height.set(max(4, self.height.get() + dh))
        self.preview_now()

    def find(self) -> None:
        with mss.mss() as sct:
            found = find_bar(sct, self.monitor.get())
        if not found:
            self.log("No gold bar. Be in game and try again.")
            return
        self.left.set(found["left"])
        self.top.set(found["top"])
        self.width.set(found["width"])
        self.height.set(found["height"])
        self.save()
        self.preview_now()

    def preview_now(self) -> None:
        with mss.mss() as sct:
            frame = grab(sct, self.monitor.get(), self.crop())
        cv2.imwrite("xp_crop.png", frame)
        self.show_frame(frame)
        self.log(f"fill {fill_of(frame):.3f}")

    def test(self, boss: bool) -> None:
        cfg = self.snapshot()

        def go() -> None:
            engine = Engine(self)
            loop = asyncio.new_event_loop()
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
        self.log("Watching. F10 stops the motor.")

    def stop_motor(self) -> None:
        self.engine.stop()
        self.log("Stopped")

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
