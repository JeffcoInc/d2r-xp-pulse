"""Watch the D2R experience bar and pulse a toy. Screen capture only.

A small rise is a short pulse. A large rise is a longer double-hit.
A falling bar (level-up) is ignored. F9 tests both patterns. F10 stops the motor. Esc quits.

Does not open D2R.exe or read its memory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import cv2
import mss
import numpy as np
import websockets
from pynput import keyboard

CONFIG = Path("xp_pulse.json")
STOP = asyncio.Event()
QUIT = asyncio.Event()
TEST = asyncio.Event()


def parse_crop(text: str) -> dict:
    parts = [int(p.strip()) for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("crop must be x,y,width,height")
    x, y, w, h = parts
    if w < 8 or h < 2:
        raise argparse.ArgumentTypeError("crop is too small")
    return {"left": x, "top": y, "width": w, "height": h}


def load_config() -> dict:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    return {}


def save_config(data: dict) -> None:
    CONFIG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def gold_mask(bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (12, 70, 110), (42, 255, 255))


def gold_fill(bgr: np.ndarray) -> float:
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
    band = bgr[y_off:, :]
    mask = gold_mask(band)
    row_hits = (mask > 0).sum(axis=1)
    if row_hits.size == 0 or row_hits.max() < 40:
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


def region(sct, monitor: int, crop: dict) -> dict:
    origin = sct.monitors[monitor]
    return {
        "left": origin["left"] + int(crop["left"]),
        "top": origin["top"] + int(crop["top"]),
        "width": int(crop["width"]),
        "height": int(crop["height"]),
    }


def grab(sct, monitor: int, crop: dict) -> np.ndarray:
    frame = np.array(sct.grab(region(sct, monitor, crop)), dtype=np.uint8)
    return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)


class Median:
    def __init__(self, n: int = 3) -> None:
        self.samples: list[float] = []
        self.n = n

    def push(self, value: float) -> float:
        self.samples.append(value)
        self.samples = self.samples[-self.n :]
        return float(np.median(self.samples))


class Intiface:
    def __init__(self, url: str) -> None:
        self.url = url
        self.ws = None
        self.msg_id = 1
        self.devices: dict[int, dict] = {}
        self.reader = None

    def next_id(self) -> int:
        self.msg_id += 1
        return self.msg_id

    async def connect(self) -> None:
        self.ws = await websockets.connect(self.url)
        self.reader = asyncio.create_task(self._read())
        await self.send({"RequestServerInfo": {"Id": self.next_id(), "ClientName": "d2r-xp-pulse", "MessageVersion": 3}})
        await asyncio.sleep(0.3)
        await self.send({"StartScanning": {"Id": self.next_id()}})
        await asyncio.sleep(2.0)
        await self.send({"StopScanning": {"Id": self.next_id()}})
        await self.send({"RequestDeviceList": {"Id": self.next_id()}})
        await asyncio.sleep(0.4)
        if not self.devices:
            print("No toy yet. Connect it in Intiface Central, then restart.")
        else:
            for idx, dev in self.devices.items():
                print(f"Toy: [{idx}] {dev['name']}")

    async def send(self, message: dict) -> None:
        if self.ws is not None:
            await self.ws.send(json.dumps([message]))

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                for msg in payload:
                    if "DeviceAdded" in msg:
                        self._remember(msg["DeviceAdded"])
                    elif "DeviceList" in msg:
                        for dev in msg["DeviceList"].get("Devices", []):
                            self._remember(dev)
                    elif "Error" in msg:
                        print("Intiface:", msg["Error"].get("ErrorMessage"))
        except Exception as exc:
            print(f"Intiface link closed: {exc}")

    def _remember(self, dev: dict) -> None:
        msgs = dev.get("DeviceMessages", {})
        self.devices[int(dev["DeviceIndex"])] = {
            "name": dev.get("DeviceName", "toy"),
            "scalar": "ScalarCmd" in msgs,
            "vibrate": "VibrateCmd" in msgs,
        }

    async def vibrate(self, level: float) -> None:
        level = max(0.0, min(1.0, level))
        for idx, dev in list(self.devices.items()):
            if dev["scalar"] or not dev["vibrate"]:
                await self.send({"ScalarCmd": {"Id": self.next_id(), "DeviceIndex": idx, "Scalars": [{"Index": 0, "Scalar": level, "ActuatorType": "Vibrate"}]}})
            else:
                await self.send({"VibrateCmd": {"Id": self.next_id(), "DeviceIndex": idx, "Speeds": [{"Index": 0, "Speed": level}]}})

    async def stop(self) -> None:
        for idx in list(self.devices):
            await self.send({"StopDeviceCmd": {"Id": self.next_id(), "DeviceIndex": idx}})

    async def close(self) -> None:
        await self.stop()
        if self.reader:
            self.reader.cancel()
        if self.ws is not None:
            await self.ws.close()


def lovense_set(base: str, level: float) -> None:
    strength = int(round(max(0.0, min(1.0, level)) * 20))
    url = base.rstrip("/") + "/Vibrate?" + urllib.parse.urlencode({"v": strength})
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            resp.read()
    except Exception as exc:
        print(f"Lovense: {exc}")


async def pulse(toy, lovense, strength, seconds, boss, dry) -> None:
    label = "boss" if boss else "trash"
    print(f"[kill] {label}  {strength:.2f} for {seconds:.2f}s" + (" (dry)" if dry else ""))
    if dry:
        return
    if boss:
        if toy:
            await toy.vibrate(min(1.0, strength + 0.1))
        if lovense:
            await asyncio.to_thread(lovense_set, lovense, min(1.0, strength + 0.1))
        await asyncio.sleep(0.16)
        if STOP.is_set():
            return
        if toy:
            await toy.vibrate(strength * 0.3)
        if lovense:
            await asyncio.to_thread(lovense_set, lovense, strength * 0.3)
        await asyncio.sleep(0.1)
    if STOP.is_set():
        return
    if toy:
        await toy.vibrate(strength)
    if lovense:
        await asyncio.to_thread(lovense_set, lovense, strength)
    await asyncio.sleep(seconds)
    if toy:
        await toy.stop()
    if lovense:
        await asyncio.to_thread(lovense_set, lovense, 0.0)


def install_keys(loop):
    def on_press(key):
        if key == keyboard.Key.f9:
            loop.call_soon_threadsafe(TEST.set)
            print("[test] F9")
        elif key == keyboard.Key.f10:
            loop.call_soon_threadsafe(STOP.set)
            print("[stop] F10")
        elif key == keyboard.Key.esc:
            loop.call_soon_threadsafe(QUIT.set)
            loop.call_soon_threadsafe(STOP.set)

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    return listener


async def run(args) -> None:
    cfg = load_config()
    crop = args.crop or cfg.get("crop")
    with mss.mss() as sct:
        if args.monitor >= len(sct.monitors):
            raise SystemExit(f"No monitor {args.monitor}. This machine has {len(sct.monitors) - 1}.")
        print(f"Monitor {args.monitor}: {sct.monitors[args.monitor]}")
        if args.calibrate or not crop:
            found = find_bar(sct, args.monitor)
            if not found:
                raise SystemExit("Could not find a gold bar. Be in game, bar visible, then rerun --calibrate.")
            crop = found
            cfg["crop"] = crop
            cfg["monitor"] = args.monitor
            save_config(cfg)
            frame = grab(sct, args.monitor, crop)
            cv2.imwrite("xp_crop.png", frame)
            print(f"Found bar {crop}. Wrote xp_crop.png. Fill {gold_fill(frame):.3f}")
            print("If that image is the XP bar, run again with no flags.")
            return

        toy = None
        lovense = args.lovense_url if args.backend == "lovense" else None
        if args.backend == "intiface" and not args.dry_run:
            toy = Intiface(args.intiface)
            try:
                await toy.connect()
            except Exception as exc:
                print(f"Intiface not reachable ({exc}). Pulses will only print.")
                toy = None

        loop = asyncio.get_running_loop()
        keys = install_keys(loop)
        smoother = Median(3)
        last = smoother.push(gold_fill(grab(sct, args.monitor, crop)))
        last_fire = 0.0
        print(f"Watching {crop}  fill={last:.3f}  backend={args.backend}")
        print("Play. F9 tests both patterns. F10 stops the motor. Esc quits.")
        try:
            while not QUIT.is_set():
                if STOP.is_set():
                    if toy:
                        await toy.stop()
                    if lovense:
                        await asyncio.to_thread(lovense_set, lovense, 0.0)
                    STOP.clear()
                if TEST.is_set():
                    TEST.clear()
                    await pulse(toy, lovense, args.trash_strength, args.trash_seconds, False, args.dry_run)
                    await pulse(toy, lovense, args.boss_strength, args.boss_seconds, True, args.dry_run)
                fill = smoother.push(gold_fill(grab(sct, args.monitor, crop)))
                delta = fill - last
                now = time.monotonic()
                if delta > args.min_delta and (now - last_fire) >= args.cooldown:
                    boss = delta >= args.boss_delta
                    print(f"+ {delta:.4f}  fill {last:.3f} -> {fill:.3f}")
                    last_fire = now
                    await pulse(
                        toy,
                        lovense,
                        args.boss_strength if boss else args.trash_strength,
                        args.boss_seconds if boss else args.trash_seconds,
                        boss,
                        args.dry_run,
                    )
                    last = fill
                elif delta < -0.05:
                    print(f"[level] bar fell {delta:.3f}, ignored")
                    last = fill
                elif abs(delta) >= 0.001:
                    last = fill
                await asyncio.sleep(0.05)
        finally:
            keys.stop()
            if toy:
                await toy.close()
            if lovense:
                await asyncio.to_thread(lovense_set, lovense, 0.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pulse a toy when the D2R XP bar rises.")
    parser.add_argument("--crop", type=parse_crop, help="x,y,width,height of the XP bar")
    parser.add_argument("--monitor", type=int, default=1)
    parser.add_argument("--calibrate", action="store_true", help="find the gold bar and save xp_crop.png")
    parser.add_argument("--backend", choices=("intiface", "lovense"), default="intiface")
    parser.add_argument("--intiface", default="ws://127.0.0.1:12345")
    parser.add_argument("--lovense-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-delta", type=float, default=0.004)
    parser.add_argument("--boss-delta", type=float, default=0.02)
    parser.add_argument("--trash-strength", type=float, default=0.45)
    parser.add_argument("--trash-seconds", type=float, default=0.22)
    parser.add_argument("--boss-strength", type=float, default=0.7)
    parser.add_argument("--boss-seconds", type=float, default=1.4)
    parser.add_argument("--cooldown", type=float, default=0.12)
    args = parser.parse_args()
    if args.backend == "lovense" and not args.lovense_url:
        parser.error("--lovense-url is required with --backend lovense")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
