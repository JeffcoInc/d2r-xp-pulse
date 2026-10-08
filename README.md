# D2R XP Pulse

Hands-free toy feedback for Diablo II: Resurrected. When the experience bar goes up, a connected vibrator pulses. A small XP tick is a short burst. A large tick is a longer, stronger burst.

You play. You do not press a key for each kill.

## What it is

Diablo II: Resurrected pays experience when a monster dies in range. That includes your own kills, your mercenary, your summons, and party credit within range. The thin gold experience bar in the bottom HUD is the signal.

This script watches that bar the same way a screen recorder does. It captures pixels. It does not attach to `D2R.exe`, does not read game memory, and does not inject.

| XP change | What you feel |
| --- | --- |
| Small rise | Short pulse, about 45% for 0.22s |
| Large rise | Spike, dip, then about 85% for 1.4s |
| Bar resets (level-up) | Ignored |

A large rise is usually a unique, a champion pack, an act boss, or a quest reward. The script cannot read the monster's name. Size of the XP jump is the split.

## Why XP, not the monster

Other signals were tried and are worse for hands-free play.

- A hotkey works, and you cannot play while tapping it.
- Death sounds fire on your own swings until you tune them, and they cannot tell a Fallen from Andariel reliably.
- Life bars in vanilla D2R only draw on the monster you are hovering or targeting. An AoE clear does not show a bar per corpse.
- Reading process memory to get a real kill event is how Battle.net bans accounts. This repo does not do that.

XP is the game telling you that something died and you got credit.

## What it will also fire on

- Quest experience
- Picking up your corpse
- Party members killing within experience range

## What it will miss

- Level 99. The bar never moves.
- Spawns that pay no experience: Baal's appendages, some eggs, nests, and similar objects.
- A late-game character killing very low monsters. One Fallen may not move a visible pixel. Lower `--min-delta`, or accept that those ticks are invisible to a screenshot.

## Requirements

- Windows. The capture uses the screen.
- Python 3.10+
- [Intiface Central](https://intiface.com/central/) and a Buttplug-supported toy, or the Lovense Remote app in Game Mode on the same Wi-Fi
- Diablo II: Resurrected in borderless or windowed mode, experience bar visible

```bash
pip install -r requirements.txt
```

## Run

Start Intiface Central, start its server, and connect the toy there first. Confirm the toy buzzes inside Intiface before you launch the game.

Calibrate the crop. This writes `xp_crop.png` and does not touch the toy.

```bash
python d2r_xp_pulse.py --calibrate
```

The image should be the gold experience bar and almost nothing else. Then pass that rectangle as `x,y,width,height` in screen pixels. A 1920x1080 bottom-center guess:

```bash
python d2r_xp_pulse.py --crop 700,1040,500,12
```

Kill one Fallen. The console should print a small increase and `[kill] trash`. Kill a unique or an act boss and it should print `[kill] boss`.

Lovense Game Mode instead of Intiface. Phone and PC on the same Wi-Fi. In the Lovense Remote app: Discover, Game Mode, Enable LAN. Use the HTTP port, usually 20010.

```bash
python d2r_xp_pulse.py --backend lovense --lovense-url http://192.168.1.40:20010 --crop 700,1040,500,12
```

Wrong monitor:

```bash
python d2r_xp_pulse.py --monitor 2 --crop 700,1040,500,12
```

## Keys

| Key | Action |
| --- | --- |
| F10 | Motors off. Listening continues. |
| Esc | Quit |

No key is required to play.

## Tuning

```bash
python d2r_xp_pulse.py --crop 700,1040,500,12 --min-delta 0.001 --boss-delta 0.02 --boss-strength 0.7
```

- `--min-delta` is the smallest fill increase that counts. Lower it if trash kills are silent.
- `--boss-delta` is the increase that switches to the long pattern. Lower it if uniques feel like trash.
- `--trash-strength` / `--trash-seconds` and `--boss-strength` / `--boss-seconds` shape the two patterns.
- If the toy is a plug, start `--boss-strength` at `0.65`. Full strength internally is a lot.
- `--cooldown` is the minimum gap between trash pulses, so a dense pack ticks instead of holding the motor open.

## Battle.net

Screenshot capture is the same class of tool as OBS. It is not a memory reader. Blizzard's end-user license still applies to anything you run alongside the game. Do not add a process reader, an injector, or offset scanning to this and then take it online. That path gets accounts banned.

Single-player mods that draw always-on health bars are a separate install. This script does not need them.

## Layout

- `d2r_xp_pulse.py` — the tool. Experience bar in, pulse out.
- `requirements.txt` — Python dependencies.

## License

MIT. See [LICENSE](LICENSE).
