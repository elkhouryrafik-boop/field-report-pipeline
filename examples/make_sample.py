#!/usr/bin/env python
"""make_sample.py - build the fully synthetic WhatsApp-style site walk used by the demo and the tests.

    python examples/make_sample.py --out examples/sample --seed 7
        [--format android|ios] [--tts auto|pyttsx3|none] [--multilingual]
        [--profile site-inspection|damage-assessment] [--voice-dir examples/sample_voice]
        [--render-voice-dir DIR]        # maintainer: render TTS into DIR and stop

Everything is invented: a 2030 date, coordinates around 0N 0E (open ocean), a fictional site, two
"Demo/Sample" people, a watermark on every photo. For a given seed the chat, images and sidecars are
byte-identical between runs (TTS audio bytes depend on the local speech engine, so `--tts none`
copies the pre-rendered set from `examples/sample_voice/`).

Needs only the standard library, Pillow and PyAV (`av`, installed with faster-whisper); `pyttsx3` is
optional and only used by `--tts auto|pyttsx3`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

GENERATOR_VERSION = "1.0.0"
DAY = datetime(2030, 1, 14)
CHAT_NAME = "WhatsApp Chat with Demo Site Walk.txt"
SENDERS = ("Alex Demo", "Sam Sample")
SITE = "Northwind Training Depot (fictional)"
LOC_PREFIX = "location: https://maps.google.com/" "?q="
SIZE = (1280, 960)

# ---------------------------------------------------------------------------------------------
# Scripts. One entry per stop: photos (scene, finding label, category, rating) and voice notes.
# Rating = priority (site-inspection) or severity (damage-assessment).
# ---------------------------------------------------------------------------------------------
PROFILES = {
    "site-inspection": {
        "rating_key": "priority",
        "stops": [
            {"photos": [("wall_crack", "Diagonal wall crack", "Structure", "High"),
                        ("wall_crack", "Diagonal wall crack, second angle", "Structure", "High"),
                        ("ceiling_stain", "Ceiling water stain", "Envelope", "Medium")],
             "notes": ["Stop one, main hall north wall. A diagonal crack runs from the window corner up to "
                       "the ceiling, about two metres long. Priority high.",
                       "Stop one, ceiling above the hall. Water stain about one metre across near the light "
                       "fitting, probably a roof leak. Priority medium."]},
            {"photos": [("stair_handrail", "Missing stair handrail", "Access", "High"),
                        ("stair_handrail", "Missing stair handrail, landing", "Access", "High"),
                        ("ceiling_stain", "Stair ceiling stain", "Envelope", "Low")],
             "notes": ["Stop two, east stair. Handrail loose at the first landing, about one metre missing. "
                       "Priority high.",
                       "Stop two, east stair ceiling. Small damp patch, finish is flaking. Priority low."]},
            {"photos": [("blocked_door", "Blocked fire exit", "Fire & life safety", "High"),
                        ("blocked_door", "Blocked fire exit, inside view", "Fire & life safety", "High")],
             "notes": ["Stop three, west corridor. The fire exit door is blocked by stacked pallets and the "
                       "exit sign is dark. Priority high."]},
            {"photos": [("exposed_cable", "Exposed cable", "MEP", "High"),
                        ("exposed_cable", "Exposed cable at junction box", "MEP", "Medium")],
             "notes": ["Stop four, plant room. Cable hanging loose below the junction box, no conduit, "
                       "three metres of it. Priority high.",
                       "Stop four, plant room floor. Cable tray cover missing at one point. Priority medium."]},
            {"photos": [("puddle", "Standing water on floor", "Finishes", "Low"),
                        ("puddle", "Standing water, wider view", "Finishes", "Low")],
             "notes": ["Stop five, loading bay. Standing water on the floor near the drain, about two square "
                       "metres, the floor finish is lifting. Priority low."]},
        ],
    },
    "damage-assessment": {
        "rating_key": "severity",
        "stops": [
            {"photos": [("wall_crack", "Wall crack about 5 mm wide", "Structure", "Moderate"),
                        ("wall_crack", "Wall crack about 12 mm wide", "Structure", "Severe"),
                        ("wall_crack", "Hairline crack at plinth", "Structure", "Minor")],
             "notes": ["Stop one, north gable wall. Diagonal crack about twelve millimetres wide at the "
                       "widest point, running two metres. Severity severe.",
                       "Stop one, plinth. Hairline cracking only, under one millimetre. Severity minor."]},
            {"photos": [("roof_sheeting", "Roof sheeting missing", "Roof", "Severe"),
                        ("roof_sheeting", "Roof sheeting missing, east slope", "Roof", "Severe"),
                        ("ceiling_stain", "Ceiling stain below roof opening", "Roof", "Moderate")],
             "notes": ["Stop two, main roof. About six sheets of roof sheeting are missing on the east slope. "
                       "Severity severe.",
                       "Stop two, underside of the roof. Water stain on the ceiling below the opening. "
                       "Severity moderate."]},
            {"photos": [("door_frame", "Door frame distorted", "Openings", "Moderate"),
                        ("door_frame", "Door frame distorted, leaf jammed", "Openings", "Moderate")],
             "notes": ["Stop three, store room. The door frame is distorted and the leaf no longer closes. "
                       "Severity moderate."]},
            {"photos": [("exposed_cable", "Exposed cable", "Services", "Minor"),
                        ("exposed_cable", "Exposed cable at panel", "Services", "Minor")],
             "notes": ["Stop four, services bay. Cable hanging free below the panel, two metres of it. "
                       "Severity minor.",
                       "Stop four, services bay. Panel cover is missing but the board is intact. "
                       "Severity minor."]},
            {"photos": [("rubble", "Rubble across access way", "Access", "Moderate"),
                        ("fence_gap", "Perimeter fence gap", "External works", "Minor")],
             "notes": ["Stop five, rear yard. Rubble blocks half of the access way and a four metre length of "
                       "fence is down. Severity moderate."]},
        ],
    },
}

# Non-English renderings of voice note 6 and 7 (flat note index, 1-based) for --multilingual.
# Same facts as the English note so the sidecar can carry the English text as well.
MULTILINGUAL = {
    "site-inspection": {
        6: ("fr", "Arrêt quatre, local technique. Un câble pend sous la boîte de jonction, sans gaine, "
                  "sur trois mètres. Priorité haute."),
        7: ("es", "Parada cuatro, suelo del cuarto técnico. Falta la tapa de la bandeja de cables en un "
                  "punto. Prioridad media."),
    },
    "damage-assessment": {
        6: ("fr", "Arrêt quatre, local technique. Un câble pend sous le tableau, deux mètres. "
                  "Gravité mineure."),
        7: ("es", "Parada cuatro, zona de servicios. Falta la tapa del cuadro pero el cuadro está intacto. "
                  "Gravedad menor."),
    },
}


def flat_notes(profile: str) -> list[tuple[int, str]]:
    out = []
    for si, stop in enumerate(PROFILES[profile]["stops"], 1):
        for t in stop["notes"]:
            out.append((si, t))
    return out


# ---------------------------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------------------------
def build_events(profile: str, rng: random.Random) -> list[dict]:
    """The ordered 40-message script. Media filenames are assigned here."""
    stops = PROFILES[profile]["stops"]
    ev: list[dict] = []
    ev.append({"k": "text", "s": SENDERS[0], "t": "Starting the site walk now. Stop one is the main hall."})
    ev.append({"k": "text", "s": SENDERS[1], "t": "Ok, I have the camera. Voice notes first, then photos."})
    ev.append({"k": "text", "s": SENDERS[0], "t": "Sharing a pin for each stop, a dot closes the stop."})
    photo_n = note_n = 0
    for si, stop in enumerate(stops, 1):
        sender = SENDERS[(si - 1) % 2]
        photos, notes = list(stop["photos"]), list(stop["notes"])
        # interleave: photo, pin(if any), notes and remaining photos
        seq: list[dict] = []
        for idx, ph in enumerate(photos):
            photo_n += 1
            seq.append({"k": "photo", "s": sender, "n": photo_n, "stop": si, "idx": idx + 1,
                        "of": len(photos), "meta": ph})
            if idx == 0 and si <= 4:
                seq.append({"k": "pin", "s": sender, "stop": si})
            if idx < len(notes):
                note_n += 1
                seq.append({"k": "audio", "s": sender, "n": note_n, "stop": si, "text": notes[idx]})
        for idx in range(len(photos), len(notes)):  # more notes than photos (none today)
            note_n += 1
            seq.append({"k": "audio", "s": sender, "n": note_n, "stop": si, "text": notes[idx]})
        ev.extend(seq)
        if si == 2:
            ev.append({"k": "omitted", "s": SENDERS[1]})
            ev.append({"k": "text", "s": SENDERS[0], "t": "Stair lighting is on but the bulb at the landing is dim.\n"
                       "Will note it in the report if it is not a one-off."})
        if si == 3:
            ev.append({"k": "text", "s": SENDERS[1], "t": "Door is wedged shut from the inside as well. "
                       "Checked twice. <This message was edited>"})
        if si == 4:
            ev.append({"k": "text", "s": SENDERS[0], "t": "This message was deleted"})
        if si < 5:
            ev.append({"k": "text", "s": sender, "t": "."})
            if si >= 2:
                ev.append({"k": "text", "s": SENDERS[si % 2], "t": f"Moving on to stop {si + 1}."})
    ev.append({"k": "text", "s": SENDERS[1], "t": "That is all five stops. Heading back to the office."})
    ev.append({"k": "text", "s": SENDERS[0], "t": "Great, I will upload everything tonight."})
    return ev


def schedule(n: int, rng: random.Random) -> list[datetime]:
    """n timestamps from 09:02:xx to 11:10:xx; minute gaps drawn from the rng."""
    total_min = (11 * 60 + 10) - (9 * 60 + 2)
    gaps = [total_min // (n - 1)] * (n - 1)
    rest = total_min - sum(gaps)
    for _ in range(rest):
        gaps[rng.randrange(len(gaps))] += 1
    # shuffle weight: move a minute between neighbours so spacing is irregular but total is fixed
    for _ in range(n):
        a, b = rng.randrange(len(gaps)), rng.randrange(len(gaps))
        if gaps[a] > 1 and a != b:
            gaps[a] -= 1
            gaps[b] += 1
    t = DAY.replace(hour=9, minute=2)
    out = [t]
    for g in gaps:
        t = t + timedelta(minutes=g)
        out.append(t)
    return [x.replace(second=rng.randrange(60)) for x in out]


def pins(rng: random.Random) -> dict[int, tuple[float, float]]:
    base = {1: (0.0012, 0.0021), 2: (0.0018, 0.0014), 3: (0.0007, 0.0033), 4: (0.0028, 0.0024)}
    out = {}
    for k, (la, ln) in base.items():
        la2 = round(la + rng.choice((-2, -1, 0, 1, 2)) * 0.0001, 4)
        ln2 = round(ln + rng.choice((-2, -1, 0, 1, 2)) * 0.0001, 4)
        out[k] = (la2, ln2)
        d_m = math.hypot(la2, ln2) * 111_320
        assert d_m < 500, d_m
    return out


def fmt_line(fmt: str, ts: datetime, sender: str, body: str, attach: bool = False) -> str:
    if fmt == "ios":
        lrm = "‎" if attach else ""
        return f"[{ts:%d/%m/%Y, %H:%M:%S}] {sender}: {lrm}{body}"
    hour = ts.hour % 12 or 12
    ampm = "am" if ts.hour < 12 else "pm"
    return f"{ts:%d/%m/%Y}, {hour}:{ts:%M} {ampm} - {sender}: {body}"


def render_chat(fmt: str, events: list[dict], times: list[datetime], pin_xy) -> str:
    lines = []
    for e, ts in zip(events, times):
        k = e["k"]
        if k == "text":
            lines.append(fmt_line(fmt, ts, e["s"], e["t"]))
        elif k == "photo":
            fn = e["file"]
            lines.append(fmt_line(fmt, ts, e["s"], f"<attached: {fn}>" if fmt == "ios"
                                  else f"{fn} (file attached)", attach=True))
        elif k == "audio":
            fn = e["file"]
            lines.append(fmt_line(fmt, ts, e["s"], f"<attached: {fn}>" if fmt == "ios"
                                  else f"{fn} (file attached)", attach=True))
        elif k == "pin":
            la, ln = pin_xy[e["stop"]]
            lines.append(fmt_line(fmt, ts, e["s"], f"{LOC_PREFIX}{la},{ln}"))
        elif k == "omitted":
            lines.append(fmt_line(fmt, ts, e["s"], "<Media omitted>"))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# Images (Pillow only)
# ---------------------------------------------------------------------------------------------
def _font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _gradient(draw, box, c1, c2):
    x0, y0, x1, y1 = box
    for y in range(y0, y1):
        f = (y - y0) / max(1, y1 - y0 - 1)
        draw.line([(x0, y), (x1, y)], fill=tuple(int(a + (b - a) * f) for a, b in zip(c1, c2)))


def scene(kind: str, rng: random.Random):
    from PIL import Image, ImageDraw
    w, h = SIZE
    img = Image.new("RGB", SIZE, (150, 150, 145))
    d = ImageDraw.Draw(img)
    j = lambda a, b: rng.randint(a, b)  # noqa: E731
    if kind == "wall_crack":
        _gradient(d, (0, 0, w, h), (178, 170, 158), (150, 143, 132))
        d.rectangle((0, h - 140, w, h), fill=(104, 98, 92))
        x0, y0 = j(100, 300), j(60, 160)
        pts = [(x0, y0)]
        for _ in range(9):
            x, y = pts[-1]
            pts.append((x + j(40, 120), y + j(50, 90)))
        d.line(pts, fill=(52, 48, 45), width=j(4, 8), joint="curve")
        d.line([(x + 3, y) for x, y in pts], fill=(120, 114, 106), width=2)
        d.rectangle((w - 300, 120, w - 80, 380), outline=(80, 90, 100), width=10, fill=(150, 175, 195))
    elif kind == "stair_handrail":
        _gradient(d, (0, 0, w, h), (170, 168, 160), (140, 138, 130))
        for i in range(7):
            y = 420 + i * 70
            d.polygon([(120 + i * 90, y), (w - 120, y), (w - 120, y + 70), (120 + i * 90, y + 70)],
                      fill=(120 - i * 4, 116 - i * 4, 110 - i * 4), outline=(70, 68, 64))
        d.line([(140, 350), (620, 840)], fill=(60, 64, 70), width=14)  # rail up to the gap
        d.line([(860, 840), (1160, 910)], fill=(60, 64, 70), width=14)
        for x in (150, 260, 370, 480, 590):
            d.line([(x, 360 + (x - 140) * 1.0), (x, 440 + (x - 140) * 1.0)], fill=(60, 64, 70), width=8)
        d.text((660, 780), "gap", fill=(200, 60, 50), font=_font(30))
    elif kind == "ceiling_stain":
        _gradient(d, (0, 0, w, h), (226, 224, 218), (206, 204, 196))
        cx, cy = j(450, 800), j(300, 520)
        for r, c in ((260, (204, 190, 160)), (200, (190, 172, 138)), (130, (170, 148, 108))):
            d.ellipse((cx - r, cy - r * 0.7, cx + r, cy + r * 0.7), fill=c)
        d.rectangle((cx - 60, 60, cx + 60, 100), fill=(250, 250, 240), outline=(120, 120, 110), width=4)
    elif kind == "blocked_door":
        _gradient(d, (0, 0, w, h), (168, 166, 156), (140, 138, 130))
        d.rectangle((430, 120, 850, 840), fill=(70, 100, 78), outline=(40, 52, 44), width=10)
        d.rectangle((480, 40, 800, 100), fill=(30, 36, 32), outline=(90, 90, 90), width=4)  # dark exit sign
        d.rectangle((760, 470, 800, 500), fill=(200, 200, 190))
        for i in range(4):
            y = 660 - i * 90
            d.rectangle((330 + j(-20, 20), y, 960 + j(-20, 20), y + 80), fill=(170, 130, 82),
                        outline=(100, 76, 46), width=4)
        d.rectangle((0, 840, w, h), fill=(110, 106, 100))
    elif kind == "exposed_cable":
        _gradient(d, (0, 0, w, h), (136, 140, 144), (112, 116, 120))
        d.rectangle((420, 100, 860, 360), fill=(176, 178, 170), outline=(60, 60, 60), width=8)
        d.ellipse((620, 220, 660, 260), fill=(70, 70, 70))
        pts = [(640, 360)]
        for _ in range(8):
            x, y = pts[-1]
            pts.append((x + j(-70, 70), y + j(50, 85)))
        d.line(pts, fill=(30, 30, 34), width=14, joint="curve")
        d.line([(x + 14, y) for x, y in pts[:-1]], fill=(190, 160, 50), width=6)
        d.rectangle((0, 860, w, h), fill=(92, 92, 92))
    elif kind == "puddle":
        _gradient(d, (0, 0, w, h), (126, 124, 118), (104, 102, 96))
        for x in range(0, w, 160):
            d.line([(x, 0), (x, h)], fill=(96, 94, 88), width=3)
        cx, cy = j(500, 780), j(520, 700)
        d.ellipse((cx - 340, cy - 130, cx + 340, cy + 130), fill=(92, 120, 138))
        d.ellipse((cx - 220, cy - 70, cx + 160, cy + 40), fill=(122, 150, 166))
        d.rectangle((120, 700, 220, 780), fill=(60, 60, 60))  # drain
    elif kind == "roof_sheeting":
        _gradient(d, (0, 0, w, 330), (170, 196, 214), (206, 220, 228))
        for i in range(10):
            x = i * 140 - 30
            d.polygon([(x, 960), (x + 140, 960), (x + 215, 330), (x + 75, 330)],
                      fill=(122 + (i % 2) * 14, 126 + (i % 2) * 14, 130 + (i % 2) * 14), outline=(80, 82, 84))
        mx = j(3, 6) * 130
        d.polygon([(mx, 960), (mx + 280, 960), (mx + 355, 330), (mx + 75, 330)], fill=(48, 44, 40))
        for x in range(mx + 40, mx + 300, 60):
            d.line([(x, 340), (x - 40, 950)], fill=(110, 84, 54), width=10)
    elif kind == "door_frame":
        _gradient(d, (0, 0, w, h), (172, 168, 158), (146, 142, 132))
        d.polygon([(420, 100), (870, 130), (850, 860), (440, 860)], fill=(60, 56, 52))
        d.polygon([(420, 100), (870, 130), (850, 860), (440, 860)], outline=(120, 90, 60), width=22)
        d.polygon([(470, 150), (830, 175), (820, 850), (470, 850)], fill=(140, 104, 70), outline=(84, 62, 40))
        d.rectangle((740, 480, 770, 520), fill=(210, 200, 90))
    elif kind == "rubble":
        _gradient(d, (0, 0, w, h), (168, 188, 202), (150, 150, 142))
        d.rectangle((0, 520, w, h), fill=(132, 126, 116))
        for _ in range(70):
            x, y, r = j(80, w - 80), j(560, 900), j(20, 70)
            c = j(80, 150)
            d.polygon([(x - r, y), (x - r // 2, y - r), (x + r, y - r // 3), (x + r // 2, y + r // 2)],
                      fill=(c, c - 8, c - 14), outline=(60, 58, 54))
    elif kind == "fence_gap":
        _gradient(d, (0, 0, w, h), (176, 198, 212), (140, 150, 120))
        d.rectangle((0, 560, w, h), fill=(112, 120, 92))
        for x in range(60, w, 110):
            if 500 < x < 820:
                d.line([(x, 880), (x + 90, 820)], fill=(70, 70, 74), width=6)  # fallen
                continue
            d.line([(x, 380), (x, 780)], fill=(70, 70, 74), width=8)
        d.line([(0, 460), (500, 460)], fill=(90, 90, 94), width=4)
        d.line([(820, 460), (w, 460)], fill=(90, 90, 94), width=4)
    else:
        raise ValueError(kind)
    return img


def make_photo(path: Path, kind: str, stop: int, idx: int, of: int, label: str, rng: random.Random):
    from PIL import Image, ImageDraw
    img = scene(kind, rng).convert("RGBA")
    layer = Image.new("RGBA", (1800, 1800), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((330, 820), "SYNTHETIC SAMPLE", fill=(255, 255, 255, 105), font=_font(120))
    layer = layer.rotate(32, resample=Image.BICUBIC)
    ox, oy = (layer.width - SIZE[0]) // 2, (layer.height - SIZE[1]) // 2
    img.alpha_composite(layer.crop((ox, oy, ox + SIZE[0], oy + SIZE[1])))
    img = img.convert("RGB")
    d = ImageDraw.Draw(img)
    d.rectangle((0, SIZE[1] - 64, SIZE[0], SIZE[1]), fill=(28, 32, 38))
    d.text((24, SIZE[1] - 52), f"Stop {stop} · photo {idx}/{of} · {label}", fill=(235, 235, 230),
           font=_font(34))
    img.save(path, "JPEG", quality=62, optimize=False)  # no exif= argument -> no EXIF


def make_map(path: Path, pin_xy: dict, n_stops: int):
    from PIL import Image, ImageDraw
    w = 800
    img = Image.new("RGB", (w, w), (238, 240, 236))
    d = ImageDraw.Draw(img)
    for g in range(0, w + 1, 100):
        d.line([(g, 0), (g, w)], fill=(206, 210, 204), width=1)
        d.line([(0, g), (w, g)], fill=(206, 210, 204), width=1)
    d.rectangle((60, 90, 740, 700), outline=(150, 156, 150), width=3)
    pts = {}
    for s in range(1, n_stops + 1):
        la, ln = pin_xy.get(s, (0.0022, 0.0036))
        pts[s] = (int(w / 2 + ln / 0.005 * 340), int(w / 2 - la / 0.005 * 340))
    ordered = [pts[s] for s in sorted(pts)]
    d.line(ordered, fill=(120, 130, 150), width=3)
    for s, (x, y) in pts.items():
        d.ellipse((x - 20, y - 20, x + 20, y + 20), fill=(52, 92, 140), outline=(255, 255, 255), width=3)
        d.text((x, y), str(s), fill=(255, 255, 255), font=_font(26), anchor="mm")
    d.text((w // 2, 40), "Schematic - not to scale - synthetic data", fill=(60, 60, 60),
           font=_font(28), anchor="mm")
    d.text((w // 2, w - 40), "Northwind Training Depot (fictional)", fill=(90, 90, 90), font=_font(22),
           anchor="mm")
    img.save(path, "PNG")


# ---------------------------------------------------------------------------------------------
# TTS -> Opus
# ---------------------------------------------------------------------------------------------
def wav_to_opus(wav: Path, opus: Path):
    import av
    from av.audio.resampler import AudioResampler
    with av.open(str(wav)) as src, av.open(str(opus), "w", format="ogg") as dst:
        out = dst.add_stream("libopus", rate=48000)
        out.bit_rate = 24000
        rs = AudioResampler(format="s16", layout="mono", rate=48000)
        sin = src.streams.audio[0]
        for frame in src.decode(sin):
            for f in rs.resample(frame):
                for pkt in out.encode(f):
                    dst.mux(pkt)
        for f in rs.resample(None):
            for pkt in out.encode(f):
                dst.mux(pkt)
        for pkt in out.encode(None):
            dst.mux(pkt)


def _pick_voice(engine, lang: str):
    names = {"en": ("english", "en-", "en_", "david", "zira"), "fr": ("french", "fr-", "fr_", "fran"),
             "es": ("spanish", "es-", "es_", "espa")}[lang]
    for v in engine.getProperty("voices"):
        blob = f"{v.id} {v.name} {getattr(v, 'languages', '')}".lower()
        if any(n in blob for n in names):
            return v.id
    return None


def voice_langs() -> set[str]:
    """Languages (en/fr/es) for which the local speech engine has a voice."""
    try:
        import pyttsx3
        engine = pyttsx3.init()
        return {lg for lg in ("en", "fr", "es") if _pick_voice(engine, lg)}
    except Exception:
        return set()


_TTS_CHILD = r"""
import sys, pyttsx3
text, lang, wav = sys.argv[1:4]
names = {"en": ("english", "en-", "en_"), "fr": ("french", "fr-", "fr_"), "es": ("spanish", "es-", "es_")}[lang]
e = pyttsx3.init()
for v in e.getProperty("voices"):
    if any(n in (v.id + " " + v.name + " " + str(getattr(v, "languages", ""))).lower() for n in names):
        e.setProperty("voice", v.id)
        break
