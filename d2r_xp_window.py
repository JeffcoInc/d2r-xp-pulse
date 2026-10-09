"""D2R XP Pulse window.

Streaks, a near-level tease, a level-up celebration, and a jackpot pattern.
Screen capture only. Does not read the game process.

Run: python d2r_xp_window.py
F8 pauses. F10 stops and writes the session card.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import ttk

import cv2
import mss
import numpy as np
import websockets

CONFIG = Path("xp_pulse.json")
PATTERNS = ("pulse", "wave", "stairs", "earthquake")


def load_config() -> dict:
    if not CONFIG.exists():
        return {}
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_config(data: dict) -> None:
    CONFIG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def gold_mask(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (12, 70, 110), (42, 255, 255))


def fill_of(bgr) -> float:
    mask = gold_mask(bgr)
    cols = mask.shape[1]
    if cols == 0:
        return 0.0
    need = max(1, int(mask.shape[0] * 0.3))
    hits = np.where((mask > 0).sum(axis=0) >= need)[0]
    if hits.size == 0:
        return float((mask > 0).mean())
    return float((int(hits.max()) + 1) / cols)


def masked(bgr):
    out = bgr.copy()
    out[gold_mask(bgr) > 0] = (40, 220, 40)
    return out


def find_bar(sct, monitor: int):
    mon = sct.monitors[monitor]
    bgr = cv2.cvtColor(np.array(sct.grab(mon)), cv2.COLOR_BGRA2BGR)
    height, width = bgr.shape[:2]
    y_off = int(height * 0.72)
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
    return {"left": max(0, int(xs.min()) - pad), "top": y_off + max(0, top - pad), "width": min(width, int(xs.max() - xs.min()) + pad * 2), "height": max(8, bottom - top + pad * 2)}


def grab(sct, monitor: int, crop: dict):
    origin = sct.monitors[monitor]
    region = {"left": origin["left"] + int(crop["left"]), "top": origin["top"] + int(crop["top"]), "width": max(8, int(crop["width"])), "height": max(4, int(crop["height"]))}
    return cv2.cvtColor(np.array(sct.grab(region)), cv2.COLOR_BGRA2BGR)


def death_screen(sct, monitor: int) -> bool:
    mon = sct.monitors[monitor]
    region = {"left": mon["left"] + mon["width"] // 3, "top": mon["top"] + mon["height"] // 3, "width": mon["width"] // 3, "height": mon["height"] // 5}
    bgr = cv2.cvtColor(np.array(sct.grab(region)), cv2.COLOR_BGRA2BGR)
    blue, green, red = [float(c.mean()) for c in cv2.split(bgr)]
    return red > 120 and red > green + 35 and red > blue + 35


def looks_like_town(sct, monitor: int) -> bool:
    mon = sct.monitors[monitor]
    region = {"left": mon["left"] + mon["width"] // 4, "top": mon["top"] + int(mon["height"] * 0.35), "width": mon["width"] // 2, "height": mon["height"] // 5}
    hsv = cv2.cvtColor(cv2.cvtColor(np.array(sct.grab(region)), cv2.COLOR_BGRA2BGR), cv2.COLOR_BGR2HSV)
    stone = ((hsv[:, :, 0] < 25) & (hsv[:, :, 1] < 80) & (hsv[:, :, 2] > 70)).mean()
    return float(stone) > 0.55


class Intiface:
    def __init__(self, url: str) -> None:
        self.url = url
        self.ws = None
        self.msg_id = 1
        self.devices = {}

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
        self.devices[int(dev["DeviceIndex"])] = {"name": dev.get("DeviceName", "toy"), "scalar": "ScalarCmd" in msgs, "oscillate": "OscillateCmd" in msgs}

    async def level(self, strength: float, oscillate: bool) -> None:
        strength = max(0.0, min(1.0, strength))
        for idx, dev in self.devices.items():
            if dev["scalar"]:
                body = {"ScalarCmd": {"Id": self.next_id(), "DeviceIndex": idx, "Scalars": [{"Index": 0, "Scalar": strength, "ActuatorType": "Vibrate"}]}}
            else:
                body = {"VibrateCmd": {"Id": self.next_id(), "DeviceIndex": idx, "Speeds": [{"Index": 0, "Speed": strength}]}}
            await self.ws.send(json.dumps([body]))
            if oscillate and dev["oscillate"]:
                await self.ws.send(json.dumps([{"OscillateCmd": {"Id": self.next_id(), "DeviceIndex": idx, "Speeds": [{"Index": 0, "Speed": strength}]}}]))

    async def stop(self) -> None:
        for idx in list(self.devices):
            await self.ws.send(json.dumps([{"StopDeviceCmd": {"Id": self.next_id(), "DeviceIndex": idx}}]))


def lovense(url: str, strength: float) -> None:
    level = int(round(max(0.0, min(1.0, strength)) * 20))
    body = json.dumps({"command": "Function", "action": f"Vibrate:{level}", "timeSec": 2, "apiVer": 1}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/command", data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=1.5) as resp:
        resp.read()


def steps_for(name: str, strength: float, seconds: float):
    strength = max(0.05, min(1.0, strength))
    if name == "wave":
        return [(strength * 0.35, seconds * 0.25), (strength, seconds * 0.4), (strength * 0.4, seconds * 0.35)]
    if name == "stairs":
        part = seconds / 3
        return [(strength * 0.4, part), (strength * 0.7, part), (strength, part)]
    if name == "earthquake":
        return [(strength, 0.12), (strength * 0.2, 0.08), (strength, 0.12), (0.0, max(0.05, seconds - 0.32))]
    return [(strength, seconds)]


class Engine:
    def __init__(self, app) -> None:
        self.app = app
        self.running = False
        self.toy = None
        self.pack = []
        self.on_log = []
        self.streak = 0
        self.best = 0
        self.last_tease = 0.0

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.streak = 0
        threading.Thread(target=lambda: asyncio.run(self._watch()), daemon=True).start()

    def stop(self) -> None:
        self.running = False

    def room(self, cfg, seconds: float) -> float:
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
        last_move = time.monotonic()
        samples = []
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
                now = time.monotonic()
                if last is None:
                    last = smooth
                    await asyncio.sleep(0.05)
                    continue
                delta = smooth - last
                if abs(delta) >= 0.004:
                    last_move = now
                if now - last_move > 2.5 and self.streak:
                    self.streak = 0
                    self.app.set_streak(0)
                town = cfg["town"] and now - last_move > 8 and looks_like_town(sct, cfg["monitor"])
                if delta < -0.05:
                    self.app.log("Level up.")
                    self.app.count("level")
                    await self.play(cfg, "earthquake", min(cfg["ceiling"], cfg["boss_strength"] + 0.1), 1.6, True)
                    last = smooth
                    self.streak = 0
                elif not town and delta >= cfg["min_delta"] and now - last_fire >= cfg["cooldown"]:
                    kind, pattern, seconds, base = "trash", cfg["trash_pattern"], cfg["trash_seconds"], cfg["trash_strength"]
                    if delta >= cfg["jackpot_delta"]:
                        kind, pattern, seconds, base = "jackpot", "earthquake", cfg["boss_seconds"] * 1.4, cfg["boss_strength"]
                    elif delta >= cfg["boss_delta"]:
                        kind, pattern, seconds, base = "boss", cfg["boss_pattern"], cfg["boss_seconds"], cfg["boss_strength"]
                    if not (cfg["elites"] and kind == "trash"):
                        self.pack = [t for t in self.pack if now - t < 2.5] + [now]
                        self.streak = len(self.pack)
                        self.best = max(self.best, self.streak)
                        self.app.set_streak(self.streak)
                        if self.streak >= 10:
                            pattern, seconds = "earthquake", max(seconds, 0.9)
                        elif self.streak >= 3 and pattern == "pulse":
                            pattern = "wave"
                        strength = min(cfg["ceiling"], base * min(1.45, 1 + 0.08 * (self.streak - 1)))
                        granted = self.room(cfg, seconds)
                        if granted < 0.05:
                            self.app.log("on-time ceiling, skipped")
                        else:
                            self.app.count(kind)
                            self.app.log(f"{kind} +{delta:.3f} streak {self.streak} {pattern}")
                            self.on_log.append((now, granted))
                            await self.play(cfg, pattern, strength, granted, kind != "trash")
                            last_fire = time.monotonic()
                    last = smooth
                elif not town and smooth >= 0.9 and now - self.last_tease > 8 and now - last_fire > 3:
                    self.last_tease = now
                    self.app.log("near level")
                    await self.play(cfg, "stairs", min(cfg["ceiling"], 0.35), 0.6, False)
                await asyncio.sleep(0.05)
        await self.halt()
        self.app.session(self.best)

    async def play(self, cfg, pattern: str, strength: float, seconds: float, oscillate: bool) -> None:
        if cfg["dry"]:
            return
        for level, hold in steps_for(pattern, strength, seconds):
            await self._set(cfg, level, oscillate and level > 0.5)
            await asyncio.sleep(hold)
        await self.halt()

    async def _set(self, cfg, strength: float, oscillate: bool) -> None:
        if self.toy:
            await self.toy.level(strength, oscillate)
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
        cfg = load_config()
        self.root = tk.Tk()
        self.root.title("D2R XP Pulse")
        self.root.configure(bg="#141414")
        self.root.geometry("540x860")
        self.engine = Engine(self)
        self.photo = None
        self.counts = {"trash": 0, "boss": 0, "jackpot": 0, "level": 0}
        self.paused = False
        self.armed_at = 0.0
        crop = cfg.get("crop") or {"left": 700, "top": 1040, "width": 500, "height": 12}
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
        self.trash_pattern = tk.StringVar(value=cfg.get("trash_pattern", "pulse"))
        self.boss_pattern = tk.StringVar(value=cfg.get("boss_pattern", "stairs"))
        self.dry = tk.BooleanVar(value=True)
        self.elites = tk.BooleanVar(value=bool(cfg.get("elites", False)))
        self.town = tk.BooleanVar(value=True)
        self.fill_var = tk.DoubleVar(value=0)
        self.streak_var = tk.StringVar(value="Streak 0")
        self.band = None
        self.drag = None
        self._build()
        self.root.bind("<F8>", lambda _e: self.toggle_pause())
        self.root.bind("<F10>", lambda _e: self.stop_motor())

    def _build(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#141414", foreground="#eee")
        style.configure("TCheckbutton", background="#141414", foreground="#eee")
        ttk.Label(self.root, text="D2R XP Pulse", font=("Segoe UI", 16)).pack(anchor="w", padx=10, pady=4)
        self.status = ttk.Label(self.root, text="Dry run is on. Grab the bar, then Start.")
        self.status.pack(anchor="w", padx=10)
        self.score = ttk.Label(self.root, text="Trash 0   Boss 0   Jackpot 0   Level 0")
        self.score.pack(anchor="w", padx=10)
        ttk.Label(self.root, textvariable=self.streak_var).pack(anchor="w", padx=10)
        ttk.Progressbar(self.root, variable=self.fill_var, maximum=1).pack(fill="x", padx=10, pady=4)
        self.canvas = tk.Canvas(self.root, height=90, bg="#000", highlightthickness=0)
        self.canvas.pack(fill="x", padx=10, pady=4)
        self.canvas.bind("<ButtonPress-1>", self._down)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._up)
        row = ttk.Frame(self.root)
        row.pack(fill="x", padx=10)
        ttk.Button(row, text="Find bar", command=self.find).pack(side="left", padx=2)
        ttk.Button(row, text="Grab screen", command=self.grab_screen).pack(side="left", padx=2)
        ttk.Button(row, text="Test", command=self.test).pack(side="left", padx=2)
        ttk.Label(self.root, text="Trash pattern").pack(anchor="w", padx=10)
        ttk.Combobox(self.root, textvariable=self.trash_pattern, values=PATTERNS, width=14).pack(anchor="w", padx=10)
        ttk.Label(self.root, text="Boss pattern").pack(anchor="w", padx=10)
        ttk.Combobox(self.root, textvariable=self.boss_pattern, values=PATTERNS, width=14).pack(anchor="w", padx=10)
        self._slider("Trash", self.trash_s)
        self._slider("Boss", self.boss_s)
        self._slider("Ceiling", self.ceiling)
        ttk.Checkbutton(self.root, text="Elites only", variable=self.elites).pack(anchor="w", padx=10)
        ttk.Checkbutton(self.root, text="Town silence", variable=self.town).pack(anchor="w", padx=10)
        ttk.Checkbutton(self.root, text="Dry run", variable=self.dry).pack(anchor="w", padx=10)
        ttk.Entry(self.root, textvariable=self.intiface).pack(fill="x", padx=10, pady=2)
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", padx=10, pady=4)
        ttk.Button(actions, text="Start", command=self.start).pack(side="left", padx=2)
        ttk.Button(actions, text="Pause F8", command=self.toggle_pause).pack(side="left", padx=2)
        ttk.Button(actions, text="Stop F10", command=self.stop_motor).pack(side="left", padx=2)
        self.log_box = tk.Text(self.root, height=7, bg="#1c1c1c", fg="#d8d2c6")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=6)

    def _slider(self, label: str, var) -> None:
        ttk.Label(self.root, text=label).pack(anchor="w", padx=10)
        ttk.Scale(self.root, from_=0.05, to=1, variable=var).pack(fill="x", padx=10)

    def log(self, text: str) -> None:
        def write() -> None:
            self.log_box.insert("end", text + "\n")
            self.log_box.see("end")
            self.status.configure(text=text)
        self.root.after(0, write)

    def count(self, kind: str) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        text = f"Trash {self.counts['trash']}   Boss {self.counts['boss']}   Jackpot {self.counts['jackpot']}   Level {self.counts['level']}"
        self.root.after(0, lambda: self.score.configure(text=text))

    def set_streak(self, value: int) -> None:
        self.root.after(0, lambda: self.streak_var.set(f"Streak {value}"))

    def set_fill(self, value: float) -> None:
        self.root.after(0, lambda: self.fill_var.set(max(0, min(1, value))))

    def show_frame(self, frame) -> None:
        shown = cv2.resize(frame, (500, 28), interpolation=cv2.INTER_NEAREST)
        ok, buf = cv2.imencode(".ppm", cv2.cvtColor(shown, cv2.COLOR_BGR2RGB))
        if not ok:
            return
        raw = buf.tobytes()
        def paint() -> None:
            self.photo = tk.PhotoImage(data=raw)
            self.canvas.delete("shot")
            self.canvas.create_image(0, 60, image=self.photo, anchor="nw", tags="shot")
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
            "trash_pattern": self.trash_pattern.get(), "boss_pattern": self.boss_pattern.get(),
            "min_delta": 0.004, "boss_delta": 0.02, "jackpot_delta": 0.06, "cooldown": 0.16,
            "elites": self.elites.get(), "town": self.town.get(), "dry": self.dry.get(),
        }

    def save(self) -> None:
        data = self.snapshot()
        data["last_session"] = self.counts
        save_config(data)
        self.log("Saved xp_pulse.json")

    def find(self) -> None:
        with mss.mss() as sct:
            found = find_bar(sct, self.monitor.get())
        if not found:
            self.log("No gold bar. Be in game, or Grab screen and drag.")
            return
        self.left.set(found["left"])
        self.top.set(found["top"])
        self.width.set(found["width"])
        self.height.set(found["height"])
        self.save()
        self.log("Found bar.")

    def grab_screen(self) -> None:
        with mss.mss() as sct:
            mon = sct.monitors[self.monitor.get()]
            band = {"left": mon["left"], "top": mon["top"] + int(mon["height"] * 0.78), "width": mon["width"], "height": int(mon["height"] * 0.22)}
            frame = cv2.cvtColor(np.array(sct.grab(band)), cv2.COLOR_BGRA2BGR)
        self.band = {"origin": (band["left"], band["top"]), "size": (frame.shape[1], frame.shape[0])}
        shown = cv2.resize(frame, (500, 80), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".ppm", cv2.cvtColor(shown, cv2.COLOR_BGR2RGB))
        if not ok:
            return
        self.photo = tk.PhotoImage(data=buf.tobytes())
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.log("Drag a box around the gold bar.")

    def _down(self, event) -> None:
        self.drag = (event.x, event.y, event.x, event.y)

    def _move(self, event) -> None:
        if not self.drag:
            return
        self.drag = (self.drag[0], self.drag[1], event.x, event.y)
        self.canvas.delete("box")
        self.canvas.create_rectangle(*self.drag, outline="#c6a15a", width=2, tags="box")

    def _up(self, event) -> None:
        if not self.drag or not self.band:
            return
        x0, y0, x1, y1 = self.drag
        left, right = sorted((x0, x1))
        top, bottom = sorted((y0, y1))
        if right - left < 8:
            return
        sx = self.band["size"][0] / 500
        sy = self.band["size"][1] / 80
        self.left.set(int(self.band["origin"][0] + left * sx))
        self.top.set(int(self.band["origin"][1] + top * sy))
        self.width.set(max(8, int((right - left) * sx)))
        self.height.set(max(4, int((bottom - top) * sy)))
        self.save()
        self.log("Crop saved from the drag box.")

    def test(self) -> None:
        cfg = self.snapshot()
        threading.Thread(target=lambda: asyncio.run(self.engine.play(cfg, cfg["boss_pattern"], cfg["boss_strength"], cfg["boss_seconds"], True)), daemon=True).start()
        self.log("test " + cfg["boss_pattern"])

    def start(self) -> None:
        self.save()
        self.armed_at = time.monotonic()
        self.engine.start()
        self.log("Watching. F8 pauses. F10 writes the session.")

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.log("paused" if self.paused else "listening")

    def stop_motor(self) -> None:
        self.engine.stop()
        self.log("Stopping")

    def session(self, best: int) -> None:
        elapsed = int(time.monotonic() - self.armed_at) if self.armed_at else 0
        card = {**self.counts, "best_streak": best, "seconds": elapsed}
        data = load_config()
        data["last_session"] = card
        save_config(data)
        self.log(f"Session  trash {card['trash']}  boss {card['boss']}  jackpot {card['jackpot']}  level {card['level']}  streak {best}  {elapsed}s")

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
