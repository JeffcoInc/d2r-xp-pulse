# D2R XP Pulse

Hands-free toy feedback for Diablo II: Resurrected. When the experience bar goes up, a connected vibrator pulses. A small XP tick is a short burst. A large tick is a longer, stronger double-hit.

You play. You do not press a key for each kill.

## What it is

Diablo II: Resurrected pays experience when a monster dies in range. That includes your own kills, your mercenary, your summons, and party credit within range. The thin gold experience bar in the bottom HUD is the signal.

This script watches that bar the same way a screen recorder does. It captures pixels. It does not attach to `D2R.exe`, does not read game memory, and does not inject.

The fill is the right edge of the gold, not a raw pixel count, so a tiny tick still registers if it moves that edge. Three samples are median-filtered so one flicker does not fire.

| XP change | What you feel |
| --- | --- |
| Small rise | Short pulse, 45% for 0.22s |
| Large rise | Spike, dip, then 85% for 1.4s |
| Bar falls (level-up) | Ignored |

A large rise is usually a unique, a champion pack, an act boss, or a quest reward. The script cannot read the monster's name. Size of the XP jump is the split.

## Why XP, not the monster

- A hotkey works, and you cannot play while tapping it.
- Death sounds fire on your own swings until you tune them.
- Life bars in vanilla D2R only draw on the monster you are hovering or targeting.
- Reading process memory to get a real kill event is how Battle.net bans accounts. This repo does not do that.

XP is the game telling you that something died and you got credit.

## What it will also fire on

- Quest experience
- Picking up your corpse
- Party members killing within experience range

## What it will miss

- Level 99. The bar never moves.
- Spawns that pay no experience.
- A late-game character killing very low monsters, if the edge does not move a visible pixel. Lower `--min-delta` if trash stays silent.

## Requirements

- Windows. The capture uses the screen.
- Python 3.10+
- [Intiface Central](https://intiface.com/central/) and a Buttplug-supported toy, or the Lovense Remote app in Game Mode on the same Wi-Fi
- Diablo II: Resurrected in borderless or windowed mode, experience bar visible

```bash
pip install -r requirements.txt
```

## Run

Start Intiface Central, start its server, and connect the toy. Confirm it buzzes there before you launch the game.

First run finds the bar. Be in game, experience bar visible.

```bash
python d2r_xp_pulse.py --calibrate
```

That writes `xp_crop.png` (gold line, green outline) and saves the crop to `xp_pulse.json`. Check the image. If it is the bar, play:

```bash
python d2r_xp_pulse.py
```

Wrong monitor: `--monitor 2`, then calibrate again.

Dry run, no toy:

```bash
python d2r_xp_pulse.py --dry-run
```

Lovense Game Mode. Phone and PC on the same Wi-Fi. Discover, Game Mode, Enable LAN. HTTP port, usually 20010.

```bash
python d2r_xp_pulse.py --backend lovense --lovense-url http://192.168.1.40:20010
```

## Keys

| Key | Action |
| --- | --- |
| F9 | Play both patterns so you can feel the difference |
| F10 | Motors off. Listening continues. |
| Esc | Quit |

## Tuning

```bash
python d2r_xp_pulse.py --min-delta 0.002 --boss-delta 0.02 --boss-strength 0.7
```

- `--min-delta` is the smallest edge move that counts.
- `--boss-delta` is the move that switches to the long pattern.
- If the toy is a plug, start `--boss-strength` at `0.65`.

`xp_pulse.json` and `xp_crop.png` are local. They are gitignored.

## Battle.net

Screenshot capture is the same class of tool as OBS. It is not a memory reader. Blizzard's end-user license still applies to anything you run alongside the game. Do not add a process reader or an injector and take it online.

## License

MIT. See [LICENSE](LICENSE).
