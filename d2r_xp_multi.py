"""Several toys at once for D2R XP Pulse.

Connect every toy in Intiface, plus an optional Lovense URL.
Each toy can be Both, Trash, Boss, or Off.
A boss, jackpot, or level-up hits Boss and Both. Trash hits Trash and Both.

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


def load_config() -> dict:
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def gold_fill(bgr: np.ndarray) -> float:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (12, 70, 110), (42, 255, 255))
    if mask.shape[1] == 0:
        return 0.0
    need = max(1, int(mask.shape[0] * 0.3))
    hits = np.where((mask > 0).sum(axis=0) >= need)[0]
    if hits.size == 0:
        return float((mask > 0).mean())
    return float((int(hits.max()) + 1) / mask.shape[1])


def grab(sct, crop: dict) -> np.ndarray:
    frame = np.array(sct.grab(crop))
    return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)


class Intiface:
    def __init__(self, url: str) -> None:
        self.url = url
        self.ws = None
        self.msg_id = 1
        self.devices: dict[int, str] = {}

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
            raw = await asyncio.wait_for(self.ws.recv(), timeout=0.4)
            for msg in json.loads(raw):
                if "DeviceList" in msg:
                    for dev in msg["DeviceList"].get("Devices", []):
                        self.devices[int(dev["DeviceIndex"])] = dev.get("DeviceName", "toy")
                if "DeviceAdded" in msg:
                    dev = msg["DeviceAdded"]
                    self.devices[int(dev["DeviceIndex"])] = dev.get("DeviceName", "toy")
        except asyncio.TimeoutError:
            pass

    async def level(self, indexes: list[int], strength: float) -> None:
        strength = max(0.0, min(1.0, strength))
        for idx in indexes:
            body = {"ScalarCmd": {"Id": self.next_id(), "DeviceIndex": idx, "Scalars": [{"Index": 0, "Scalar": strength, "ActuatorType": "Vibrate"}]}}
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


class App:
    def __init__(self) -> None:
        cfg = load_config()
        self.root = tk.Tk()
        self.root.title("D2R XP Pulse — toys")
        self.root.configure(bg="#141414")
        self.root.geometry("480x640")
        self.link = None
        self.roles: dict[str, tk.StringVar] = {}
        self.running = False
        self.intiface = tk.StringVar(value=cfg.get("intiface", "ws://127.0.0.1:12345"))
        self.lovense = tk.StringVar(value=cfg.get("lovense", ""))
        self.lovense_role = tk.StringVar(value=cfg.get("lovense_role", "both"))
        self.dry = tk.BooleanVar(value=True)
        crop = cfg.get("crop") or {"left": 700, "top": 1040, "width": 500, "height": 12}
        self.crop = crop
        self._build()

    def _build(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#141414", foreground="#eee")
        style.configure("TCheckbutton", background="#141414", foreground="#eee")
        ttk.Label(self.root, text="Several toys", font=("Segoe UI", 16)).pack(anchor="w", padx=10, pady=8)
        ttk.Label(self.root, text="Scan, set a role on each, then Start. Both means every kill.").pack(anchor="w", padx=10)
        ttk.Entry(self.root, textvariable=self.intiface).pack(fill="x", padx=10, pady=4)
        ttk.Button(self.root, text="Scan Intiface", command=self.scan).pack(anchor="w", padx=10)
        self.list = ttk.Frame(self.root)
        self.list.pack(fill="x", padx=10, pady=8)
        ttk.Label(self.root, text="Lovense URL, optional second path").pack(anchor="w", padx=10)
        ttk.Entry(self.root, textvariable=self.lovense).pack(fill="x", padx=10)
        ttk.Combobox(self.root, textvariable=self.lovense_role, values=("off", "trash", "boss", "both"), state="readonly", width=10).pack(anchor="w", padx=10, pady=4)
        ttk.Checkbutton(self.root, text="Dry run", variable=self.dry).pack(anchor="w", padx=10)
        row = ttk.Frame(self.root)
        row.pack(fill="x", padx=10, pady=6)
        ttk.Button(row, text="Start", command=self.start).pack(side="left", padx=2)
        ttk.Button(row, text="Stop", command=self.stop).pack(side="left", padx=2)
        ttk.Button(row, text="Test all", command=self.test).pack(side="left", padx=2)
        self.log_box = tk.Text(self.root, height=12, bg="#1c1c1c", fg="#d8d2c6")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=8)

    def log(self, text: str) -> None:
        def write() -> None:
            self.log_box.insert("end", text + "\n")
            self.log_box.see("end")
        self.root.after(0, write)

    def scan(self) -> None:
        def go() -> None:
            link = Intiface(self.intiface.get())
            try:
                asyncio.run(link.connect())
            except Exception as exc:
                self.log("Intiface: %s" % exc)
                return
            self.link = link
            self.root.after(0, lambda: self.fill_list(link.devices))
            self.log("Found %d" % len(link.devices))

        threading.Thread(target=go, daemon=True).start()

    def fill_list(self, devices: dict[int, str]) -> None:
        for child in self.list.winfo_children():
            child.destroy()
        self.roles.clear()
        if not devices:
            ttk.Label(self.list, text="No toys. Connect them in Intiface, then scan again.").pack(anchor="w")
            return
        for idx, name in devices.items():
            var = tk.StringVar(value="both")
            self.roles[str(idx)] = var
            row = ttk.Frame(self.list)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text="%s  %s" % (idx, name), width=28).pack(side="left")
            ttk.Combobox(row, textvariable=var, values=("off", "trash", "boss", "both"), state="readonly", width=8).pack(side="left")

    def chosen(self, kind: str) -> list[int]:
        wanted = {"trash": {"trash", "both"}, "boss": {"boss", "both"}}[kind]
        return [int(idx) for idx, var in self.roles.items() if var.get() in wanted]

    def pulse(self, kind: str, strength: float, seconds: float) -> None:
        indexes = self.chosen(kind)
        use_lovense = self.lovense.get().strip() and self.lovense_role.get() in (kind, "both")
        self.log("%s -> toys %s%s" % (kind, indexes, " + lovense" if use_lovense else ""))
        if self.dry.get():
            return

        async def go() -> None:
            if self.link and indexes:
                await self.link.level(indexes, strength)
            if use_lovense:
                await asyncio.to_thread(lovense, self.lovense.get().strip(), strength)
            await asyncio.sleep(seconds)
            if self.link and indexes:
                await self.link.stop(indexes)
            if use_lovense:
                await asyncio.to_thread(lovense, self.lovense.get().strip(), 0)

        threading.Thread(target=lambda: asyncio.run(go()), daemon=True).start()

    def test(self) -> None:
        self.pulse("trash", 0.35, 0.3)
        self.root.after(500, lambda: self.pulse("boss", 0.6, 0.6))

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        threading.Thread(target=self.watch, daemon=True).start()
        self.log("Watching. Trash and boss can be different toys.")

    def stop(self) -> None:
        self.running = False
        self.log("Stopped")

    def watch(self) -> None:
        last = None
        with mss.mss() as sct:
            while self.running:
                frame = grab(sct, self.crop)
                fill = gold_fill(frame)
                if last is not None:
                    delta = fill - last
                    if delta < -0.05:
                        self.pulse("boss", 0.7, 1.4)
                        self.log("level celebration")
                        last = fill
                    elif delta >= 0.02:
                        self.pulse("boss", 0.65, 1.0)
                        last = fill
                    elif delta >= 0.004:
                        self.pulse("trash", 0.4, 0.22)
                        last = fill
                else:
                    last = fill
                time.sleep(0.08)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