else:
    if lang != "en":
        sys.exit(3)
e.setProperty("rate", 150)
e.save_to_file(text, wav)
e.runAndWait()
"""


def tts_render(items: list[tuple[str, str, str]], tmp: Path) -> tuple[dict[str, Path], str | None]:
    """items = [(filename, text, lang)] -> {filename: opus path}. Returns ({}, None) if no engine/voice.

    Each note is synthesised in its own short-lived child process: pyttsx3 (SAPI5 in particular)
    can hang on a second runAndWait() in the same process.
    """
    import subprocess
    try:
        import pyttsx3  # noqa: F401
    except Exception as exc:
        print(f"note: pyttsx3 unavailable ({type(exc).__name__}); no TTS", file=sys.stderr)
        return {}, None
    out: dict[str, Path] = {}
    for fn, text, lang in items:
        wav = tmp / (Path(fn).stem + ".wav")
        try:
            r = subprocess.run([sys.executable, "-c", _TTS_CHILD, text, lang, str(wav)],
                               capture_output=True, timeout=60)
        except Exception:
            continue
        if r.returncode != 0 or not wav.is_file() or wav.stat().st_size < 1000:
            continue
        opus = tmp / fn
        wav_to_opus(wav, opus)
        out[fn] = opus
    return out, "pyttsx3" if out else None


# ---------------------------------------------------------------------------------------------
def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def generate(out: Path, seed: int, fmt: str, tts: str, multilingual: bool, profile: str, voice_dir: Path):
    rng = random.Random(seed)
    spec = PROFILES[profile]
    rkey = spec["rating_key"]
    export = out / "export"
    if out.exists():
        shutil.rmtree(out)
    export.mkdir(parents=True)

    events = build_events(profile, rng)
    times = schedule(len(events), rng)
    pin_xy = pins(rng)
    assert len(events) == 40, len(events)

    ymd = "20300114"
    for e in events:
        if e["k"] == "photo":
            e["file"] = f"IMG-{ymd}-WA{e['n']:04d}.jpg"
        elif e["k"] == "audio":
            e["file"] = f"PTT-{ymd}-WA{e['n']:04d}.opus"

    (export / CHAT_NAME).write_text(render_chat(fmt, events, times, pin_xy), encoding="utf-8", newline="\n")

    # photos + caption sidecars
    for e in events:
        if e["k"] != "photo":
            continue
        kind, label, cat, rating = e["meta"]
        make_photo(export / e["file"], kind, e["stop"], e["idx"], e["of"], label, rng)
        cap = {"caption": f"{label} (synthetic scene, stop {e['stop']}).", "category": cat, rkey: rating,
               "finding": label, "stop": e["stop"], "scene": kind}
        (export / (Path(e["file"]).stem + ".caption.json")).write_text(
            json.dumps(cap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")

    # voice notes: text, language, audio
    ml = MULTILINGUAL[profile] if multilingual else {}
    if ml and tts != "none":  # only keep a foreign-language note if a voice for it exists
        have = voice_langs()
        ml = {k: v for k, v in ml.items() if v[0] in have}
    else:
        ml = {}
    notes = []
    for e in events:
        if e["k"] == "audio":
            lang, text = ml.get(e["n"], ("en", e["text"]))
            notes.append({"file": e["file"], "n": e["n"], "lang": lang, "text": text, "text_en": e["text"]})

    engine_used, langs_rendered = "none", {}
    audio: dict[str, Path] = {}
    tmp = Path(tempfile.mkdtemp(prefix="frp-tts-"))
    try:
        if tts in ("auto", "pyttsx3"):
            audio, eng = tts_render([(n["file"], n["text"], n["lang"]) for n in notes], tmp)
            if eng:
                engine_used = eng
            elif tts == "pyttsx3":
                raise SystemExit("--tts pyttsx3 requested but no speech engine is available")
        if len(audio) < len(notes):  # fall back to the committed set for anything not rendered
            sv = voice_dir / profile
            if tts == "pyttsx3" and audio:
                raise SystemExit("--tts pyttsx3: not every note could be rendered")
            audio = {}
            if not sv.is_dir():
                raise SystemExit(f"no TTS available and no pre-rendered voice set in {sv}")
            for n in notes:
                src = sv / n["file"]
                if not src.is_file():
                    raise SystemExit(f"missing pre-rendered voice note {src}")
                audio[n["file"]] = src
            engine_used = "prerendered:examples/sample_voice"
            # pre-rendered audio is English only; sidecars must describe what is actually spoken
            for n in notes:
                n["lang"], n["text"] = "en", n["text_en"]
        for n in notes:
            shutil.copyfile(audio[n["file"]], export / n["file"])
            langs_rendered[n["file"]] = n["lang"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for n in notes:
        side = {"text": n["text"], "language": n["lang"]}
        if n["lang"] != "en":
            side["text_en"] = n["text_en"]
        (export / (Path(n["file"]).stem + ".transcript.json")).write_text(
            json.dumps(side, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")

    make_map(out / "map.png", pin_xy, 5)
    cfg = {"site": SITE, "site_slug": "northwind-demo", "profile": profile,
           "date_from": "2030-01-14", "date_to": "2030-01-14", "inspectors": list(SENDERS),
           "export_txt": f"export/{CHAT_NAME}", "media_dir": "export", "map_image": "map.png"}
    (out / "config.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8", newline="\n")

    files = {str(p.relative_to(out)).replace("\\", "/"): sha256(p)
             for p in sorted(out.rglob("*")) if p.is_file()}
    manifest = {"generator": "examples/make_sample.py", "generator_version": GENERATOR_VERSION, "seed": seed,
                "format": fmt, "profile": profile, "tts_requested": tts, "tts_engine": engine_used,
                "multilingual_requested": multilingual,
                "voice_languages": langs_rendered,
                "counts": {"messages": len(events), "photos": 12, "voice_notes": len(notes),
                           "pins": len(pin_xy), "stops": 5},
                "files_sha256": files}
    (out / "sample_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
                                              newline="\n")
    return manifest


def render_voice(dest: Path):
    """Maintainer helper: render the English TTS set for both profiles into dest/<profile>/."""
    for profile in PROFILES:
        rng = random.Random(7)
        events = build_events(profile, rng)
        n = 0
        items = []
        for e in events:
            if e["k"] == "audio":
                items.append((f"PTT-20300114-WA{e['n']:04d}.opus", e["text"], "en"))
        tmp = Path(tempfile.mkdtemp(prefix="frp-tts-"))
        try:
            audio, eng = tts_render(items, tmp)
            if len(audio) != len(items):
                raise SystemExit("could not render all notes (no speech engine?)")
            (dest / profile).mkdir(parents=True, exist_ok=True)
            for fn, p in audio.items():
                shutil.copyfile(p, dest / profile / fn)
                n += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print(f"rendered {n} notes for {profile} with {eng}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the synthetic site-walk sample.")
    ap.add_argument("--out", default="examples/sample")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--format", choices=["android", "ios"], default="android")
    ap.add_argument("--tts", choices=["auto", "pyttsx3", "none"], default="auto")
    ap.add_argument("--multilingual", action="store_true")
    ap.add_argument("--profile", choices=list(PROFILES), default="site-inspection")
    ap.add_argument("--voice-dir", default=str(Path(__file__).resolve().parent / "sample_voice"))
    ap.add_argument("--render-voice-dir", default=None, help="maintainer: render TTS set here and exit")
    a = ap.parse_args(argv)
    if a.render_voice_dir:
        render_voice(Path(a.render_voice_dir))
        return 0
    m = generate(Path(a.out), a.seed, a.format, a.tts, a.multilingual, a.profile, Path(a.voice_dir))
    print(f"wrote {a.out}: {m['counts']['messages']} messages, tts={m['tts_engine']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
