"""D2R XP Pulse window with pack ramp, ceiling, death cut, and profiles.

Screen capture only. Does not read the game process.
Run: python d2r_xp_window.py
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
    if not CONFIG.exists():
        return {}
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_config(data: dict) -> None:
    CONFIG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def gold_mask(bgr: np.ndarray) -> np.ndarray:
    return cv2.inRange(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV), GOLD_LO, GOLD_HI)


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


def masked(bgr: np.ndarray) -> np.ndarray:
    out = bgr.copy()
    out[gold_mask(bgr) > 0] = (40, 220, 40)
    return out


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


def death_screen(sct, monitor: int) -> bool:
    mon = sct.monitors[monitor]
    region = {
        "left": mon["left"] + mon["width"] // 3,
        "top": mon["top"] + mon["height"] // 3,
        "width": mon["width"] // 3,
        "height": mon["height"] // 5,
    }
    bgr = cv2.cvtColor(np.array(sct.grab(region)), cv2.COLOR_BGRA2BGR)
    b, g, r = [float(c.mean()) for c in cv2.split(bgr)]
    return r > 120 and r > g + 35 and r > b + 35


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
                elif "DeviceRemoved" in msg:
                    self.devices.pop(int(msg["DeviceRemoved"]["DeviceIndex"]), None)

    def _remember(self, dev: dict) -> None:
        msgs = dev.get("DeviceMessages", {})
        count = 1
        scalar = msgs.get("ScalarCmd", {})
        if isinstance(scalar, dict):
            count = int(scalar.get("FeatureCount") or scalar.get("ScalarCmdCount") or 1)
        self.devices[int(dev["DeviceIndex"])] = {"name": dev.get("DeviceName", "toy"), "scalar": "ScalarCmd" in msgs, "motors": max(1, count)}

    async def level(self, strength: float, both: bool) -> None:
        strength = max(0.0, min(1.0, strength))
        for idx, dev in self.devices.items():
            n = dev["motors"] if both else 1
            if dev["scalar"]:
                body = {"ScalarCmd": {"Id": self.next_id(), "DeviceIndex": idx, "Scalars": [{"Index": i, "Scalar": strength, "ActuatorType": "Vibrate"} for i in range(n)]}}
            else:
                body = {"VibrateCmd": {"Id": self.next_id(), "DeviceIndex": idx, "Speeds": [{"Index": 0, "Speed": strength}]}}
            await self.ws.send(json.dumps([body]))

    async def stop(self) -> None:
        for idx in list(self.devices):
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
        self.toy: Intiface | None = None
        self.pack: list[float] = []
        self.on_log: list[tuple[float, float]] = []

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        threading.Thread(target=lambda: asyncio.run(self._watch()), daemon=True).start()

    def stop(self) -> None:
        self.running = False

    def room(self, cfg: dict, seconds: float) -> float:
        now = time.monotonic()
        self.on_log = [(t, d) for t, d in self.on_log if now - t < 10]
        return max(0.0, min(seconds, cfg["max_on"] - sum(d for _, d in self.on_log)))

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
        reds = 0
        with mss.mss() as sct:
            while self.running:
                if self.app.paused:
                    await asyncio.sleep(0.1)
                    continue
                cfg = self.app.snapshot()
                try:
                    reds = reds + 1 if death_screen(sct, cfg["monitor"]) else 0
                    if reds >= 4:
                        self.app.log("Death screen. Motors off.")
                        self.running = False
                        break
                    frame = grab(sct, cfg["monitor"], cfg["crop"])
                except Exception as exc:
                    self.app.log(f"Crop: {exc}")
                    await asyncio.sleep(0.4)
                    continue
                self.app.show_frame(masked(frame))
                fill = fill_of(frame)
                samples = (samples + [fill])[-5:]
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
                elif delta >= cfg["min_delta"] and now - last_fire >= cfg["cooldown"]:
                    boss = delta >= cfg["boss_delta"]
                    if not (cfg["elites"] and not boss):
                        self.pack = [t for t in self.pack if now - t < 1.2] + [now]
                        ramp = min(1.45, 1 + 0.15 * (len(self.pack) - 1))
                        base = cfg["boss_strength"] if boss else cfg["trash_strength"]
                        strength = min(cfg["ceiling"], base * ramp)
                        seconds = self.room(cfg, cfg["boss_seconds"] if boss else cfg["trash_seconds"])
                        if seconds < 0.05:
                            self.app.log("on-time ceiling, skipped")
                        else:
                            self.app.count("boss" if boss else "trash")
                            self.app.log(f"{'boss' if boss else 'trash'} +{delta:.3f} x{ramp:.2f}")
                            self.on_log.append((now, seconds))
                            try:
                                await self.pattern(cfg, strength, seconds, boss or len(self.pack) >= 3)
                            except Exception as exc:
                                self.app.log(f"Toy dropped: {exc}. Paused.")
                                self.app.paused = True
                                break
                            last_fire = time.monotonic()
                    last = smooth
                await asyncio.sleep(0.05)
        await self.halt()
        self.app.log("Disarmed")

    async def pattern(self, cfg: dict, strength: float, seconds: float, both: bool) -> None:
        if cfg["dry"] or not self.running and seconds <= 0:
            return
        if both:
            await self._set(cfg, min(cfg["ceiling"], strength), True)
            await asyncio.sleep(0.14)
            await self._set(cfg, strength * 0.35, False)
            await asyncio.sleep(0.08)
        await self._set(cfg, strength, both)
        await asyncio.sleep(seconds)
        await self.halt()

    async def _set(self, cfg: dict, strength: float, both: bool) -> None:
        if self.toy:
            await self.toy.level(strength, both)
        elif cfg["backend"] == "lovense" and cfg["lovense"]:
            await asyncio.to_thread(lovense, cfg["lovense"], strength)

    async def halt(self) -> None:
        cfg = self.app.snapshot()
        if self.toy:
            try:
                await self.toy.stop()
            except Exception:
                self.app.log("Intiface link lost")
        elif cfg["backend"] == "lovense" and cfg["lovense"]:
            try:
                await asyncio.to_thread(lovense, cfg["lovense"], 0)
            except Exception:
                pass


class App:
    def __init__(self) -> None:
        raw = load_config()
        self.profiles = raw.get("profiles") or {"default": raw}
        name = raw.get("profile") or "default"
        cfg = self.profiles.get(name) or {}
        self.root = tk.Tk()
        self.root.title("D2R XP Pulse")
        self.root.configure(bg="#141414")
        self.root.geometry("500x820")
        self.engine = Engine(self)
        self.photo = None
        self.trash_n = 0
        self.boss_n = 0
        self.paused = False
        crop = cfg.get("crop") or {"left": 700, "top": 1040, "width": 500, "height": 12}
        self.profile = tk.StringVar(value=name)
        self.left = tk.IntVar(value=int(crop.get("left", 700)))
        self.top = tk.IntVar(value=int(crop.get("top", 1040)))
        self.width = tk.IntVar(value=int(crop.get("width", 500)))
        self.height = tk.IntVar(value=int(crop.get("height", 12)))
        self.monitor = tk.IntVar(value=int(cfg.get("monitor", 1)))
        self.backend = tk.StringVar(value=cfg.get("backend", "intiface"))
        self.intiface = tk.StringVar(value=cfg.get("intiface", "ws://127.0.0.1:12345"))
        self.lovense = tk.StringVar(value=cfg.get("lovense", "http://127.0.0.1:20010"))
        self.trash_s = tk.DoubleVar(value=float(cfg.get("trash_strength", 0.4)))
        self.trash_t = tk.DoubleVar(value=float(cfg.get("trash_seconds", 0.22)))
        self.boss_s = tk.DoubleVar(value=float(cfg.get("boss_strength", 0.65)))
        self.boss_t = tk.DoubleVar(value=float(cfg.get("boss_seconds", 1.2)))
        self.ceiling = tk.DoubleVar(value=float(cfg.get("ceiling", 0.75)))
        self.max_on = tk.DoubleVar(value=float(cfg.get("max_on", 3.0)))
        self.min_delta = tk.DoubleVar(value=float(cfg.get("min_delta", 0.004)))
        self.boss_delta = tk.DoubleVar(value=float(cfg.get("boss_delta", 0.02)))
        self.cooldown = tk.DoubleVar(value=float(cfg.get("cooldown", 0.12)))
        self.dry = tk.BooleanVar(value=True)
        self.elites = tk.BooleanVar(value=bool(cfg.get("elites", False)))
        self.ontop = tk.BooleanVar(value=False)
        self.fill_var = tk.DoubleVar(value=0)
        self._build()
        keyboard.Listener(on_press=self._key).start()

    def _key(self, key) -> None:
        if key == keyboard.Key.f10:
            self.root.after(0, self.stop_motor)
        elif key == keyboard.Key.f8:
            self.root.after(0, self.toggle_pause)

    def _build(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#141414", foreground="#eee")
        style.configure("TCheckbutton", background="#141414", foreground="#eee")
        ttk.Label(self.root, text="D2R XP Pulse", font=("Segoe UI", 16)).pack(anchor="w", padx=10, pady=4)
        self.status = ttk.Label(self.root, text="Dry run is on. Find the bar, then Start.")
        self.status.pack(anchor="w", padx=10)
        self.counts = ttk.Label(self.root, text="Trash 0    Boss 0")
        self.counts.pack(anchor="w", padx=10)
        prof = ttk.Frame(self.root)
        prof.pack(fill="x", padx=10, pady=4)
        ttk.Label(prof, text="Profile").pack(side="left")
        ttk.Entry(prof, textvariable=self.profile, width=16).pack(side="left", padx=4)
        ttk.Button(prof, text="Load", command=self.load_profile).pack(side="left")
        self.preview = ttk.Label(self.root)
        self.preview.pack(padx=10, pady=4)
        ttk.Progressbar(self.root, variable=self.fill_var, maximum=1).pack(fill="x", padx=10)
        row = ttk.Frame(self.root)
        row.pack(fill="x", padx=10, pady=4)
        for text, cmd in (("Find bar", self.find), ("Preview", self.preview_now), ("Test trash", lambda: self.test(False)), ("Test boss", lambda: self.test(True))):
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=2)
        crop = ttk.Frame(self.root)
        crop.pack(fill="x", padx=10)
        for label, var in (("X", self.left), ("Y", self.top), ("W", self.width), ("H", self.height), ("Monitor", self.monitor)):
            ttk.Label(crop, text=label).pack(side="left")
            ttk.Entry(crop, textvariable=var, width=6).pack(side="left", padx=3)
        self._slider("Trash", self.trash_s)
        self._slider("Boss", self.boss_s)
        self._slider("Ceiling", self.ceiling)
        ttk.Checkbutton(self.root, text="Elites only", variable=self.elites).pack(anchor="w", padx=10)
        ttk.Checkbutton(self.root, text="Dry run", variable=self.dry).pack(anchor="w", padx=10)
        ttk.Checkbutton(self.root, text="Keep on top", variable=self.ontop, command=self.top).pack(anchor="w", padx=10)
        ttk.Entry(self.root, textvariable=self.intiface).pack(fill="x", padx=10, pady=2)
        ttk.Entry(self.root, textvariable=self.lovense).pack(fill="x", padx=10, pady=2)
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", padx=10, pady=4)
        ttk.Button(actions, text="Start", command=self.start).pack(side="left", padx=2)
        ttk.Button(actions, text="Pause F8", command=self.toggle_pause).pack(side="left", padx=2)
        ttk.Button(actions, text="Stop F10", command=self.stop_motor).pack(side="left", padx=2)
        ttk.Button(actions, text="Save", command=self.save).pack(side="left", padx=2)
        self.log_box = tk.Text(self.root, height=7, bg="#1c1c1c", fg="#d8d2c6")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=6)

    def _slider(self, label: str, var: tk.DoubleVar) -> None:
        ttk.Label(self.root, text=label).pack(anchor="w", padx=10)
        ttk.Scale(self.root, from_=0.05, to=1, variable=var).pack(fill="x", padx=10)

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
        view = cv2.resize(frame, (440, 40), interpolation=cv2.INTER_NEAREST)
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
            "crop": self.crop(), "monitor": self.monitor.get(), "backend": self.backend.get(),
            "intiface": self.intiface.get(), "lovense": self.lovense.get(),
            "trash_strength": self.trash_s.get(), "trash_seconds": self.trash_t.get(),
            "boss_strength": self.boss_s.get(), "boss_seconds": self.boss_t.get(),
            "ceiling": self.ceiling.get(), "max_on": self.max_on.get(),
            "min_delta": self.min_delta.get(), "boss_delta": self.boss_delta.get(),
            "cooldown": self.cooldown.get(), "dry": self.dry.get(), "elites": self.elites.get(),
        }

    def save(self) -> None:
        name = self.profile.get().strip() or "default"
        self.profiles[name] = self.snapshot()
        save_config({"profile": name, "profiles": self.profiles})
        self.log(f"Saved profile {name}")

    def load_profile(self) -> None:
        cfg = self.profiles.get(self.profile.get().strip())
        if not cfg:
            self.log("No saved profile by that name")
            return
        crop = cfg.get("crop") or {}
        self.left.set(int(crop.get("left", self.left.get())))
        self.top.set(int(crop.get("top", self.top.get())))
        self.width.set(int(crop.get("width", self.width.get())))
        self.height.set(int(crop.get("height", self.height.get())))
        self.boss_s.set(float(cfg.get("boss_strength", 0.65)))
        self.ceiling.set(float(cfg.get("ceiling", 0.75)))
        self.elites.set(bool(cfg.get("elites", False)))
        self.log(f"Loaded {self.profile.get()}")

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
        self.preview_now()

    def preview_now(self) -> None:
        with mss.mss() as sct:
            frame = grab(sct, self.monitor.get(), self.crop())
        self.show_frame(masked(frame))
        self.log(f"fill {fill_of(frame):.3f}  green is the mask")

    def test(self, boss: bool) -> None:
        cfg = self.snapshot()

        def go() -> None:
            engine = Engine(self)
            engine.running = True
            if not cfg["dry"] and cfg["backend"] == "intiface":
                engine.toy = Intiface(cfg["intiface"])
                asyncio.run(engine.toy.connect())
            strength = min(cfg["ceiling"], cfg["boss_strength"] if boss else cfg["trash_strength"])
            asyncio.run(engine.pattern(cfg, strength, cfg["boss_seconds"] if boss else cfg["trash_seconds"], boss))
            self.log("test " + ("boss" if boss else "trash"))

        threading.Thread(target=go, daemon=True).start()

    def start(self) -> None:
        self.paused = False
        self.save()
        self.engine.start()
        self.log("Armed. F8 pauses. F10 stops.")

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.log("Paused" if self.paused else "Listening")

    def stop_motor(self) -> None:
        self.engine.stop()
        self.log("Stopped")

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
