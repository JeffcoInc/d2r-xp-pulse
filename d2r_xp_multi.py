"""Easy multi-toy orchestra for D2R XP Pulse, with a controlled stroker.

Vibrators take lead, pulse, and sustain. A linear device takes the stroker part.
Trash is one short stroke. A boss is two. A level-up is three. Depth is capped.

Connect the stroker in Intiface Central so it exposes LinearCmd.
python d2r_xp_multi.py
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
PARTS = ("lead", "pulse", "sustain")


def load_config() -> dict:
    if not CONFIG.exists():
        return {}
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def gold_fill(bgr: np.ndarray) -> float:
    mask = cv2.inRange(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV), (12, 70, 110), (42, 255, 255))
    if mask.shape[1] == 0:
        return 0.0
    need = max(1, int(mask.shape[0] * 0.3))
    hits = np.where((mask > 0).sum(axis=0) >= need)[0]
    if hits.size == 0:
        return float((mask > 0).mean())
    return float((int(hits.max()) + 1) / mask.shape[1])


def grab(sct, crop: dict) -> np.ndarray:
    return cv2.cvtColor(np.array(sct.grab(crop)), cv2.COLOR_BGRA2BGR)


class Intiface:
    def __init__(self, url: str) -> None:
        self.url = url
        self.ws = None
        self.msg_id = 1
        self.devices: dict[int, dict] = {}

    def next_id(self) -> int:
        self.msg_id += 1
        return self.msg_id

    async def connect(self) -> None:
        self.ws = await websockets.connect(self.url)
        await self.ws.send(json.dumps([{"RequestServerInfo": {"Id": self.next_id(), "ClientName": "d2r-xp-pulse", "MessageVersion": 3}}]))
        await asyncio.sleep(0.3)
        await self.ws.send(json.dumps([{"StartScanning": {"Id": self.next_id()}}]))
        await asyncio.sleep(2.0)
        await self.ws.send(json.dumps([{"RequestDeviceList": {"Id": self.next_id()}}]))
        await asyncio.sleep(0.5)
        try:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=0.5)
            for msg in json.loads(raw):
                if "DeviceList" in msg:
                    for dev in msg["DeviceList"].get("Devices", []):
                        self._remember(dev)
                if "DeviceAdded" in msg:
                    self._remember(msg["DeviceAdded"])
        except asyncio.TimeoutError:
            pass

    def _remember(self, dev: dict) -> None:
        msgs = dev.get("DeviceMessages", {})
        self.devices[int(dev["DeviceIndex"])] = {
            "name": dev.get("DeviceName", "toy"),
            "linear": "LinearCmd" in msgs,
        }

    async def set_one(self, idx: int, strength: float) -> None:
        body = {"ScalarCmd": {"Id": self.next_id(), "DeviceIndex": idx, "Scalars": [{"Index": 0, "Scalar": max(0.0, min(1.0, strength)), "ActuatorType": "Vibrate"}]}}
        await self.ws.send(json.dumps([body]))

    async def stroke(self, idx: int, position: float, duration_ms: int) -> None:
        body = {"LinearCmd": {"Id": self.next_id(), "DeviceIndex": idx, "Vectors": [{"Index": 0, "Duration": int(duration_ms), "Position": max(0.0, min(1.0, position))}]}}
        await self.ws.send(json.dumps([body]))

    async def stop(self, indexes: list[int]) -> None:
        for idx in indexes:
            await self.ws.send(json.dumps([{"StopDeviceCmd": {"Id": self.next_id(), "DeviceIndex": idx}}]))


def lovense(url: str, strength: float) -> None:
    if not url:
        return
    level = int(round(max(0.0, min(1.0, strength)) * 20))
    target = url.rstrip("/") + "/Vibrate?" + urllib.parse.urlencode({"v": level})
    with urllib.request.urlopen(target, timeout=1.5) as resp:
        resp.read()


def phrase(kind: str) -> list[tuple[str, float, float, float]]:
    if kind == "level":
        return [("lead", 0.0, 0.7, 0.4), ("pulse", 0.15, 0.55, 0.35), ("sustain", 0.0, 0.4, 1.4)]
    if kind == "boss":
        return [("lead", 0.0, 0.62, 0.25), ("pulse", 0.18, 0.5, 0.2), ("lead", 0.4, 0.7, 0.45), ("sustain", 0.0, 0.35, 1.1)]
    return [("lead", 0.0, 0.4, 0.16), ("pulse", 0.12, 0.28, 0.12)]


def stroke_plan(kind: str, depth: float) -> list[tuple[float, int]]:
    depth = max(0.2, min(1.0, depth))
    low = 0.5 - depth * 0.35
    high = 0.5 + depth * 0.35
    if kind == "level":
        return [(high, 500), (low, 500), (high, 600)]
    if kind == "boss":
        return [(high, 450), (low, 450)]
    return [(0.5 + depth * 0.2, 280)]


class App:
    def __init__(self) -> None:
        cfg = load_config()
        self.root = tk.Tk()
        self.root.title("D2R orchestra")
        self.root.configure(bg="#141414")
        self.root.geometry("480x680")
        self.link = None
        self.parts: dict[str, str] = {}
        self.running = False
        self.intiface = tk.StringVar(value=cfg.get("intiface", "ws://127.0.0.1:12345"))
        self.lovense = tk.StringVar(value=cfg.get("lovense", ""))
        self.dry = tk.BooleanVar(value=True)
        self.depth = tk.DoubleVar(value=float(cfg.get("stroke_depth", 0.55)))
        self.crop = cfg.get("crop") or {"left": 700, "top": 1040, "width": 500, "height": 12}
        self.status = tk.StringVar(value="1. Connect toys in Intiface.  2. Find toys.  3. Hear the phrase.  4. Start.")
        self._build()

    def _build(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#141414", foreground="#eee")
        style.configure("TCheckbutton", background="#141414", foreground="#eee")
        ttk.Label(self.root, text="Orchestra", font=("Segoe UI", 18)).pack(anchor="w", padx=12, pady=8)
        ttk.Label(self.root, textvariable=self.status, wraplength=440).pack(anchor="w", padx=12)
        ttk.Label(self.root, text="Lead hits the beat. Pulse answers. A stroker moves on its own part.", wraplength=440).pack(anchor="w", padx=12, pady=4)
        ttk.Button(self.root, text="Find my toys", command=self.scan).pack(anchor="w", padx=12, pady=6)
        self.list = ttk.Frame(self.root)
        self.list.pack(fill="x", padx=12, pady=4)
        ttk.Label(self.root, text="Stroke depth. 0.5 is a short controlled move. 1 is the full throw.").pack(anchor="w", padx=12)
        ttk.Scale(self.root, from_=0.25, to=1, variable=self.depth).pack(fill="x", padx=12)
        ttk.Label(self.root, text="Lovense URL, optional. It takes the next open vibrator part.").pack(anchor="w", padx=12)
        ttk.Entry(self.root, textvariable=self.lovense).pack(fill="x", padx=12, pady=2)
        ttk.Checkbutton(self.root, text="Dry run (no motion until you uncheck)", variable=self.dry).pack(anchor="w", padx=12, pady=6)
        row = ttk.Frame(self.root)
        row.pack(fill="x", padx=12)
        ttk.Button(row, text="Hear the phrase", command=self.test).pack(side="left", padx=2)
        ttk.Button(row, text="Start", command=self.start).pack(side="left", padx=2)
        ttk.Button(row, text="Stop", command=self.stop).pack(side="left", padx=2)
        self.log_box = tk.Text(self.root, height=12, bg="#1c1c1c", fg="#d8d2c6")
        self.log_box.pack(fill="both", expand=True, padx=12, pady=8)

    def log(self, text: str) -> None:
        def write() -> None:
            self.log_box.insert("end", text + "\n")
            self.log_box.see("end")
            self.status.set(text)
        self.root.after(0, write)

    def scan(self) -> None:
        def go() -> None:
            link = Intiface(self.intiface.get())
            try:
                asyncio.run(link.connect())
            except Exception as exc:
                self.log("Intiface is not running. Open Intiface Central, start the server, then try again. (%s)" % exc)
                return
            self.link = link
            self.root.after(0, lambda: self.assign(link.devices))

        threading.Thread(target=go, daemon=True).start()

    def assign(self, devices: dict[int, dict]) -> None:
        for child in self.list.winfo_children():
            child.destroy()
        self.parts = {}
        vibe_i = 0
        if not devices and not self.lovense.get().strip():
            ttk.Label(self.list, text="No toys yet. Connect one in Intiface and press Find my toys again.").pack(anchor="w")
            return
        for idx, dev in devices.items():
            if dev["linear"]:
                part = "stroker"
            else:
                part = PARTS[vibe_i % 3]
                vibe_i += 1
            self.parts[str(idx)] = part
            ttk.Label(self.list, text="%s  ->  %s" % (dev["name"], part)).pack(anchor="w", pady=2)
        if self.lovense.get().strip():
            part = PARTS[vibe_i % 3]
            self.parts["lovense"] = part
            ttk.Label(self.list, text="Lovense  ->  %s" % part).pack(anchor="w", pady=2)
        self.log("Assigned %d toys. A linear device is the stroker." % len(self.parts))

    def indexes(self, part: str) -> list[int]:
        return [int(idx) for idx, owned in self.parts.items() if owned == part and idx != "lovense"]

    def play(self, kind: str) -> None:
        notes = phrase(kind)
        strokes = stroke_plan(kind, self.depth.get())
        self.log("%s phrase, stroker depth %.2f" % (kind, self.depth.get()))
        if self.dry.get():
            return

        async def go() -> None:
            used: list[int] = []
            stroker_ids = self.indexes("stroker")

            async def move_stroker() -> None:
                for position, duration in strokes:
                    for idx in stroker_ids:
                        await self.link.stroke(idx, position, duration)
                    await asyncio.sleep(duration / 1000)
                if self.link:
                    await self.link.stop(stroker_ids)

            stroker_task = asyncio.create_task(move_stroker()) if self.link and stroker_ids else None
            for part, delay, strength, hold in notes:
                if delay:
                    await asyncio.sleep(delay)
                ids = self.indexes(part)
                used.extend(ids)
                if self.link:
                    for idx in ids:
                        await self.link.set_one(idx, strength)
                if self.parts.get("lovense") == part:
                    await asyncio.to_thread(lovense, self.lovense.get().strip(), strength)
                await asyncio.sleep(hold)
                if self.link:
                    await self.link.stop(ids)
                if self.parts.get("lovense") == part:
                    await asyncio.to_thread(lovense, self.lovense.get().strip(), 0)
            if stroker_task:
                await stroker_task
            if self.link:
                await self.link.stop(list(set(used + stroker_ids)))

        threading.Thread(target=lambda: asyncio.run(go()), daemon=True).start()

    def test(self) -> None:
        if not self.parts and self.link:
            self.assign(self.link.devices)
        self.play("boss")

    def start(self) -> None:
        if self.running:
            return
        if not self.parts:
            self.log("Find toys first.")
            return
        self.running = True
        threading.Thread(target=self.watch, daemon=True).start()
        self.log("Playing. Leave this window open.")

    def stop(self) -> None:
        self.running = False
        if self.link:
            ids = [int(i) for i in self.parts if i != "lovense"]

            def go() -> None:
                asyncio.run(self.link.stop(ids))

            threading.Thread(target=go, daemon=True).start()
        self.log("Stopped")

    def watch(self) -> None:
        last = None
        with mss.mss() as sct:
            while self.running:
                try:
                    fill = gold_fill(grab(sct, self.crop))
                except Exception as exc:
                    self.log("Crop missing. Run the main window once and Find bar. %s" % exc)
                    self.running = False
                    return
                if last is not None:
                    delta = fill - last
                    if delta < -0.05:
                        self.play("level")
                        last = fill
                    elif delta >= 0.02:
                        self.play("boss")
                        last = fill
                    elif delta >= 0.004:
                        self.play("trash")
                        last = fill
                else:
                    last = fill
                time.sleep(0.08)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
