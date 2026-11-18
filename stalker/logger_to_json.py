#!/usr/bin/env python3
"""
logger_to_json.py - S.T.A.L.K.E.R. 2 Zone heat map logger (Windows, Python 3.9+, stdlib only)

Reads what the mods write and turns it into data.json for index.html:
  * DLFR (Dynamic Live Faction Relations): %LOCALAPPDATA%\\Stalker2\\Saved\\DLFRAsi\\*
    plus its log (DLFRAsi.log / DLFR.log, only written when Log=1 in DLFR.ini).
    The format isn't documented, so files are parsed adaptively: JSON/INI/key=value/CSV
    snapshots are diffed (death/kill counters going up -> death events, fight/war/front
    counters going up -> fight events, new Chronicle-style text lines -> chronicle events,
    'front'/'hotspot' entries -> hottest fronts); log files are tailed line by line.
  * ZoneSensor (the UE4SS Lua mod): %LOCALAPPDATA%\\Stalker2\\Saved\\ZoneSensor_snap.jsonl
    -> NPC groups around you (clustered), your position/trail, and deaths it saw.

Writes data.json every 60 s (default). Never writes into the game or mod folders.

COMMANDS (PowerShell, run from the folder with this script):
  python logger_to_json.py                     # run: update data.json every 60 s (Ctrl+C stops)
  python logger_to_json.py run --push-every 10 # also git-push data.json to GitHub Pages every 10 min
  python logger_to_json.py once                # one update, then exit
  python logger_to_json.py discover            # Phase 0: report what DLFR/mods write (discovery_report.md)
  python logger_to_json.py discover --watch 600   # ...and watch the files for 10 min while you play
  python logger_to_json.py calib-add "Rostok gate" 1380 2100    # you stand there in-game; u v from the page's pick tool
  python logger_to_json.py calib-add "Bridge" 2310 1475 --world 123456 -78901   # type the world position yourself
  python logger_to_json.py calib-undo          # remove the last calibration point
  python logger_to_json.py calib-show          # show calibration points and error
  python logger_to_json.py place "Rostok" 1380 2100   # give a DLFR place name a map position

FILES (all next to this script unless changed in logger_config.json):
  logger_config.json   settings (created with defaults on first run)
  locations.json       place name -> map pixel [u, v] (for DLFR events that only carry a name)
  calib_points.json    calibration points (world x,y <-> map pixel u,v)
  data.json            output for index.html
  zoneheat_state\\      the logger's own history (events.jsonl, reader_state.json) - don't publish
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "logger_config.json"

DEFAULTS = {
    "setup": "Unchained",
    "interval_seconds": 60,
    "output": "data.json",
    "map_image": "map.jpg",
    "map_size_if_no_image": [4096, 4096],
    "dlfr_paths": [
        "%LOCALAPPDATA%\\Stalker2\\Saved\\DLFRAsi",
        "%LOCALAPPDATA%\\Stalker2\\Saved\\DLFRAsi.log",
        "%LOCALAPPDATA%\\Stalker2\\Saved\\DLFR.log",
    ],
    "zonesensor_file": "%LOCALAPPDATA%\\Stalker2\\Saved\\ZoneSensor_snap.jsonl",
    "discovery_extra_dirs": ["%LOCALAPPDATA%\\Stalker2\\Saved"],
    "locations_file": "locations.json",
    "calib_points_file": "calib_points.json",
    "state_dir": "zoneheat_state",
    "keep_hours": 72,
    "max_conflicts": 4000,
    "cluster_eps_m": 35,
    "cluster_min_size": 3,
    "trail_points": 200,
    "location_jitter_pct": 0.6,
    "log_rules": [
        {"regex": "(?P<a>\\w+)\\s+killed\\s+(?:an?\\s+)?(?:\\d+\\s+)?(?P<b>\\w+)(?:.*?\\b(?:at|near)\\s+(?P<loc>[A-Z][\\w'\\-]+(?: [A-Z][\\w'\\-]+)?))?",
         "kind": "death"}
    ],
    "factions": {
        "Loners": ["loner", "loners", "neutral", "neutrals", "free stalkers"],
        "Duty": ["duty"],
        "Freedom": ["freedom"],
        "Bandits": ["bandit", "bandits"],
        "Monolith": ["monolith", "monolithians"],
        "Mercenaries": ["mercenary", "mercenaries", "merc", "mercs"],
        "Ward": ["ward", "varta", "military", "militaries", "army"],
        "Scientists": ["scientist", "scientists", "ecologist", "ecologists"],
        "Spark": ["spark", "iskra"],
        "Noon": ["noon", "noonites"],
        "Corps": ["corps", "korpus", "corpus"],
        "Scavengers": ["scavenger", "scavengers"],
        "Renegades": ["renegade", "renegades"],
        "Mutants": ["mutant", "mutants"],
    },
}

DEFAULT_LOCATIONS = {
    "_help": "Place name -> map pixel [u, v] on YOUR map image. Use the page's pick tool (+) and "
             "'python logger_to_json.py place NAME U V', or type it here. 'aliases' = other spellings DLFR may use.",
    **{n: {"pixel": None, "aliases": a} for n, a in [
        ("Lesser Zone", []), ("Zalissya", []), ("Cordon", []), ("Rookie Village", []), ("Garbage", []),
        ("Slag Heap", []), ("Rostok", []), ("Wild Island", []), ("Shelter", []), ("Zaton", []),
        ("Sultansk", []), ("Icarus", []), ("Yaniv", ["Yaniv Station"]), ("Chemical Plant", []),
        ("Malachite", ["STC Malachite"]), ("Cement Factory", ["Cement Plant"]), ("Swamps", ["Great Swamps"]),
        ("Burnt Forest", []), ("Red Forest", []), ("Duga", []), ("Pripyat", ["Prypiat"]), ("SIRCAA", []),
        ("Chornobyl NPP", ["ChNPP", "Chernobyl NPP"])]},
}

DEATH_WORDS = {"death", "deaths", "dead", "died", "dies", "killed", "kill", "kills", "casualty", "casualties",
               "loss", "losses", "lost", "victims", "fallen", "slain"}
FIGHT_WORDS = {"fight", "fights", "fighting", "battle", "battles", "clash", "clashes", "attack", "attacks",
               "attacked", "raid", "raids", "skirmish", "assault", "war", "wars", "front", "fronts", "combat",
               "firefight", "ambush"}
FRONT_WORDS = {"front", "fronts", "hotspot", "hotspots", "hot", "hottest", "frontline"}
STRUCTURED = {"json", "jsonl", "ini", "kv", "csv"}
DEAD_NUM = re.compile(r"(\d+)\s*(?:dead|killed|deaths?|casualt(?:y|ies)|loss(?:es)?|fallen|victims)", re.I)
NUM_DEAD = re.compile(r"(?:dead|killed|deaths?|casualt(?:y|ies)|loss(?:es)?)\s*[:=]?\s*(\d+)", re.I)
TIMESTAMPISH = re.compile(r"^\d{4}-\d{2}-\d{2}|^\d{1,2}:\d{2}")
COORD_PATTERNS = [
    re.compile(r"X\s*=\s*(-?\d+(?:\.\d+)?)[ ,;]+Y\s*=\s*(-?\d+(?:\.\d+)?)(?:[ ,;]+Z\s*=\s*(-?\d+(?:\.\d+)?))?", re.I),
    re.compile(r"\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)"),
]
ID_FIELDS = ("id", "uid", "sid", "key", "name", "pair", "title", "front", "location")


# =============================================================================
# config + small file helpers
# =============================================================================

def expand(p) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(str(p))))
    return path if path.is_absolute() else HERE / path


def load_config(path=None) -> dict:
    cfg_path = Path(path) if path else CONFIG_FILE
    cfg = json.loads(json.dumps(DEFAULTS))
    if cfg_path.exists():
        user = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    elif path is None:
        cfg_path.write_text(json.dumps(DEFAULTS, indent=2), encoding="utf-8")
        print(f"Created {cfg_path} with default settings.")
    state = expand(cfg["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    cfg["_state"] = state
    loc = expand(cfg["locations_file"])
    if not loc.exists():
        write_json_atomic(loc, DEFAULT_LOCATIONS)
    return cfg


def write_json_atomic(path: Path, obj, compact=False) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    for attempt in range(5):  # Windows: the file may be open in another program for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.3)
    os.replace(tmp, path)


def decode_bytes(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    if len(raw) >= 8 and raw[1::2].count(0) > len(raw) // 4:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def read_text(path: Path, max_bytes=None) -> str:
    with open(path, "rb") as f:
        raw = f.read() if max_bytes is None else f.read(max_bytes)
    return decode_bytes(raw)


def looks_binary(raw: bytes) -> bool:
    if not raw:
        return False
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or (len(raw) >= 8 and raw[1::2].count(0) > len(raw) // 4):
        return False
    sample = raw[:4096]
    return sum(1 for b in sample if b < 9 or 13 < b < 32) > len(sample) * 0.05


def detect_format(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            raw = f.read(65536)
    except OSError:
        return "unreadable"
    if looks_binary(raw):
        return "binary"
    s = decode_bytes(raw).lstrip()
    if not s:
        return "empty"
    if s[0] in "{[":
        try:
            json.loads(read_text(path))
            return "json"
        except Exception:
            lines = [l for l in s.splitlines() if l.strip()][:20]
            ok = 0
            for l in lines:
                try:
                    json.loads(l)
                    ok += 1
                except Exception:
                    pass
            if lines and ok >= len(lines) * 0.8:
                return "jsonl"
    if s.startswith("<"):
        return "xml"
    lines = [l.strip() for l in s.splitlines() if l.strip()][:200]
    if any(re.match(r"^\[[^\]]+\]$", l) for l in lines) and any("=" in l for l in lines):
        return "ini"
    if lines and sum(bool(re.match(r"^[\w .\-/]+\s*[=:]\s*\S", l)) for l in lines) >= len(lines) * 0.7:
        return "kv"
    if len(lines) >= 3:
        for sep in (",", ";"):
            counts = [l.count(sep) for l in lines[:20]]
            if counts[0] >= 2 and len(set(counts)) == 1:
                return "csv"
    return "log"


def coerce(v: str):
    v = v.strip().strip('"')
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d*\.\d+(e-?\d+)?", v, re.I):
        return float(v)
    return v


def parse_structured(path: Path, fmt: str):
    text = read_text(path)
    if fmt == "json":
        return json.loads(text)
    if fmt == "jsonl":
        out = []
        for l in text.splitlines():
            if l.strip():
                try:
                    out.append(json.loads(l))
                except Exception:
                    pass
        return out
    if fmt in ("ini", "kv"):
        data, section = {}, None
        section = data
        for line in text.splitlines():
            line = line.strip()
            if not line or line[0] in ";#":
                continue
            m = re.match(r"^\[(.+)\]$", line)
            if m:
                section = data.setdefault(m.group(1).strip(), {})
                continue
            m = re.match(r"^([^=:]+?)\s*[=:]\s*(.*)$", line)
            if m:
                section[m.group(1).strip()] = coerce(m.group(2))
        return data
    if fmt == "csv":
        import csv
        import io
        delim = ";" if text.count(";") > text.count(",") else ","
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        return [{h: coerce(v) for h, v in zip(rows[0], r)} for r in rows[1:]] if rows else []
    return None


def flatten(obj, prefix="", out=None, max_items=20000) -> dict:
    out = {} if out is None else out
    if len(out) >= max_items:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}/{k}" if prefix else str(k), out, max_items)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            flatten(v, f"{prefix}/{i}" if prefix else str(i), out, max_items)
    else:
        out[prefix] = obj
    return out


def read_jsonl_tail(path: Path, max_bytes=4_000_000) -> list:
    if not path.exists():
        return []
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()
        raw = f.read()
    out = []
    for line in decode_bytes(raw).splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass  # line still being written
    return out


def image_size(path: Path):
    with open(path, "rb") as f:
        head = f.read(32)
        if head.startswith(b"\x89PNG"):
            w, h = struct.unpack(">II", head[16:24])
            return int(w), int(h)
        if head[:2] == b"\xff\xd8":
            f.seek(2)
            while True:
                b = f.read(1)
                while b and b != b"\xff":
                    b = f.read(1)
                while b == b"\xff":
                    b = f.read(1)
                if not b:
                    break
                if b[0] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    f.read(3)
                    h, w = struct.unpack(">HH", f.read(4))
                    return int(w), int(h)
                seglen = struct.unpack(">H", f.read(2))[0]
                f.seek(seglen - 2, 1)
    return None


def _id(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode("utf-8", "replace")).hexdigest()[:16]


# =============================================================================
# names: factions + locations
# =============================================================================

def tokens(text) -> list:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text))
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)
    return [t for t in re.split(r"[^A-Za-z0-9]+", text.lower()) if t]


class Lexicon:
    def __init__(self, factions: dict, locations: dict):
        self.factions, self.locations = factions, locations
        self.single, self.multi = {}, []
        for canon, aliases in factions.items():
            for a in {canon.lower(), *[x.lower() for x in aliases]}:
                if " " in a:
                    self.multi.append((a, canon))
                else:
                    self.single[a] = canon
        self.loc_patterns = []
        for name, info in locations.items():
            for n in [name] + list((info or {}).get("aliases", [])):
                pat = re.compile(r"(?<![a-z0-9])" + re.escape(n.lower()) + r"(?![a-z0-9])")
                self.loc_patterns.append((len(n), pat, name))
        self.loc_patterns.sort(key=lambda x: -x[0])

    def factions_in(self, text) -> list:
        found = []
        for t in tokens(text):
            c = self.single.get(t)
            if c and c not in found:
                found.append(c)
        low = str(text).lower()
        for a, c in self.multi:
            if a in low and c not in found:
                found.append(c)
        return found

    def location_in(self, text):
        low = str(text).lower() + " " + " ".join(tokens(text))
        for _, pat, name in self.loc_patterns:
            if pat.search(low):
                return name
        return None


def load_locations(cfg) -> dict:
    p = expand(cfg["locations_file"])
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


def make_lexicon(cfg) -> Lexicon:
    return Lexicon(cfg["factions"], load_locations(cfg))


def find_coords(text):
    for pat in COORD_PATTERNS:
        m = pat.search(str(text))
        if m:
            z = m.group(3) if (m.lastindex or 0) >= 3 else None
            return float(m.group(1)), float(m.group(2)), (float(z) if z else None)
    return None


def coords_from_dict(d):
    if not isinstance(d, dict):
        return None
    low = {str(k).lower(): v for k, v in d.items()}
    for kx, ky, kz in (("x", "y", "z"), ("posx", "posy", "posz"), ("locx", "locy", "locz"), ("worldx", "worldy", "worldz")):
        if kx in low and ky in low:
            try:
                return float(low[kx]), float(low[ky]), (float(low[kz]) if low.get(kz) is not None else None)
            except (TypeError, ValueError):
                pass
    for key in ("location", "pos", "position", "loc", "coords", "worldlocation"):
        v = low.get(key)
        if isinstance(v, dict):
            c = coords_from_dict(v)
            if c:
                return c
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            try:
                return float(v[0]), float(v[1]), (float(v[2]) if len(v) > 2 else None)
            except (TypeError, ValueError):
                pass
        if isinstance(v, str):
            c = find_coords(v)
            if c:
                return c
    return None


# =============================================================================
# calibration: world (UE units, cm) -> map pixels
# =============================================================================

def solve3(A, b):
    M = [list(map(float, r)) + [float(x)] for r, x in zip(A, b)]
    n = 3
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            raise ValueError("calibration points are collinear or duplicated - pick spots that form a triangle")
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def fit_affine(points):
    ata = [[0.0] * 3 for _ in range(3)]
    atu, atv = [0.0] * 3, [0.0] * 3
    for p in points:
        x, y = map(float, p["world"][:2])
        u, v = map(float, p["pixel"][:2])
        row = (x, y, 1.0)
        for i in range(3):
            for j in range(3):
                ata[i][j] += row[i] * row[j]
            atu[i] += row[i] * u
            atv[i] += row[i] * v
    m = [solve3(ata, atu), solve3(ata, atv)]
    res = []
    for p in points:
        u, v = apply_affine(m, *p["world"][:2])
        res.append(math.hypot(u - p["pixel"][0], v - p["pixel"][1]))
    return m, res


def apply_affine(m, x, y):
    (a, b, c), (d, e, f) = m
    return a * x + b * y + c, d * x + e * y + f


def pixels_per_meter(m) -> float:
    (a, b, _), (d, e, _) = m
    return (math.hypot(a, d) + math.hypot(b, e)) / 2 * 100.0  # UE units are cm


def load_points(cfg) -> list:
    p = expand(cfg["calib_points_file"])
    return json.loads(p.read_text(encoding="utf-8")).get("points", []) if p.exists() else []


def calibration(cfg):
    pts = load_points(cfg)
    if len(pts) < 3:
        return None, None
    try:
        return fit_affine(pts)
    except ValueError:
        return None, None


def map_size(cfg):
    img = expand(cfg["map_image"])
    if img.exists():
        sz = image_size(img)
        if sz:
            return sz, True
    return tuple(cfg["map_size_if_no_image"]), False


def latest_player(cfg, max_age_s=60):
    for row in reversed(read_jsonl_tail(expand(cfg["zonesensor_file"]), 400_000)):
        if row.get("type") == "snap" and row.get("player"):
            age = time.time() - float(row.get("t", 0))
            return (row["player"], age) if age <= max_age_s else (None, age)
    return None, None


def calib_report(cfg) -> str:
    pts = load_points(cfg)
    if len(pts) < 3:
        return f"{len(pts)} calibration point(s) - need at least 3 (4-6 spread across the map is best)."
    try:
        m, res = fit_affine(pts)
    except ValueError as e:
        return str(e)
    (w, _), _ = map_size(cfg)
    rms = math.sqrt(sum(r * r for r in res) / len(res))
    lines = [f"{len(pts)} points, RMS error {rms:.1f} px = {100 * rms / w:.2f}% of map width "
             f"({'good' if 100 * rms / w < 1 else 'check the worst point'}); scale {pixels_per_meter(m):.4f} px/m"]
    lines += [f"  {p['name']:<24} error {r:6.1f} px" for p, r in zip(pts, res)]
    return "\n".join(lines)


def cmd_calib_add(cfg, name, u, v, world):
    if world is None:
        player, age = latest_player(cfg)
        if not player:
            print("No fresh player position from ZoneSensor"
                  + (f" (last snapshot {age:.0f} s old)" if age else "")
                  + ". Is the game running with ZoneSensor? Or pass --world X Y.")
            return
        world = [player["x"], player["y"]]
    pts = load_points(cfg)
    pts.append({"name": name, "world": [float(world[0]), float(world[1])], "pixel": [float(u), float(v)],
                "added": datetime.now().isoformat(timespec="seconds")})
    write_json_atomic(expand(cfg["calib_points_file"]), {"points": pts})
    print(f"Added '{name}': world {world[0]:.0f}, {world[1]:.0f} -> pixel {u:.0f}, {v:.0f}")
    print(calib_report(cfg))


def cmd_calib_undo(cfg):
    pts = load_points(cfg)
    if pts:
        p = pts.pop()
        write_json_atomic(expand(cfg["calib_points_file"]), {"points": pts})
        print(f"Removed '{p['name']}'")
    print(calib_report(cfg))


def cmd_place(cfg, name, u, v):
    p = expand(cfg["locations_file"])
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    key = next((k for k in data if k.lower() == name.lower()), name)
    entry = data.get(key) if isinstance(data.get(key), dict) else {"pixel": None, "aliases": []}
    entry["pixel"] = [round(float(u), 1), round(float(v), 1)]
    data[key] = entry
    write_json_atomic(p, data)
    print(f"'{key}' placed at {entry['pixel']}. Old and new events at '{key}' will now show on the map.")


# =============================================================================
# DLFR reader (adaptive)
# =============================================================================

def stabilize(obj, lex):
    """Lists of dicts -> dicts keyed by a stable identity, so re-sorted lists
    (e.g. re-ranked hottest fronts) don't look like every counter changed."""
    if isinstance(obj, dict):
        return {k: stabilize(v, lex) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [stabilize(v, lex) for v in obj]
        if items and all(isinstance(v, dict) for v in items):
            for f in ID_FIELDS:
                ids = [next((v[k] for k in v if str(k).lower() == f), None) for v in items]
                if all(isinstance(i, (str, int)) for i in ids) and len(set(ids)) == len(ids):
                    return {f"{f}={i}": v for i, v in zip(ids, items)}
            ids = []
            for v in items:
                facs = sorted({fc for s in v.values() if isinstance(s, str) for fc in lex.factions_in(s)})
                ids.append("+".join(facs) if len(facs) >= 2 else None)
            if all(ids) and len(set(ids)) == len(ids):
                return {f"pair={i}": v for i, v in zip(ids, items)}
        return items
    return obj


class DLFRReader:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state_path = cfg["_state"] / "reader_state.json"
        self.events_path = cfg["_state"] / "events.jsonl"
        self.fronts_path = cfg["_state"] / "fronts.json"
        self.state = {"files": {}, "seen": []}
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        self.seen = set(self.state.get("seen", []))
        self.rules = []
        for r in cfg.get("log_rules") or []:
            try:
                self.rules.append((re.compile(r["regex"], re.I), r.get("kind", "fight")))
            except (re.error, KeyError) as e:
                print(f"[dlfr] ignoring bad log rule: {e}")
        self.lex = make_lexicon(cfg)

    def files(self):
        out = []
        for raw in self.cfg["dlfr_paths"]:
            p = expand(raw)
            if p.is_dir():
                out += [q for q in sorted(p.rglob("*")) if q.is_file() and q.suffix.lower() not in (".tmp", ".bak")]
            elif p.is_file():
                out.append(p)
        return out

    def add_events(self, events):
        new = [e for e in events if e["id"] not in self.seen]
        for e in new:
            self.seen.add(e["id"])
        if new:
            with open(self.events_path, "a", encoding="utf-8") as f:
                for e in new:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return new

    def poll(self):
        self.lex = make_lexicon(self.cfg)  # pick up locations.json edits
        events, fronts = [], []
        for p in self.files():
            st = self.state["files"].get(str(p))
            fmt = st["fmt"] if st and st.get("fmt") else detect_format(p)
            if fmt in ("binary", "unreadable", "empty", "xml"):
                continue
            try:
                if fmt in STRUCTURED:
                    ev, fr = self._structured(p, fmt)
                    fronts += fr
                else:
                    ev = self._log(p)
                events += ev
            except Exception as e:  # never die on one odd file
                print(f"[dlfr] {p.name}: {e!r}")
        new = self.add_events(events)
        if fronts:
            write_json_atomic(self.fronts_path, {"t": time.time(), "fronts": fronts})
        self.state["seen"] = list(self.seen)[-20000:]
        write_json_atomic(self.state_path, self.state)
        return new

    def _structured(self, p: Path, fmt: str):
        key, stt = str(p), p.stat()
        fst = self.state["files"].get(key)
        if fst and fst.get("mtime") == stt.st_mtime and fst.get("size") == stt.st_size:
            return [], fst.get("fronts", [])
        flat = flatten(stabilize(parse_structured(p, fmt), self.lex))
        events = [] if fst is None else self._diff(p, fst.get("flat", {}), flat)  # first sight = baseline
        fronts = self._fronts(p, flat)
        self.state["files"][key] = {"fmt": fmt, "mtime": stt.st_mtime, "size": stt.st_size, "flat": flat, "fronts": fronts}
        return events, fronts

    @staticmethod
    def _context(flat, parent):
        leaf, texts = {}, [parent]
        pre = parent + "/" if parent else ""
        for k, v in flat.items():
            if k.startswith(pre) and "/" not in k[len(pre):]:
                leaf[k[len(pre):]] = v
                if isinstance(v, str):
                    texts.append(v)
        return " ".join(texts), leaf

    def _diff(self, p, prev, flat):
        src = f"{p.parent.name}/{p.name}"
        by_parent = defaultdict(list)
        events = []
        prev_strings = {v for v in prev.values() if isinstance(v, str)}
        for k, v in flat.items():
            old = prev.get(k)
            parent = k.rsplit("/", 1)[0] if "/" in k else ""
            ktoks = {t for t in tokens(k) if not t.isdigit()}
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                oldn = old if isinstance(old, (int, float)) and not isinstance(old, bool) else (0 if old is None else None)
                if oldn is None or v <= oldn:
                    continue
                if ktoks & DEATH_WORDS:
                    by_parent[parent].append(("death", k, v - oldn, v))
                elif ktoks & FIGHT_WORDS:
                    by_parent[parent].append(("fight", k, v - oldn, v))
            elif isinstance(v, str) and v not in prev_strings and len(v) >= 8 and not TIMESTAMPISH.match(v):
                facs = self.lex.factions_in(v)
                vt = set(tokens(v))
                if facs and (vt & (DEATH_WORDS | FIGHT_WORDS) or ktoks & {"chronicle", "event", "events", "log", "history", "news"}):
                    ctx, leaf = self._context(flat, parent)
                    m = DEAD_NUM.search(v) or NUM_DEAD.search(v)
                    dead = int(m.group(1)) if m else (1 if vt & DEATH_WORDS else 0)
                    events.append(self._event(src, "chronicle", facs, ctx + " " + v, leaf, dead, 0.5 + dead, v, _id(src, k, v)))
        for parent, items in by_parent.items():
            ctx, leaf = self._context(flat, parent)
            for kind in ("death", "fight"):
                its = [i for i in items if i[0] == kind]
                if not its:
                    continue
                deltas = [i[2] for i in its]
                mx = max(deltas)
                # a total plus its parts (e.g. deaths24h + lossesDuty + lossesFreedom): count the total once
                total = mx if (len(its) > 1 and abs(sum(deltas) - 2 * mx) < 1e-9) else sum(deltas)
                keys = " ".join(i[1] for i in its)
                facs = self.lex.factions_in(ctx + " " + keys)
                text = f"{parent or p.name}: " + ", ".join(f"{i[1].split('/')[-1]}+{i[2]:g}" for i in its)
                eid = _id(src, parent, kind, [i[3] for i in its])
                if kind == "death":
                    events.append(self._event(src, "death", facs, ctx + " " + keys, leaf, int(round(total)), float(total), text, eid))
                else:
                    events.append(self._event(src, "fight", facs, ctx, leaf, 0, max(0.5, 0.5 * total), text, eid))
        return events

    def _fronts(self, p, flat):
        groups = {}
        for k, v in flat.items():
            if not set(tokens(k)) & FRONT_WORDS:
                continue
            parent = k.rsplit("/", 1)[0] if "/" in k else k
            g = groups.setdefault(parent, {"texts": [], "numbers": {}})
            if isinstance(v, str):
                g["texts"].append(v)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                g["numbers"][k.split("/")[-1]] = v
        fronts = []
        for parent, g in groups.items():
            loc = self.lex.location_in(" ".join(g["texts"]))
            facs = self.lex.factions_in(" ".join(g["texts"]) + " " + parent)
            if not (loc or facs):
                continue
            heat = sum(v for kk, v in g["numbers"].items() if set(tokens(kk)) & (DEATH_WORDS | FIGHT_WORDS | {"heat", "score", "intensity"}))
            fronts.append({"name": loc or " / ".join(g["texts"])[:60], "location": loc, "factions": facs, "heat": heat})
        fronts.sort(key=lambda f: -(f["heat"] or 0))
        return fronts

    def _log(self, p: Path):
        key, size = str(p), p.stat().st_size
        with open(p, "rb") as f:
            utf16 = f.read(2) in (b"\xff\xfe", b"\xfe\xff")
        nl = b"\n\x00" if utf16 else b"\n"
        if key not in self.state["files"]:
            self.state["files"][key] = {"fmt": "log", "offset": size}  # start at the end; no history flood
            return []
        offset = self.state["files"][key].get("offset", 0)
        if size < offset:  # truncated = new game session
            offset = 0
        if size == offset:
            return []
        with open(p, "rb") as f:
            f.seek(offset)
            chunk = f.read(8_000_000)
        cut = chunk.rfind(nl)
        if cut < 0:
            return []
        chunk = chunk[:cut + len(nl)]
        self.state["files"][key]["offset"] = offset + len(chunk)
        text = chunk.decode("utf-16-le" if utf16 else "utf-8", errors="replace").lstrip("﻿")
        src = f"{p.parent.name}/{p.name}"
        out = []
        for i, line in enumerate(text.splitlines()):
            line = line.strip()
            if line:
                ev = self._line(src, line, _id(src, offset, i, line))
                if ev:
                    out.append(ev)
        return out

    def _line(self, src, line, eid):
        for rx, kind in self.rules:
            m = rx.search(line)
            if not m:
                continue
            g = m.groupdict()
            facs = list(dict.fromkeys(self.lex.factions_in(g.get("a") or "") + self.lex.factions_in(g.get("b") or ""))) \
                or self.lex.factions_in(line)
            if not facs:
                continue
            leaf = {"x": g["x"], "y": g["y"], "z": g.get("z")} if g.get("x") and g.get("y") else {}
            nm = DEAD_NUM.search(line) or NUM_DEAD.search(line)
            dead = int(g["dead"]) if g.get("dead") else (int(nm.group(1)) if nm else (1 if kind == "death" else 0))
            return self._event(src, g.get("kind") or kind, facs, (g.get("loc") or "") + " " + line, leaf, dead,
                               float(dead) if dead else 0.5, line, eid)
        toks = set(tokens(line))
        is_death, is_fight = bool(toks & DEATH_WORDS), bool(toks & FIGHT_WORDS)
        if not (is_death or is_fight):
            return None
        facs = self.lex.factions_in(line)
        if not facs:
            return None
        m = DEAD_NUM.search(line) or NUM_DEAD.search(line)
        dead = int(m.group(1)) if m else (1 if is_death else 0)
        return self._event(src, "death" if is_death else "fight", facs, line, {}, dead, float(dead) if dead else 0.5, line, eid)

    def _event(self, src, kind, facs, ctx, leaf, dead, weight, text, eid):
        ev = {"id": eid, "t": round(time.time(), 1), "setup": self.cfg["setup"], "source": src, "kind": kind,
              "factions": facs, "location": self.lex.location_in(ctx), "dead": int(dead),
              "weight": round(float(weight), 2), "text": str(text)[:240]}
        xyz = (coords_from_dict(leaf) if leaf else None) or find_coords(ctx)
        if xyz:
            ev["x"], ev["y"] = xyz[0], xyz[1]
        return ev


# =============================================================================
# ZoneSensor + clustering
# =============================================================================

def dbscan(pts, eps, min_pts):
    cell = defaultdict(list)
    for i, (x, y) in enumerate(pts):
        cell[(int(x // eps), int(y // eps))].append(i)

    def nb(i):
        x, y = pts[i]
        cx, cy = int(x // eps), int(y // eps)
        return [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in cell.get((cx + dx, cy + dy), ())
                if (pts[j][0] - x) ** 2 + (pts[j][1] - y) ** 2 <= eps * eps]

    labels = [None] * len(pts)
    cid = 0
    for i in range(len(pts)):
        if labels[i] is not None:
            continue
        n = nb(i)
        if len(n) < min_pts:
            labels[i] = -1
            continue
        labels[i] = cid
        queue = [j for j in n if j != i]
        while queue:
            j = queue.pop()
            if labels[j] == -1:
                labels[j] = cid
            if labels[j] is not None:
                continue
            labels[j] = cid
            n2 = nb(j)
            if len(n2) >= min_pts:
                queue.extend(n2)
        cid += 1
    return labels


def zonesensor_ingest(cfg, reader: DLFRReader):
    """Store deaths ZoneSensor saw (with real positions) in the event history; return snapshots."""
    rows = read_jsonl_tail(expand(cfg["zonesensor_file"]), 6_000_000)
    deaths = []
    for r in rows:
        if r.get("type") == "death" and r.get("x") is not None:
            fac = r.get("fac") or ("Mutants" if r.get("kind") == "mutant" else "Unknown")
            deaths.append({"id": _id("zs", r.get("id"), r.get("t")), "t": float(r.get("t", time.time())),
                           "setup": cfg["setup"], "source": "ZoneSensor", "kind": "death_seen",
                           "factions": [fac], "location": None, "x": r["x"], "y": r["y"], "dead": 1,
                           "weight": 1.0, "text": f"{fac} {r.get('cls') or ''} died".strip()})
    new = reader.add_events(deaths)
    return [r for r in rows if r.get("type") == "snap"], new


def build_groups(cfg, snap, m):
    ppm = pixels_per_meter(m)
    by_key, actors = defaultdict(list), []
    for a in snap.get("actors", []):
        if a.get("alive") is False or a.get("x") is None:
            continue
        kind = a.get("kind") or "other"
        key = "Mutants" if kind == "mutant" else (a.get("fac") or "Unknown")
        by_key[key].append(a)
        u, v = apply_affine(m, a["x"], a["y"])
        actors.append({"u": round(u, 1), "v": round(v, 1), "faction": key, "kind": kind, "cls": a.get("cls")})
    groups = []
    eps, min_pts = float(cfg["cluster_eps_m"]), int(cfg["cluster_min_size"])
    for key, members in by_key.items():
        pts = [(mm["x"] / 100.0, mm["y"] / 100.0) for mm in members]
        clusters = defaultdict(list)
        for mm, lab, pt in zip(members, dbscan(pts, eps, min_pts), pts):
            if lab is not None and lab >= 0:
                clusters[lab].append((mm, pt))
        for items in clusters.values():
            cx = sum(pt[0] for _, pt in items) / len(items)
            cy = sum(pt[1] for _, pt in items) / len(items)
            rad = max(math.hypot(pt[0] - cx, pt[1] - cy) for _, pt in items) + 8
            u, v = apply_affine(m, cx * 100, cy * 100)
            groups.append({"t": snap.get("t"), "u": round(u, 1), "v": round(v, 1), "r": round(rad * ppm, 1),
                           "n": len(items), "faction": key, "mutant": key == "Mutants",
                           "combat": any(mm.get("combat") is True for mm, _ in items),
                           "classes": [[c, n] for c, n in Counter(mm.get("cls") or "?" for mm, _ in items).most_common(3)]})
    groups.sort(key=lambda g: -g["n"])
    return groups, actors


# =============================================================================
# data.json
# =============================================================================

def jitter(eid, scale):
    h = hashlib.sha1(str(eid).encode()).digest()
    a, r = h[0] / 255 * 2 * math.pi, (h[1] / 255) ** 0.5 * scale
    return r * math.cos(a), r * math.sin(a)


def build_data(cfg, snaps) -> dict:
    now = time.time()
    since = now - float(cfg["keep_hours"]) * 3600
    (w, h), has_image = map_size(cfg)
    m, _ = calibration(cfg)
    locs = load_locations(cfg)
    jit = w * float(cfg["location_jitter_pct"]) / 100

    def loc_px(name):
        info = locs.get(name or "")
        return tuple(info["pixel"][:2]) if info and info.get("pixel") else None

    events = read_jsonl_tail(cfg["_state"] / "events.jsonl", 20_000_000)
    conflicts, unplaced = [], []
    wars = defaultdict(lambda: {"dead": 0, "events": 0, "last": 0.0})
    for e in events:
        t = float(e.get("t", 0))
        if t < since or (e.get("setup") and e.get("setup") != cfg["setup"]):
            continue
        facs = e.get("factions") or []
        pair = " vs ".join(sorted(facs)[:2]) if len(facs) >= 2 else (facs[0] if facs else "Unknown")
        wars[pair]["dead"] += int(e.get("dead") or 0)
        wars[pair]["events"] += 1
        wars[pair]["last"] = max(wars[pair]["last"], t)
        uv = None
        if e.get("x") is not None and e.get("y") is not None and m:
            uv = apply_affine(m, float(e["x"]), float(e["y"]))
        elif e.get("location") and loc_px(e["location"]):
            px = loc_px(e["location"])
            dx, dy = jitter(e.get("id"), jit)
            uv = (px[0] + dx, px[1] + dy)
        row = {"t": round(t, 1), "kind": e.get("kind", "fight"), "factions": facs, "location": e.get("location"),
               "dead": int(e.get("dead") or 0), "text": (e.get("text") or "")[:160], "source": e.get("source")}
        if uv:
            row.update(u=round(uv[0], 1), v=round(uv[1], 1), w=float(e.get("weight") or 1.0))
            conflicts.append(row)
        else:
            unplaced.append(row)
    conflicts = conflicts[-int(cfg["max_conflicts"]):]

    groups, actors, player, trail, groups_t = [], [], None, [], None
    if snaps and m:
        last = snaps[-1]
        groups_t = float(last.get("t", 0))
        groups, actors = build_groups(cfg, last, m)
        if last.get("player"):
            u, v = apply_affine(m, last["player"]["x"], last["player"]["y"])
            player = {"u": round(u, 1), "v": round(v, 1), "t": groups_t}
        recent = [s for s in snaps if s.get("player") and float(s.get("t", 0)) >= since]
        step = max(1, len(recent) // int(cfg["trail_points"]))
        for s in recent[::step]:
            u, v = apply_affine(m, s["player"]["x"], s["player"]["y"])
            trail.append([round(u, 1), round(v, 1)])

    fronts = []
    fp = cfg["_state"] / "fronts.json"
    if fp.exists():
        try:
            for i, f in enumerate(json.loads(fp.read_text(encoding="utf-8")).get("fronts", [])[:10]):
                px = loc_px(f.get("location"))
                fronts.append({"rank": i + 1, "name": f.get("name"), "u": px and round(px[0], 1),
                               "v": px and round(px[1], 1), "factions": f.get("factions", []), "heat": f.get("heat")})
        except Exception:
            pass

    return {
        "schema": "zoneheat/1",
        "generated": round(now, 1),
        "setup": cfg["setup"],
        "map": {"width": w, "height": h, "calibrated": bool(m),
                "pixels_per_meter": round(pixels_per_meter(m), 5) if m else None,
                "labels": {k: v["pixel"][:2] for k, v in locs.items() if v.get("pixel")}},
        "conflicts": conflicts,
        "groups": groups,
        "groups_t": groups_t,
        "actors": actors,
        "fronts": fronts,
        "wars": sorted(({"pair": k, **v} for k, v in wars.items()), key=lambda r: (-r["dead"], -r["events"]))[:20],
        "player": player,
        "trail": trail,
        "unplaced": unplaced[-100:][::-1],
    }


def git_push(out: Path):
    repo = out.parent
    kw = {"cwd": str(repo), "capture_output": True, "text": True, "timeout": 120}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    if not shutil.which("git"):
        print("[git] git not found on PATH - install Git for Windows or drop --push-every")
        return
    try:
        if subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], **kw).returncode != 0:
            print(f"[git] {repo} is not a git repository - clone your GitHub Pages repo there first")
            return
        subprocess.run(["git", "add", out.name], **kw)
        if subprocess.run(["git", "diff", "--cached", "--quiet", "--", out.name], **kw).returncode == 0:
            return  # nothing changed
        r = subprocess.run(["git", "commit", "-m", f"data.json {datetime.now():%Y-%m-%d %H:%M}", "--", out.name], **kw)
        if r.returncode != 0:
            print("[git] commit failed:", r.stderr.strip()[:300])
            return
        r = subprocess.run(["git", "push"], **kw)
        print("[git] pushed data.json" if r.returncode == 0 else f"[git] push failed: {r.stderr.strip()[:300]}")
    except subprocess.TimeoutExpired:
        print("[git] timed out")


def update_once(cfg, reader: DLFRReader, verbose=True):
    new = reader.poll()
    snaps, zs_new = zonesensor_ingest(cfg, reader)
    data = build_data(cfg, snaps)
    out = expand(cfg["output"])
    write_json_atomic(out, data, compact=True)
    if verbose:
        stamp = datetime.now().strftime("%H:%M:%S")
        for e in (new + zs_new)[:8]:
            print(f"[{stamp}]   + {e['kind']:10} {'/'.join(e['factions']) or '?':22} {e.get('location') or '-':16} "
                  f"dead={e['dead']}  {e['text'][:60]}")
        print(f"[{stamp}] data.json: {len(data['conflicts'])} placed / {len(data['unplaced'])} unplaced events, "
              f"{len(data['groups'])} groups, {len(data['fronts'])} fronts"
              + ("" if data["map"]["calibrated"] else " (map not calibrated yet)"))
    return out


def cmd_run(cfg, once=False, push_every=0.0):
    reader = DLFRReader(cfg)
    files = reader.files()
    print(f"Setup '{cfg['setup']}': {len(files)} DLFR file(s) found; ZoneSensor file "
          f"{'found' if expand(cfg['zonesensor_file']).exists() else 'not found yet'}; output {expand(cfg['output'])}")
    if not files:
        print("  No DLFR files yet - run 'python logger_to_json.py discover' after playing a few minutes with DLFR.")
    if push_every and push_every < 6:
        print("  GitHub Pages rebuilds are rate-limited (about 10 per hour) - using --push-every 6.")
        push_every = 6
    last_push = 0.0
    while True:
        try:
            out = update_once(cfg, reader)
            if push_every and time.time() - last_push >= push_every * 60:
                git_push(out)
                last_push = time.time()
        except Exception as e:
            print(f"[error] {e!r}")
        if once:
            return
        time.sleep(float(cfg["interval_seconds"]))


# =============================================================================
# discovery (Phase 0)
# =============================================================================

def cmd_discover(cfg, watch=0, interval=10):
    lex = make_lexicon(cfg)
    out_dir = HERE / "discovery"
    (out_dir / "samples").mkdir(parents=True, exist_ok=True)
    files, seen = [], set()
    for raw in cfg["dlfr_paths"]:
        p = expand(raw)
        for q in (sorted(p.rglob("*")) if p.is_dir() else [p] if p.is_file() else []):
            if q.is_file() and q not in seen:
                seen.add(q)
                files.append(q)
    cutoff = time.time() - 7 * 86400
    for raw in cfg["discovery_extra_dirs"]:
        root = expand(raw)
        if not root.is_dir():
            continue
        for q in root.rglob("*"):
            rel = q.relative_to(root)
            if len(rel.parts) > 3 or any(x in ("SaveGames", "Crashes", "D3DCache", "webcache") for x in rel.parts):
                continue
            if q.is_file() and q not in seen and q.suffix.lower() in (".json", ".jsonl", ".ini", ".txt", ".log", ".csv", ".cfg"):
                try:
                    if q.stat().st_mtime >= cutoff and q.stat().st_size < 50_000_000:
                        seen.add(q)
                        files.append(q)
                except OSError:
                    pass
    lines = [f"# Discovery report - {datetime.now():%Y-%m-%d %H:%M:%S}", f"Setup: {cfg['setup']}", "", "Searched:"]
    lines += [f"- `{expand(p)}` {'(exists)' if expand(p).exists() else '(NOT FOUND)'}"
              for p in cfg["dlfr_paths"] + cfg["discovery_extra_dirs"]]
    found_pos, found_locs = [], Counter()
    body = []
    for p in files:
        fmt = detect_format(p)
        body.append(f"\n## `{p}`\n- format: {fmt}, size {p.stat().st_size} bytes, modified "
                    f"{datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M:%S}")
        if fmt in ("binary", "unreadable", "empty"):
            continue
        text = read_text(p, 20_000_000)
        toks = Counter(tokens(text))
        facs = Counter(f for t in tokens(text) for f in lex.factions_in(t))
        locs = Counter(n for line in text.splitlines() for n in [lex.location_in(line)] if n)
        coords = [m.group(0) for pat in COORD_PATTERNS for m in pat.finditer(text)][:8]
        caps = Counter(m.group(0) for m in re.finditer(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+){0,2}\b", text))
        known = {k.lower() for k in cfg["factions"]} | {k.lower() for k in lex.locations}
        body.append(f"- factions: {dict(facs.most_common())}")
        body.append(f"- known places: {dict(locs.most_common())}")
        body.append(f"- death words: { {w: toks[w] for w in DEATH_WORDS if toks[w]} }")
        body.append(f"- fight words: { {w: toks[w] for w in FIGHT_WORDS if toks[w]} }")
        body.append(f"- coordinate-like: {coords or 'none'}")
        body.append(f"- other capitalised names (maybe places DLFR uses): "
                    f"{[(k, v) for k, v in caps.most_common(60) if k.lower() not in known][:30]}")
        if coords:
            found_pos.append(str(p))
        found_locs.update(locs)
        if fmt in STRUCTURED:
            try:
                flat = flatten(parse_structured(p, fmt))
                keys = sorted({re.sub(r"/\d+(?=/|$)", "/#", k) for k in flat})[:150]
                body.append(f"- {len(flat)} values; key paths (list indexes shown as #):")
                body += [f"  - `{k}`" for k in keys]
                if any(re.search(r"(^|/)(x|y|location|pos|position)(/|$)", k, re.I) for k in keys):
                    found_pos.append(str(p))
            except Exception as e:
                body.append(f"- parse error: {e!r}")
        else:
            tl = text.splitlines()
            body.append(f"- {len(tl)} lines. First 10 / last 20:\n```")
            body += [l[:300] for l in tl[:10]] + ["..."] + [l[:300] for l in tl[-20:]]
            body.append("```")
        if p.stat().st_size <= 5_000_000:
            try:
                shutil.copy2(p, out_dir / "samples" / re.sub(r"[^\w.\-]+", "_", f"{p.parent.name}_{p.name}"))
            except OSError:
                pass
    lines += ["", "## Verdict", f"- files: {len(files)}", f"- with positions: {sorted(set(found_pos)) or 'none'}",
              f"- known place names seen: {dict(found_locs.most_common()) or 'none'}"]
    if found_pos:
        lines.append("=> DLFR gives positions: calibrate the map and conflicts land exactly.")
    elif found_locs:
        lines.append("=> Place NAMES only: give each name a map position with the page's pick tool + 'place'.")
    else:
        lines.append("=> No positions/known names yet: check 'other capitalised names', add DLFR's place names to "
                     "locations.json; ZoneSensor supplies positions near you.")
    lines += body
    if watch:
        print(f"Watching {len(files)} file(s) for {watch} s - play now (fight, open the PDA Chronicle, sleep, save).")
        before = {str(p): (p.stat().st_mtime, p.stat().st_size) for p in files}
        flat0 = {}
        for p in files:
            fmt = detect_format(p)
            if fmt in STRUCTURED:
                try:
                    flat0[str(p)] = flatten(parse_structured(p, fmt))
                except Exception:
                    pass
        changes = Counter()
        end = time.time() + watch
        last = dict(before)
        while time.time() < end:
            time.sleep(interval)
            for p in files:
                try:
                    cur = (p.stat().st_mtime, p.stat().st_size)
                except OSError:
                    continue
                if cur != last[str(p)]:
                    changes[str(p)] += 1
                    last[str(p)] = cur
                    print(f"  {datetime.now():%H:%M:%S} changed: {p.name} ({cur[1]} bytes)")
        lines += ["", "## Change watch", f"{watch} s, checked every {interval} s"]
        if not changes:
            lines.append("No file changed - DLFR may only write on save/load or PDA open. Try a longer watch.")
        for k, n in changes.most_common():
            p = Path(k)
            lines.append(f"\n### `{k}` changed {n}x")
            if k in flat0:
                try:
                    now_flat = flatten(parse_structured(p, detect_format(p)))
                    diff = [(kk, flat0[k].get(kk), vv) for kk, vv in now_flat.items() if flat0[k].get(kk) != vv]
                    lines += [f"- `{kk}`: {str(a)[:60]!r} -> {str(b)[:60]!r}" for kk, a, b in diff[:60]]
                except Exception as e:
                    lines.append(f"- {e!r}")
            else:
                with open(p, "rb") as f:
                    f.seek(min(before[k][1], p.stat().st_size))
                    new = [l for l in decode_bytes(f.read(200_000)).splitlines() if l.strip()]
                lines += ["```"] + new[-40:] + ["```"]
    report = out_dir / "discovery_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[: lines.index("## Verdict") + 6]))
    print(f"\nFull report: {report}\nSample copies: {out_dir / 'samples'}")


# =============================================================================
# main
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description="Zone heat map logger -> data.json",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--config", help="settings file (default logger_config.json next to this script)")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="update data.json every interval_seconds (default)")
    r.add_argument("--push-every", type=float, default=0, help="git commit+push data.json every N minutes (>= 6)")
    sub.add_parser("once", help="one update, then exit")
    d = sub.add_parser("discover", help="Phase 0 report on what DLFR/mods write")
    d.add_argument("--watch", type=int, default=0)
    d.add_argument("--interval", type=int, default=10)
    c = sub.add_parser("calib-add", help="add a calibration point")
    c.add_argument("name")
    c.add_argument("u", type=float)
    c.add_argument("v", type=float)
    c.add_argument("--world", nargs=2, type=float, metavar=("X", "Y"))
    sub.add_parser("calib-undo")
    sub.add_parser("calib-show")
    pl = sub.add_parser("place", help="set a place name's map position")
    pl.add_argument("name")
    pl.add_argument("u", type=float)
    pl.add_argument("v", type=float)
    args = ap.parse_args()
    cfg = load_config(args.config)
    try:
        if args.cmd in (None, "run"):
            cmd_run(cfg, push_every=getattr(args, "push_every", 0))
        elif args.cmd == "once":
            cmd_run(cfg, once=True)
        elif args.cmd == "discover":
            cmd_discover(cfg, args.watch, args.interval)
        elif args.cmd == "calib-add":
            cmd_calib_add(cfg, args.name, args.u, args.v, args.world)
        elif args.cmd == "calib-undo":
            cmd_calib_undo(cfg)
        elif args.cmd == "calib-show":
            print(calib_report(cfg))
        elif args.cmd == "place":
            cmd_place(cfg, args.name, args.u, args.v)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
