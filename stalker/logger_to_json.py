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
  * Zone Wanderers events: %LOCALAPPDATA%\\Stalker2\\Saved\\ZoneWanderers_Events.txt
     -> battle/losses coordinates. ZoneWanderersAsi.log clash and loss lines supply
     the time, sides, fighters fielded, and losses. Close clashes are merged into one battle.

Writes data.json every 60 s (default). Never writes into the game or mod folders.

COMMANDS (PowerShell, run from the folder with this script):
  python logger_to_json.py                     # run: update data.json every 60 s (Ctrl+C stops)
  python logger_to_json.py run --push-every 10 # also git-push data.json to GitHub Pages every 10 min
  python logger_to_json.py run --push-every 10 --serve 8787   # ...and serve the map + live view on home Wi-Fi
  python logger_to_json.py serve               # only the Wi-Fi server (http://<PC-IP>:8787/)
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
    dlfr_regions.json  region id -> map pixel (DLFR_Chronik; approximate until calibrated)
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
from datetime import datetime, timedelta
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
    "wanderers_events_file": "%LOCALAPPDATA%\\Stalker2\\Saved\\ZoneWanderers_Events.txt",
    "wanderers_log_file": "%LOCALAPPDATA%\\Stalker2\\Saved\\ZoneWanderersAsi.log",
    "zone_messages_dir": "%LOCALAPPDATA%\\Stalker2\\Saved\\ZoneMessagesAsi\\Saves",
    "battle_cluster_m": 200,
    "battle_cluster_s": 90,
    "discovery_extra_dirs": ["%LOCALAPPDATA%\\Stalker2\\Saved"],
    "locations_file": "locations.json",
    "calib_points_file": "calib_points.json",
    "state_dir": "zoneheat_state",
    "keep_hours": 168,
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
        "Ward": ["ward", "varta"],
        "Military": ["military", "militaries", "army"],
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


# Approximate Zone Wanderers metres -> map pixels. THIS IS NOT A CALIBRATED FIT.
# Reuses the two PDA landmarks already used in dlfr_regions.json (Pripyat, SIRCAA).
# Wanderers logs distances in metres; those metres appear to share origin (0, 0)
# with the DLFR cell grid at about 215 m per cell, with Y increasing south
# (down on the PDA image). The 215 m figure comes from lining SIRCAA's grid
# centroid up with a player position at the eastern compound; it will be wrong
# at the edges until calib-add is run. Override any of these keys in
# logger_config.json under "wanderers_map" to recalibrate later, in one place.
# If calib_points.json has 3+ points, metres are converted to UE cm (*100) and
# the affine world-to-pixel fit is used instead.
DEFAULT_WANDERERS_MAP = {
    "approximate": True,
    "cell_size_m": 215.0,
    "y_down": True,
    "pripyat_grid": [12.588, 8.059],
    "pripyat_px": [455.0, 175.0],
    "sircaa_grid": [33.615, 26.615],
    "sircaa_px": [1228.0, 710.0],
    "sx_per_cell": 36.762,
    "sy_per_cell": 28.83077,
    "note": "Approximate axis-aligned linear map from Zone Wanderers metres to PDA-map pixels, via the DLFR grid. Replace by running calib-add, or edit wanderers_map in logger_config.json.",
}

WANDERERS_FIGHT_KINDS = {"battle", "losses"}


def wanderers_map(cfg) -> dict:
    wm = dict(DEFAULT_WANDERERS_MAP)
    user = cfg.get("wanderers_map") or {}
    if isinstance(user, dict):
        wm.update({k: v for k, v in user.items() if not str(k).startswith("_")})
    return wm


def wanderers_to_pixel(cfg, x, y, affine=None):
    """Map Zone Wanderers metres to map pixels (u right, v down).

    Prefer the calibrated affine (world cm) when 3+ calib points exist.
    Otherwise use the approximate linear map in wanderers_map().
    """
    if affine:
        return apply_affine(affine, float(x) * 100.0, float(y) * 100.0)
    wm = wanderers_map(cfg)
    cs = float(wm["cell_size_m"])
    gx, gy = float(x) / cs, float(y) / cs
    pg, pp = wm["pripyat_grid"], wm["pripyat_px"]
    u = float(pp[0]) + float(wm["sx_per_cell"]) * (gx - float(pg[0]))
    v = float(pp[1]) + float(wm["sy_per_cell"]) * (gy - float(pg[1]))
    return u, v


def parse_wanderers_events(path: Path) -> list:
    """Parse ZoneWanderers_Events.txt into session-aware records.

    Line format: id|event|groupId|Faction|X|Y|activity
    IDs restart at 1 on a new game / save / logger reset, and earlier
    sessions stay in the file, so the id alone is not unique. A new
    session starts whenever the id goes down. session_key is a hash of
    that session's first line (stable if the file is only appended).
    """
    if not path.exists():
        return []
    try:
        raw = read_text(path)
    except OSError:
        return []
    records = []
    session_key = None
    prev_id = None
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split("|")
        if len(parts) < 7:
            continue
        try:
            eid = int(parts[0])
            x = float(parts[4])
            y = float(parts[5])
        except (TypeError, ValueError):
            continue
        kind = parts[1].strip()
        group = parts[2].strip()
        faction = parts[3].strip()
        activity = parts[6].strip()
        if prev_id is None or eid < prev_id:
            session_key = hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]
        prev_id = eid
        records.append({
            "eid": eid,
            "kind": kind,
            "group": group,
            "faction": faction,
            "x": x,
            "y": y,
            "activity": activity,
            "session_key": session_key,
            "raw": s,
        })
    return records


def wanderers_ingest(cfg, reader: "DLFRReader"):
    """Turn battle/losses events into fight records and append new ones to history."""
    path = expand(cfg.get("wanderers_events_file") or DEFAULTS["wanderers_events_file"])
    recs = parse_wanderers_events(path)
    if not recs:
        return []
    lex = make_lexicon(cfg)
    now = time.time()
    seen_path = cfg["_state"] / "wanderers_seen.json"
    try:
        seen = json.loads(seen_path.read_text(encoding="utf-8")) if seen_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        seen = {}
    first_seen = seen.get("first_seen") or {}
    events = []
    for r in recs:
        if r["kind"] not in WANDERERS_FIGHT_KINDS:
            continue
        key = f"zw:{r['session_key']}:{r['eid']}"
        if key in first_seen:
            t = float(first_seen[key])
        else:
            t = now
            first_seen[key] = t
        facs = lex.factions_in(r["faction"]) or ([r["faction"]] if r["faction"] else ["Unknown"])
        dead = 1 if r["kind"] == "losses" else 0
        weight = 0.7 if r["kind"] == "losses" else 1.0
        act = r["activity"] or "unknown"
        text = f"{facs[0]} {r['kind']} ({act})"
        events.append({
            "id": key,
            "t": round(t, 1),
            "setup": cfg["setup"],
            "source": "ZoneWanderers",
            "kind": "fight",
            "factions": facs,
            "location": None,
            "x": r["x"],
            "y": r["y"],
            "coord_space": "wanderers_m",
            "dead": dead,
            "weight": weight,
            "text": text[:240],
            "zw_event": r["kind"],
            "activity": act,
            "group_id": r["group"],
            "approximate": calibration(cfg)[0] is None,
        })
    seen["first_seen"] = first_seen
    if len(first_seen) > 20000:
        keep = sorted(first_seen.items(), key=lambda kv: kv[1])[-15000:]
        seen["first_seen"] = dict(keep)
    write_json_atomic(seen_path, seen)
    return reader.add_events(events)


ZW_SPEED_MPS = 1.3
ZW_HEADER_RE = re.compile(r"^===== ZoneWanderersAsi start (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}) =====")
ZW_SETOFF_RE = re.compile(
    r"^(?P<t>\d{2}:\d{2}:\d{2})\s+(?:group|pack) (?P<id>\d+) \((?P<f>[^,]+), (?P<n>\d+)\) sets off:"
    r".*from \((?P<fx>-?[\d.]+), (?P<fy>-?[\d.]+)\) to \((?P<tx>-?[\d.]+), (?P<ty>-?[\d.]+)\)"
)
ZW_ACT_RE = re.compile(r"sets off:\s*([A-Za-z_]+)")
ZW_ARRIV_RE = re.compile(
    r"^(?P<t>\d{2}:\d{2}:\d{2})\s+(?:group|pack) (?P<id>\d+) arrived.*?\((?P<x>-?[\d.]+), (?P<y>-?[\d.]+)"
)
ZW_CLASH_RE = re.compile(
    r"^(?P<t>\d{2}:\d{2}:\d{2})\s+groups (?P<a>\d+) \((?P<af>[^,]+), (?P<an>\d+)\) and "
    r"(?P<b>\d+) \((?P<bf>[^,]+), (?P<bn>\d+)\) clash"
)
ZW_LOST_RE = re.compile(
    r"^(?P<t>\d{2}:\d{2}:\d{2})\s+group (?P<id>\d+) \((?P<f>[^)]+)\) lost "
    r"(?P<lost>\d+) of (?P<of>\d+) \((?P<why>\w+)\)"
)


class _ZwClock:
    """Turn HH:MM:SS log lines into datetimes using the Asi 'start' headers."""

    def __init__(self):
        self.date = None
        self.prev_sec = None

    def header(self, text):
        self.date = datetime.strptime(text, "%d.%m.%Y %H:%M:%S")
        self.prev_sec = self.date.hour * 3600 + self.date.minute * 60 + self.date.second

    def stamp(self, t):
        if self.date is None:
            return None
        h, m, s = map(int, t.split(":"))
        sec = h * 3600 + m * 60 + s
        if self.prev_sec is not None and sec + 120 < self.prev_sec:
            self.date = self.date + timedelta(days=1)
        self.prev_sec = sec
        return datetime(self.date.year, self.date.month, self.date.day, h, m, s)


def parse_wanderers_log(path: Path):
    """Parse ZoneWanderersAsi.log for clashes, losses, and last-known group positions."""
    pos = defaultdict(list)
    clashes, losses = [], []
    if not path.exists():
        return pos, clashes, losses
    try:
        raw = read_text(path)
    except OSError:
        return pos, clashes, losses
    clock = _ZwClock()
    for ln in raw.splitlines():
        mh = ZW_HEADER_RE.match(ln)
        if mh:
            clock.header(mh.group(1))
            continue
        m = ZW_SETOFF_RE.match(ln)
        if m:
            dt = clock.stamp(m["t"])
            if dt:
                pos[m["id"]].append((dt, float(m["fx"]), float(m["fy"]), "from",
                                     float(m["tx"]), float(m["ty"])))
            continue
        m = ZW_ARRIV_RE.match(ln)
        if m:
            dt = clock.stamp(m["t"])
            if dt:
                pos[m["id"]].append((dt, float(m["x"]), float(m["y"]), "arrived", None, None))
            continue
        m = ZW_LOST_RE.match(ln)
        if m:
            dt = clock.stamp(m["t"])
            losses.append({"dt": dt, "id": m["id"], "faction": m["f"],
                           "lost": int(m["lost"]), "of": int(m["of"]), "why": m["why"]})
            continue
        m = ZW_CLASH_RE.match(ln)
        if m:
            dt = clock.stamp(m["t"])
            clashes.append({"dt": dt, "a": m["a"], "af": m["af"], "an": int(m["an"]),
                            "b": m["b"], "bf": m["bf"], "bn": int(m["bn"])})
    clashes.sort(key=lambda c: c["dt"] or datetime.min)
    return pos, clashes, losses


def _zw_last_loc(hist, dt):
    """Position at or before dt; interpolate along the last set-off at 1.3 m/s."""
    cur = None
    pending = None
    for h in hist:
        if dt is not None and h[0] > dt:
            break
        if h[3] == "arrived":
            cur = (h[1], h[2], "arrived")
            pending = None
        elif h[3] == "from":
            cur = (h[1], h[2], "from")
            pending = (h[0], h[1], h[2], h[4], h[5])
    if pending and dt is not None:
        t0, fx, fy, tx, ty = pending
        elapsed = (dt - t0).total_seconds()
        if elapsed > 0 and tx is not None:
            dist = math.hypot(tx - fx, ty - fy)
            if dist > 0:
                travel = min(dist, ZW_SPEED_MPS * elapsed)
                cur = (fx + (tx - fx) * travel / dist,
                       fy + (ty - fy) * travel / dist, "interpolated")
    return cur


def _zw_cluster(clashes, placed, cluster_m, cluster_s):
    n = len(clashes)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        ci = clashes[i]
        if ci["dt"] is None:
            continue
        ids_i = {ci["a"], ci["b"]}
        pi = placed[i]
        for j in range(i + 1, n):
            cj = clashes[j]
            if cj["dt"] is None:
                continue
            gap = (cj["dt"] - ci["dt"]).total_seconds()
            if gap > cluster_s:
                break
            if gap < -cluster_s:
                continue
            close = bool(ids_i & {cj["a"], cj["b"]})
            if (not close) and pi and placed[j]:
                if math.hypot(pi[0] - placed[j][0], pi[1] - placed[j][1]) <= cluster_m:
                    close = True
            if close:
                union(i, j)
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def _zw_canon(lex, name):
    if not name:
        return "Unknown"
    facs = lex.factions_in(name) if lex else None
    return facs[0] if facs else name


def _zw_side_text(s):
    name = s["faction"]
    if s.get("fielded") is not None and s.get("lost") is not None:
        return f"{name} {s['fielded']} (lost {s['lost']})"
    if s.get("fielded") is not None:
        return f"{name} {s['fielded']}"
    if s.get("lost") is not None:
        return f"{name} (lost {s['lost']})"
    return name


def build_wanderers_battles(cfg, lex=None):
    """Merge close Asi.log clashes into battles with sides, counts, and real times.

    Fighter and loss counts come only from clash and 'lost N of M (battle)' lines
    in ZoneWanderersAsi.log. Coordinates prefer matching ZoneWanderers_Events.txt
    rows, then last-known group positions from the same log. Emission losses are
    ignored. Returns (placed, unplaced) event dicts ready for data.json.
    """
    lex = lex or make_lexicon(cfg)
    log_path = expand(cfg.get("wanderers_log_file") or DEFAULTS["wanderers_log_file"])
    ev_path = expand(cfg.get("wanderers_events_file") or DEFAULTS["wanderers_events_file"])
    pos, clashes, losses = parse_wanderers_log(log_path)
    if not clashes:
        return [], []
    events = [r for r in parse_wanderers_events(ev_path) if r["kind"] in WANDERERS_FIGHT_KINDS]
    cluster_m = float(cfg.get("battle_cluster_m") or DEFAULTS["battle_cluster_m"])
    cluster_s = float(cfg.get("battle_cluster_s") or DEFAULTS["battle_cluster_s"])
    event_match_m = cluster_m

    loss_idx = defaultdict(list)
    for L in losses:
        if L["why"] == "battle" and L["dt"] is not None:
            loss_idx[(L["dt"], L["id"])].append(L)

    placed_xy = []
    for c in clashes:
        pts = []
        for gid in (c["a"], c["b"]):
            loc = _zw_last_loc(pos.get(gid, []), c["dt"])
            if loc:
                pts.append(loc)
        if pts:
            placed_xy.append((sum(p[0] for p in pts) / len(pts),
                              sum(p[1] for p in pts) / len(pts)))
        else:
            placed_xy.append(None)

    used = set()
    placed, unplaced = [], []
    for members in _zw_cluster(clashes, placed_xy, cluster_m, cluster_s):
        sides = {}
        dts, pred_pts = [], []
        for i in members:
            c = clashes[i]
            if c["dt"]:
                dts.append(c["dt"])
            if placed_xy[i]:
                pred_pts.append(placed_xy[i])
            for gid, fac, n in ((c["a"], c["af"], c["an"]), (c["b"], c["bf"], c["bn"])):
                rec = sides.setdefault(gid, {"faction": fac, "fielded": n, "lost": 0, "lost_known": False})
                rec["fielded"] = max(rec["fielded"], n)
                for L in loss_idx.get((c["dt"], gid), []):
                    rec["lost"] += L["lost"]
                    rec["lost_known"] = True
                    rec["fielded"] = max(rec["fielded"], L["of"])
        for rec in sides.values():
            if rec["lost_known"]:
                rec["lost"] = min(rec["lost"], rec["fielded"])
        if not dts:
            continue
        dt = min(dts)
        facs_raw = {s["faction"] for s in sides.values()}
        evs = []
        if pred_pts:
            for i, e in enumerate(events):
                if i in used or e["faction"] not in facs_raw:
                    continue
                dmin = min(math.hypot(e["x"] - px, e["y"] - py) for px, py in pred_pts)
                if dmin <= event_match_m:
                    evs.append((dmin, i, e))
        evs.sort()
        picked = []
        if evs:
            anchor = evs[0][2]
            for dmin, i, e in evs:
                if math.hypot(e["x"] - anchor["x"], e["y"] - anchor["y"]) <= 150:
                    picked.append(e)
                    used.add(i)
        if picked:
            x = sum(e["x"] for e in picked) / len(picked)
            y = sum(e["y"] for e in picked) / len(picked)
            pos_source = "events"
            activity = next((e["activity"] for e in picked if e.get("activity")), None)
        elif pred_pts:
            x = sum(p[0] for p in pred_pts) / len(pred_pts)
            y = sum(p[1] for p in pred_pts) / len(pred_pts)
            pos_source = "group_position"
            activity = None
        else:
            x = y = None
            pos_source = None
            activity = None

        by_fac = defaultdict(lambda: {"faction": None, "fielded": 0, "lost": 0, "lost_known": False})
        for s in sides.values():
            canon = _zw_canon(lex, s["faction"])
            # keep mutant species names (lex leaves them unmatched); human aliases are canonical
            name = canon
            b = by_fac[name]
            b["faction"] = name
            b["fielded"] += s["fielded"]
            if s["lost_known"]:
                b["lost"] += s["lost"]
                b["lost_known"] = True
        disp = []
        for b in by_fac.values():
            row = {"faction": b["faction"], "fielded": b["fielded"]}
            if b["lost_known"]:
                row["lost"] = b["lost"]
            disp.append(row)
        text = " vs ".join(_zw_side_text(s) for s in disp)
        fielded = sum(s["fielded"] for s in disp)
        lost = sum(s["lost"] for s in disp if "lost" in s)
        lost_all = all("lost" in s for s in disp)
        weight = max(0.8, lost * 1.0 + fielded * 0.25)
        ev = {
            "id": f"zwb:{dt.isoformat()}:{'-'.join(sorted(sides))}",
            "t": round(dt.timestamp(), 1),
            "setup": cfg["setup"],
            "source": "ZoneWanderers",
            "kind": "fight",
            "factions": [s["faction"] for s in disp],
            "location": None,
            "dead": lost if lost_all or lost else 0,
            "weight": weight,
            "text": text[:240],
            "zw_event": "battle",
            "activity": activity,
            "sides": disp,
            "fielded": fielded,
            "counts_source": "ZoneWanderersAsi.log",
            "pos_source": pos_source,
            "t_source": "log",
        }
        if lost_all or lost:
            ev["lost"] = lost
        if x is not None:
            ev["x"] = x
            ev["y"] = y
            ev["coord_space"] = "wanderers_m"
            ev["approximate"] = calibration(cfg)[0] is None
            placed.append(ev)
        else:
            unplaced.append(ev)
    return placed, unplaced


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
        seen_src = list(self.state.get("seen") or [])
        seen_uniq = []
        seen_set = set()
        for x in seen_src:
            if x not in seen_set:
                seen_set.add(x)
                seen_uniq.append(x)
        self.seen_list = seen_uniq
        self.seen = seen_set
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

    def persist_seen(self):
        self.state["seen"] = self.seen_list[-20000:]
        self.seen_list = self.state["seen"]
        self.seen = set(self.seen_list)
        write_json_atomic(self.state_path, self.state)

    def add_events(self, events):
        new = [e for e in events if e["id"] not in self.seen]
        for e in new:
            if e["id"] not in self.seen:
                self.seen.add(e["id"])
                self.seen_list.append(e["id"])
        if new:
            with open(self.events_path, "a", encoding="utf-8") as f:
                for e in new:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            self.persist_seen()
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
                if p.name.lower() == "dlfr_chronik.txt":
                    ev = self._chronik(p)
                elif fmt in STRUCTURED:
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
        self.state["seen"] = self.seen_list[-20000:]
        self.seen_list = self.state["seen"]
        self.seen = set(self.seen_list)
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


    def _chronik(self, p: Path):
        """Parse DLFR_Chronik.txt in full (not from EOF).

        Line format: seq|tick|EVENT|facA|facB|val1|val2|saveId|regionOr-1
        facA/facB are 0-based indexes into DLFR_Ids.txt. Region -1 is unplaced.
        Ticks are treated as seconds; the file mtime anchors the latest tick of
        the newest playthrough (a big downward tick jump starts a new clock).
        """
        events = parse_dlfr_chronik(
            p,
            ids_path=_sibling(p, "DLFR_Ids.txt"),
            regions_path=_regions_path(self.cfg),
            anchor=p.stat().st_mtime,
        )
        return events


# =============================================================================
# DLFR_Chronik reader (whole-file, region-aware)
# =============================================================================

CHRONIK_EN = {
    "ZWISCHENFALL": "Incident", "KRIEG": "War", "WAFFENSTILLSTAND": "Ceasefire",
    "WAFFENRUHE_ENDE": "Truce ended", "BUENDNIS": "Alliance",
    "BUENDNIS_ABLAUF": "Alliance expired", "BUENDNIS_ENDE": "Alliance ended",
    "MISSTRAUEN": "Distrust", "ENTSPANNUNG": "Easing",
}
CHRONIK_CONFLICTS = {"ZWISCHENFALL", "KRIEG"}
CHRONIK_FACTION_EN = {
    "Varta": "Ward", "Neutrals": "Loners", "Corpus": "Corps", "Militaries": "Military",
}


def _sibling(path: Path, name: str) -> Path | None:
    q = path.with_name(name)
    return q if q.is_file() else None


def _regions_path(cfg) -> Path | None:
    here = Path(cfg.get("_here") or Path(__file__).resolve().parent)
    for q in (here / "dlfr_regions.json", Path(cfg.get("_state", here)) / "dlfr_regions.json"):
        if q.is_file():
            return q
    return None


def load_dlfr_ids(path: Path | None) -> list:
    """Faction names in Chronik index order, already translated to English."""
    if not path or not path.is_file():
        return []
    names = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.rsplit(None, 1)
        raw = parts[0] if parts else line
        names.append(CHRONIK_FACTION_EN.get(raw, raw))
    return names


def load_dlfr_regions(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("regions") or {}
    except Exception:
        return {}


def _chronik_clocks(rows: list) -> list:
    """Split rows into playthroughs when the tick counter resets downward."""
    if not rows:
        return []
    clocks, cur = [], [rows[0]]
    peak = rows[0]["tick"]
    for r in rows[1:]:
        if r["tick"] < peak - 50_000:  # new game / new clock
            clocks.append(cur)
            cur, peak = [r], r["tick"]
        else:
            cur.append(r)
            if r["tick"] > peak:
                peak = r["tick"]
    clocks.append(cur)
    return clocks


def chronik_event_times(rows: list, anchor: float) -> dict:
    """Map (save, seq) -> unix seconds. Latest clock ends at *anchor*; earlier
    clocks end 1 hour before the next clock starts. 1 tick = 1 second."""
    times = {}
    clocks = _chronik_clocks(rows)
    end = float(anchor)
    for clock in reversed(clocks):
        lo = min(r["tick"] for r in clock)
        hi = max(r["tick"] for r in clock)
        for r in clock:
            times[(r["save"], r["seq"])] = end - (hi - r["tick"])
        end = end - (hi - lo) - 3600
    return times


def parse_dlfr_chronik_line(line: str) -> dict | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    p = line.split("|")
    if len(p) < 9:
        return None
    try:
        return {
            "seq": int(p[0]), "tick": int(p[1]), "ev": p[2],
            "a": int(p[3]), "b": int(p[4]), "v1": int(p[5]), "v2": int(p[6]),
            "save": p[7], "region": int(p[8]), "raw": line,
        }
    except ValueError:
        return None


def parse_dlfr_chronik(path: Path, ids_path=None, regions_path=None, anchor=None,
                       types=None) -> list:
    """Read the whole Chronik file and return event dicts for logger_to_json.

    Only KRIEG and ZWISCHENFALL are emitted by default (they feed the heat
    layer). Region -1 or an unknown id leaves the event without x/y so it
    goes to unplaced. Positions come from dlfr_regions.json (approximate).
    """
    wanted = types or CHRONIK_CONFLICTS
    names = load_dlfr_ids(ids_path)
    regions = load_dlfr_regions(regions_path)
    raw = read_text(path)
    rows = []
    for line in raw.splitlines():
        r = parse_dlfr_chronik_line(line)
        if r:
            rows.append(r)
    if not rows:
        return []
    if anchor is None:
        try:
            anchor = path.stat().st_mtime
        except OSError:
            anchor = time.time()
    times = chronik_event_times(rows, anchor)

    def fac(i):
        return names[i] if 0 <= i < len(names) else f"Faction {i}"

    out = []
    for r in rows:
        if r["ev"] not in wanted:
            continue
        fa, fb = fac(r["a"]), fac(r["b"])
        ev_en = CHRONIK_EN.get(r["ev"], r["ev"])
        info = regions.get(str(r["region"])) if r["region"] != -1 else None
        loc = (info.get("name") if info else (f"Region {r['region']}" if r["region"] != -1 else None))
        if r["ev"] == "ZWISCHENFALL":
            dead = max(0, r["v2"])
            weight = float(dead) if dead else 1.0
            text = f"{ev_en}: {fa} vs {fb}" + (f", {dead} dead" if dead else "") + (f", {loc}" if loc else "")
        else:
            dead, weight = 0, 1.0
            text = f"{ev_en}: {fa} vs {fb}" + (f", {loc}" if loc else "")
        ev = {
            "id": f"chronik:{r['save']}:{r['seq']}",
            "t": round(times[(r["save"], r["seq"])], 1),
            "source": "DLFR_Chronik",
            "kind": "chronicle",
            "factions": [fa, fb],
            "location": loc,
            "dead": int(dead),
            "weight": round(weight, 2),
            "text": text[:240],
            "region": r["region"],
        }
        if info and info.get("u") is not None and info.get("v") is not None:
            # keep the raw region centre here; build_data jitters by place name
            # when only a location is set. Also store a hint pixel so a later
            # path can use it if locations.json has no entry yet.
            ev["u_hint"] = info["u"]
            ev["v_hint"] = info["v"]
        out.append(ev)
    return out



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
    seen_ids = set()
    unique_events = []
    for e in events:
        eid = e.get("id")
        if eid and eid in seen_ids:
            continue
        if eid:
            seen_ids.add(eid)
        unique_events.append(e)
    events = unique_events
    zw_battles, zw_unplaced = build_wanderers_battles(cfg)
    use_merged = bool(zw_battles or zw_unplaced)
    if use_merged:
        events = [e for e in events if e.get("source") != "ZoneWanderers"]
        events.extend(zw_battles)
        events.extend(zw_unplaced)
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
        if e.get("x") is not None and e.get("y") is not None and e.get("coord_space") == "wanderers_m":
            uv = wanderers_to_pixel(cfg, e["x"], e["y"], m)
        elif e.get("x") is not None and e.get("y") is not None and m:
            uv = apply_affine(m, float(e["x"]), float(e["y"]))
        elif e.get("location") and loc_px(e["location"]):
            px = loc_px(e["location"])
            dx, dy = jitter(e.get("id"), jit)
            uv = (px[0] + dx, px[1] + dy)
        elif e.get("u_hint") is not None and e.get("v_hint") is not None:
            dx, dy = jitter(e.get("id"), jit)
            uv = (float(e["u_hint"]) + dx, float(e["v_hint"]) + dy)
        row = {"t": round(t, 1), "kind": e.get("kind", "fight"), "factions": facs, "location": e.get("location"),
               "dead": int(e.get("dead") or 0), "text": (e.get("text") or "")[:160], "source": e.get("source")}
        if e.get("zw_event"):
            row["zw_event"] = e["zw_event"]
        if e.get("activity"):
            row["activity"] = e["activity"]
        if e.get("sides"):
            row["sides"] = e["sides"]
        if e.get("fielded") is not None:
            row["fielded"] = e["fielded"]
        if e.get("lost") is not None:
            row["lost"] = e["lost"]
        if e.get("counts_source"):
            row["counts_source"] = e["counts_source"]
        if e.get("pos_source"):
            row["pos_source"] = e["pos_source"]
        if e.get("t_source"):
            row["t_source"] = e["t_source"]
        # only mark fight positions approximate when no calib_points affine exists
        if e.get("coord_space") == "wanderers_m" and not m:
            row["approximate"] = True
        if uv:
            row.update(u=round(uv[0], 1), v=round(uv[1], 1), w=float(e.get("weight") or 1.0))
            conflicts.append(row)
        else:
            unplaced.append(row)
    conflicts.sort(key=lambda r: float(r.get("t") or 0))
    conflicts = conflicts[-int(cfg["max_conflicts"]):]
    unplaced.sort(key=lambda r: float(r.get("t") or 0))

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
                "labels": {k: v["pixel"][:2] for k, v in locs.items() if v.get("pixel")},
                "wanderers_approximate": not bool(m),
                "wanderers_note": (None if m else wanderers_map(cfg).get("note"))},
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
    kw = {"cwd": str(repo), "capture_output": True, "text": True, "timeout": 180}
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
    zw_new = wanderers_ingest(cfg, reader)
    data = build_data(cfg, snaps)
    out = expand(cfg["output"])
    write_json_atomic(out, data, compact=True)
    if verbose:
        stamp = datetime.now().strftime("%H:%M:%S")
        for e in (new + zs_new + zw_new)[:8]:
            print(f"[{stamp}]   + {e['kind']:10} {'/'.join(e['factions']) or '?':22} {e.get('location') or '-':16} "
                  f"dead={e['dead']}  {e['text'][:60]}")
        fights = sum(1 for c in data["conflicts"] if c.get("kind") == "fight" and c.get("source") == "ZoneWanderers")
        print(f"[{stamp}] data.json: {len(data['conflicts'])} placed / {len(data['unplaced'])} unplaced events, "
              f"{fights} wanderers battles, {len(data['groups'])} groups, {len(data['fronts'])} fronts"
              + ("" if data["map"]["calibrated"] else " (map not calibrated yet)")
              + (" (fight positions approximate)" if data["map"].get("wanderers_approximate") and fights else ""))
    return out


def cmd_run(cfg, once=False, push_every=0.0, serve=0, serve_dir=None):
    reader = DLFRReader(cfg)
    if serve and not once:
        start_live_server(cfg, serve, serve_dir)
    files = reader.files()
    zw = expand(cfg.get("wanderers_events_file") or DEFAULTS["wanderers_events_file"])
    zwlog = expand(cfg.get("wanderers_log_file") or DEFAULTS["wanderers_log_file"])
    print(f"Setup '{cfg['setup']}': {len(files)} DLFR file(s) found; ZoneSensor file "
          f"{'found' if expand(cfg['zonesensor_file']).exists() else 'not found yet'}; "
          f"Wanderers events {'found' if zw.exists() else 'not found yet'}; "
          f"Wanderers log {'found' if zwlog.exists() else 'not found yet'}; output {expand(cfg['output'])}")
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
# live view: tail the Zone Wanderers files and serve /live.json over home Wi-Fi
# =============================================================================

LIVE_ACTIVE_S = 180      # a fight counts as "active" this long after its last clash/loss line
LIVE_WINDOW_S = 300      # ended fights stay in live.json this long (the page fades them out)
LIVE_GROUP_MAX_AGE_S = 1800
LIVE_CACHE_S = 2.0
LIVE_SERVE_EXT = {".html", ".htm", ".json", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".ico",
                  ".js", ".css", ".txt", ".webmanifest"}


class _Tail:
    """Incremental reader for an append-only log.

    First open seeks to the last `backfill` bytes (no full reread of a big file).
    Afterwards only new bytes are read. Truncation (size smaller than our offset)
    or replacement (different file id) restarts from the top of the new file.
    A missing file just returns nothing. Partial last lines are held back until
    their newline arrives. UTF-8 and UTF-16 LE (BOM) are handled.
    """

    def __init__(self, path: Path, backfill=512 * 1024, max_read=8 * 1024 * 1024):
        self.path = Path(path)
        self.backfill = backfill
        self.max_read = max_read
        self.pos = None
        self.ident = None
        self.buf = b""
        self.utf16 = False
        self.mtime = None
        self.size = 0
        self.head = b""     # first bytes of the file (a rewrite changes them)
        self.mark = b""     # the last bytes we consumed (an in-place rewrite changes them)

    def _ident(self, st):
        return (getattr(st, "st_ino", 0), getattr(st, "st_dev", 0))

    def read_new(self):
        """Return (lines, reset). reset=True when the file was (re)opened from scratch."""
        try:
            st = os.stat(self.path)
        except OSError:
            return [], False
        self.mtime, self.size = st.st_mtime, st.st_size
        ident = self._ident(st)
        reset = False
        skip_partial = False
        rewritten = False
        if self.pos is not None and ident == self.ident and st.st_size >= self.pos:
            try:
                with open(self.path, "rb") as f:
                    head = f.read(len(self.head)) if self.head else b""
                    mark = b""
                    if self.mark:
                        f.seek(max(0, self.pos - len(self.mark)))
                        mark = f.read(len(self.mark))
                rewritten = (self.head and head != self.head) or (self.mark and mark != self.mark)
            except OSError:
                return [], False
        if self.pos is None or ident != self.ident or st.st_size < self.pos or rewritten:
            first = self.pos is None
            reset = True
            self.ident = ident
            self.buf = b""
            try:
                with open(self.path, "rb") as f:
                    self.head = f.read(64)
            except OSError:
                return [], False
            head = self.head[:4]
            self.utf16 = head.startswith(b"\xff\xfe") or (len(head) >= 4 and head[1] == 0 and head[3] == 0)
            start = max(0, st.st_size - self.backfill) if (first or st.st_size > self.backfill) else 0
            if self.utf16:
                start -= start % 2
                start = max(start, 2 if head.startswith(b"\xff\xfe") else 0)
            elif start == 0 and head.startswith(b"\xef\xbb\xbf"):
                start = 3
            skip_partial = start > 3
            self.pos = start
        if st.st_size == self.pos:
            return [], reset
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(min(st.st_size - self.pos, self.max_read))
        except OSError:
            return [], reset
        self.pos += len(data)
        self.mark = data[-32:]
        data = self.buf + data
        if self.utf16:
            if len(data) % 2:
                data, self.buf = data[:-1], data[-1:]
            else:
                self.buf = b""
            text = data.decode("utf-16-le", errors="replace")
            nl = text.rfind("\n")
            if nl < 0:
                self.buf = data + self.buf
                return [], reset
            rest = text[nl + 1:]
            self.buf = rest.encode("utf-16-le") + self.buf
            lines = text[:nl].split("\n")
        else:
            nl = data.rfind(b"\n")
            if nl < 0:
                self.buf = data
                return [], reset
            self.buf = data[nl + 1:]
            lines = [l.decode("utf-8", errors="replace") for l in data[:nl].split(b"\n")]
        if skip_partial and lines:
            lines = lines[1:]
        return [l.rstrip("\r") for l in lines], reset


def _scan_last_header(path: Path, marker=b"ZoneWanderersAsi start", chunk=1024 * 1024, limit=64 * 1024 * 1024):
    """Find the newest session header by reading backwards in chunks (one-time, on open)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            end = size
            carry = b""
            while end > 0 and size - end < limit:
                start = max(0, end - chunk)
                f.seek(start)
                block = f.read(end - start) + carry
                i = block.rfind(marker)
                if i < 0:
                    i16 = block.rfind(marker.decode().encode("utf-16-le"))
                    if i16 >= 0:
                        seg = block[max(0, i16 - 20):i16 + 120].decode("utf-16-le", errors="ignore")
                        m = re.search(r"start (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2})", seg)
                        if m:
                            return datetime.strptime(m.group(1), "%d.%m.%Y %H:%M:%S")
                else:
                    seg = block[i:i + 60].decode("utf-8", errors="ignore")
                    m = re.search(r"start (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2})", seg)
                    if m:
                        return datetime.strptime(m.group(1), "%d.%m.%Y %H:%M:%S")
                carry = block[:200]
                end = start
    except OSError:
        pass
    return None


def lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.255.255", 1))  # no packet is sent; picks the LAN interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class LiveTracker:
    """Keeps a short rolling picture of current Zone Wanderers fights and group positions."""

    def __init__(self, cfg, active_s=LIVE_ACTIVE_S, window_s=LIVE_WINDOW_S, serve_dir: Path | None = None):
        import threading
        from collections import deque
        self.cfg = cfg
        self.lex = make_lexicon(cfg)
        self.active_s = float(active_s)
        self.window_s = float(window_s)
        self.cluster_m = float(cfg.get("battle_cluster_m") or DEFAULTS["battle_cluster_m"])
        self.cluster_s = float(cfg.get("battle_cluster_s") or DEFAULTS["battle_cluster_s"])
        self.ev_path = expand(cfg.get("wanderers_events_file") or DEFAULTS["wanderers_events_file"])
        self.log_path = expand(cfg.get("wanderers_log_file") or DEFAULTS["wanderers_log_file"])
        self.ev_tail = _Tail(self.ev_path, backfill=256 * 1024)
        self.log_tail = _Tail(self.log_path, backfill=1024 * 1024)
        self.m, _ = calibration(cfg)
        self.ppm = pixels_per_meter(self.m) if self.m else 0.175
        (self.map_w, self.map_h), _ = map_size(cfg)
        self.pois = []
        for cand in [serve_dir / "pois.json" if serve_dir else None, HERE / "pois.json"]:
            if cand and cand.exists():
                try:
                    self.pois = [p for p in json.loads(cand.read_text(encoding="utf-8")).get("pois", [])
                                 if p.get("u") is not None and p.get("name")]
                    break
                except (OSError, ValueError, AttributeError):
                    pass
        self.lock = threading.Lock()
        self.cache_t = 0.0
        self.cache = None
        keep = int(self.window_s + self.cluster_s * 3 + 600)
        self.keep_s = keep
        self.clashes = deque(maxlen=5000)
        self.losses = deque(maxlen=10000)
        self.setoff = {}      # gid -> (t, fx, fy, tx, ty, faction, n)
        self.arrived = {}     # gid -> (t, x, y)
        self.ev_rows = deque(maxlen=4000)   # recent Events.txt rows (file order)
        self.ev_group = {}    # gid -> latest row
        self.prev_eid = None
        self.session_start = None   # epoch of the newest Asi header
        self.zm = ZoneMessagesReader(cfg, self)
        self.activity = {}    # gid -> (t, activity word from the 'sets off: <activity>' line)

    # ---- time helpers --------------------------------------------------
    def _line_time(self, hms: str, now: float) -> float | None:
        try:
            h, mi, s = map(int, hms.split(":"))
        except ValueError:
            return None
        today = datetime.fromtimestamp(now)
        dt = today.replace(hour=h, minute=mi, second=s, microsecond=0)
        t = dt.timestamp()
        if t > now + 300:
            t -= 86400
        return t

    # ---- ingest --------------------------------------------------------
    def _ingest_log(self, now):
        lines, reset = self.log_tail.read_new()
        if reset:
            self.clashes.clear()
            self.losses.clear()
            self.setoff.clear()
            self.arrived.clear()
            hdr = _scan_last_header(self.log_path)
            self.session_start = hdr.timestamp() if hdr else None
        for ln in lines:
            mh = ZW_HEADER_RE.match(ln)
            if mh:
                try:
                    self.session_start = datetime.strptime(mh.group(1), "%d.%m.%Y %H:%M:%S").timestamp()
                except ValueError:
                    pass
                continue
            if len(ln) < 10 or ln[2] != ":":
                continue
            t = self._line_time(ln[:8], now)
            if t is None:
                continue
            if self.session_start and t < self.session_start - 60:
                continue  # line from an older session (or a misdated one) - not live
            m = ZW_CLASH_RE.match(ln)
            if m:
                self.clashes.append({"t": t, "dt": datetime.fromtimestamp(t), "a": m["a"], "af": m["af"],
                                     "an": int(m["an"]), "b": m["b"], "bf": m["bf"], "bn": int(m["bn"])})
                continue
            m = ZW_LOST_RE.match(ln)
            if m:
                L = {"t": t, "id": m["id"], "faction": m["f"], "lost": int(m["lost"]), "of": int(m["of"]),
                     "why": m["why"]}
                self.losses.append(L)
                if L["lost"] >= L["of"]:
                    self.setoff.pop(m["id"], None)
                    self.arrived.pop(m["id"], None)
                continue
            m = ZW_SETOFF_RE.match(ln)
            if m:
                self.setoff[m["id"]] = (t, float(m["fx"]), float(m["fy"]), float(m["tx"]), float(m["ty"]),
                                        m["f"], int(m["n"]))
                ma = ZW_ACT_RE.search(ln)
                self.activity[m["id"]] = (t, ma.group(1).lower() if ma else None)
                self.arrived.pop(m["id"], None)
                continue
            m = ZW_ARRIV_RE.match(ln)
            if m:
                self.arrived[m["id"]] = (t, float(m["x"]), float(m["y"]))
        cut = now - self.keep_s
        while self.clashes and self.clashes[0]["t"] < cut:
            self.clashes.popleft()
        while self.losses and self.losses[0]["t"] < cut:
            self.losses.popleft()

    def _ingest_events(self, now):
        lines, reset = self.ev_tail.read_new()
        first_fill = reset
        if reset:
            self.ev_rows.clear()
            self.ev_group.clear()
            self.prev_eid = None
        for ln in lines:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            p = s.split("|")
            if len(p) < 7:
                continue
            try:
                eid = int(p[0])
                x = float(p[4])
                y = float(p[5])
            except ValueError:
                continue
            if self.prev_eid is not None and eid < self.prev_eid:
                # new game session: positions from the old one no longer apply
                self.ev_rows.clear()
                self.ev_group.clear()
            self.prev_eid = eid
            row = {"eid": eid, "kind": p[1].strip(), "gid": p[2].strip(), "faction": p[3].strip(),
                   "x": x, "y": y, "act": p[6].strip(), "seen": None if first_fill else now}
            self.ev_rows.append(row)
            self.ev_group[row["gid"]] = row

    # ---- positions -----------------------------------------------------
    def _group_pos(self, gid, at):
        """Best estimate of a group's position at time `at` (epoch), metres."""
        so = self.setoff.get(gid)
        ar = self.arrived.get(gid)
        if ar and (not so or ar[0] >= so[0]) and ar[0] <= at + 5:
            return ar[1], ar[2]
        if so and so[0] <= at + 5:
            t0, fx, fy, tx, ty = so[:5]
            dist = math.hypot(tx - fx, ty - fy)
            if dist <= 0:
                return fx, fy
            travel = min(dist, ZW_SPEED_MPS * max(0.0, at - t0))
            return fx + (tx - fx) * travel / dist, fy + (ty - fy) * travel / dist
        r = self.ev_group.get(gid)
        if r:
            return r["x"], r["y"]
        return None

    def _event_pos(self, c):
        """Events.txt battle/losses row for this clash's groups (newest first, time-plausible)."""
        ids = {c["a"], c["b"]}
        for r in reversed(self.ev_rows):
            if r["kind"] not in WANDERERS_FIGHT_KINDS or r["gid"] not in ids:
                continue
            if r["seen"] is not None and abs(r["seen"] - c["t"]) > 150:
                continue
            return r["x"], r["y"]
        return None

    def _uv(self, x, y):
        u, v = wanderers_to_pixel(self.cfg, x, y, self.m)
        return round(u, 1), round(v, 1)

    def _poi(self, u, v):
        best, bd = None, float("inf")
        for p in self.pois:
            d = math.hypot(u - float(p["u"]), v - float(p["v"]))
            if d < bd:
                best, bd = p, d
        if not best:
            return None
        return {"name": best["name"], "dist_m": round(bd / self.ppm), "theater": best.get("theater")}

    def _cluster(self, cs, pts):
        """Live grouping: the same groups still trading fire (gaps up to active_s) stay one
        fight; different groups merge only when close in space and time (battle_cluster_*)."""
        n = len(cs)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(n):
            for j in range(i + 1, n):
                gap = abs(cs[j]["t"] - cs[i]["t"])
                same = bool({cs[i]["a"], cs[i]["b"]} & {cs[j]["a"], cs[j]["b"]})
                near = (pts[i] and pts[j] and gap <= self.cluster_s and
                        math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]) <= self.cluster_m)
                if (same and gap <= self.active_s) or near:
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[rj] = ri
        out = defaultdict(list)
        for i in range(n):
            out[find(i)].append(i)
        return list(out.values())

    # ---- build ---------------------------------------------------------
    def build(self, now=None):
        now = time.time() if now is None else now
        self._ingest_log(now)
        self._ingest_events(now)
        horizon = now - (self.window_s + self.cluster_s * 2)
        recent = [c for c in self.clashes if c["t"] >= horizon]
        pts = []
        for c in recent:
            p = self._event_pos(c)
            src = "events"
            if not p:
                g = [q for q in (self._group_pos(c["a"], c["t"]), self._group_pos(c["b"], c["t"])) if q]
                p = (sum(q[0] for q in g) / len(g), sum(q[1] for q in g) / len(g)) if g else None
                src = "group_position" if p else None
            c["xy"], c["pos_source"] = p, src
            pts.append(p)
        fights = []
        for members in self._cluster(recent, pts):
            cs = [recent[i] for i in members]
            start = min(c["t"] for c in cs)
            last = max(c["t"] for c in cs)
            gids = {}
            for c in cs:
                for gid, fac, n in ((c["a"], c["af"], c["an"]), (c["b"], c["bf"], c["bn"])):
                    rec = gids.setdefault(gid, {"faction": fac, "fielded": n, "lost": 0})
                    rec["fielded"] = max(rec["fielded"], n)
            for L in self.losses:
                if L["why"] != "battle" or L["id"] not in gids:
                    continue
                if start - 5 <= L["t"] <= last + self.cluster_s:
                    rec = gids[L["id"]]
                    rec["lost"] += L["lost"]
                    rec["fielded"] = max(rec["fielded"], L["of"])
                    last = max(last, L["t"])
            age = now - last
            if age > self.window_s:
                continue
            xy = [c["xy"] for c in cs if c["xy"]]
            by_fac = {}
            for g in gids.values():
                name = _zw_canon(self.lex, g["faction"])
                b = by_fac.setdefault(name, {"faction": name, "fielded": 0, "lost": 0, "groups": 0})
                b["fielded"] += g["fielded"]
                b["lost"] += min(g["lost"], g["fielded"])
                b["groups"] += 1
            sides = sorted(by_fac.values(), key=lambda s: -s["fielded"])
            first = min(cs, key=lambda c: c["t"])
            f = {
                "id": f"live:{int(first['t'])}:{'-'.join(sorted((first['a'], first['b'])))}",
                "start": round(start, 1), "last": round(last, 1),
                "age_s": round(age, 1), "elapsed_s": round(now - start, 1),
                "active": age <= self.active_s,
                "sides": sides,
                "fielded": sum(s["fielded"] for s in sides),
                "lost": sum(s["lost"] for s in sides),
                "clashes": len(cs),
                "text": " vs ".join(f"{s['faction']} {s['fielded']}" for s in sides),
                "group_ids": sorted(gids),
            }
            if xy:
                x = sum(p[0] for p in xy) / len(xy)
                y = sum(p[1] for p in xy) / len(xy)
                u, v = self._uv(x, y)
                f.update(x=round(x, 1), y=round(y, 1), u=u, v=v,
                         pos_source="events" if any(c["pos_source"] == "events" for c in cs) else "group_position")
                poi = self._poi(u, v)
                if poi:
                    f["near"] = poi
            fights.append(f)
        fights.sort(key=lambda f: (not f["active"], -f["last"]))

        groups = []
        fighting = {g for f in fights if f.get("active") for g in (f.get("group_ids") or [])}
        for gid in set(self.setoff) | set(self.arrived) | set(self.ev_group):
            so, ar, er = self.setoff.get(gid), self.arrived.get(gid), self.ev_group.get(gid)
            t_known = max([q for q in (so[0] if so else None, ar[0] if ar else None,
                                       er["seen"] if er else None) if q is not None], default=None)
            if t_known is None or now - t_known > LIVE_GROUP_MAX_AGE_S:
                continue
            p = self._group_pos(gid, now)
            if not p:
                continue
            u, v = self._uv(*p)
            fac = (so[5] if so else None) or (er["faction"] if er else None) or "Unknown"
            moving = bool(so and (not ar or so[0] > ar[0]) and
                          ZW_SPEED_MPS * (now - so[0]) < math.hypot(so[3] - so[1], so[4] - so[2]))
            act_t, act = self.activity.get(gid, (None, None))
            if er and er.get("act") and (act_t is None or (er["seen"] or 0) >= act_t):
                act = er["act"].lower()
            g = {"id": gid, "faction": _zw_canon(self.lex, fac), "n": so[6] if so else None,
                 "u": u, "v": v, "moving": moving, "age_s": round(now - t_known), "activity": act,
                 "fighting": gid in fighting}
            if so and moving:
                du, dv = self._uv(so[3], so[4])
                g["dest"] = {"u": du, "v": dv}
                poi = self._poi(du, dv)
                if poi:
                    g["dest"]["near"] = poi
            poi = self._poi(u, v)
            if poi:
                g["near"] = poi
            groups.append(g)
        groups.sort(key=lambda g: g["age_s"])
        log_age = (now - self.log_tail.mtime) if self.log_tail.mtime else None
        ev_age = (now - self.ev_tail.mtime) if self.ev_tail.mtime else None
        ages = [a for a in (log_age, ev_age) if a is not None]
        try:
            zm_info, messages, alerts = self.zm.build(now, fights)
        except Exception as e:  # messages are optional; never break the fight feed
            print(f"[zm] messages skipped: {e!r}")
            zm_info, messages, alerts = {"found": False, "error": type(e).__name__}, [], []
        if zm_info.get("file_age_s") is not None:
            ages.append(zm_info["file_age_s"])
        return {
            "schema": "zoneheat-live/2",
            "now": round(now, 1),
            "setup": self.cfg.get("setup"),
            "active_s": self.active_s,
            "window_s": self.window_s,
            "files": {"log_found": self.log_tail.mtime is not None, "events_found": self.ev_tail.mtime is not None,
                      "log_age_s": None if log_age is None else round(log_age),
                      "events_age_s": None if ev_age is None else round(ev_age)},
            "game_active": bool(ages) and min(ages) < 600,
            "map": {"width": self.map_w, "height": self.map_h, "calibrated": bool(self.m)},
            "fights": fights,
            "groups": groups[:400],
            "messages_info": zm_info,
            "messages": messages,
            "alerts": alerts,
        }

    def snapshot_bytes(self) -> bytes:
        with self.lock:
            now = time.time()
            if self.cache is None or now - self.cache_t >= LIVE_CACHE_S:
                try:
                    data = self.build(now)
                except Exception as e:  # never take the server down over a parse problem
                    data = {"schema": "zoneheat-live/2", "now": round(now, 1), "error": repr(e)[:300],
                            "fights": [], "groups": [], "game_active": False}
                self.cache = json.dumps(data, separators=(",", ":")).encode("utf-8")
                self.cache_t = now
            return self.cache


# =============================================================================
# Zone Messages (Danyo123, ZoneMessagesAsi) radio history -> live messages + alerts
# =============================================================================
# %LOCALAPPDATA%\Stalker2\Saved\ZoneMessagesAsi\Saves\<playtime_s>\History.txt, newest line first:
#   id|playtime_s|?(always 1)|hour|minute|kind|variant|speaker_id|speaker_fac|subject_id|fac_a|fac_b|
#   place_id|?|area?|extra|worldX_cm|worldY_cm [|rNNN reply-to] [|zNNN wanderer group] [|dNNN]
# Zone-wide lines (dlfr_*, emi_*, fl_*) stop after `extra` (16 fields, no coordinates).
# World coordinates are UE centimetres (= Zone Wanderers metres x 100).

# Field meanings (checked over all 300 sample lines, with Gray's decode /workspace/stalker2/zm_decode/zm_fields.json):
#   f2 constant 1 (not the day) | f7 speaker id (0 system, 4294967294 player, zw_* = ZW group id)
#   f9 subject id (the dead, or the one in trouble) | f10 fac_a = subject's / own side | f11 fac_b = other side
#   (enemy / attacker / killer; >=100 = a mutant type) | f12 place id | f13 unknown | f14 region id | f15 extra
#   (reward on bitte, 1256 on emi_da) | tags rNNN reply-to, zNNN ZW group, dNNN (bitte only, unknown)
ZM_FACTIONS_DEFAULT = {   # Zone Messages' own faction indices (not DLFR ids); Gray + Claude + Grok agree on all
    # (/workspace/stalker2/zm_decode/factions.md). A trailing "?" = low-medium confidence.
    0: "Loners",          # ZW group 1465 'Neutrals' speaks as faction 0
    1: "Freedom",         # high-ish (was shown as "Duty?")
    2: "Duty",            # medium-high
    3: "Bandits",         # kanal_bandits, the player's own channel (Kai plays Bandits), Sultansk
    4: "Military?",       # low-medium (4 vs 7 is the weakest split)
    5: "Ward",            # high
    6: "Spark",           # medium
    7: "Mercenaries?",    # low-medium
    8: "Scientists",      # all 'wissenschaft' lines, Malachite
    9: "Corpus",          # high
    10: "Noon",           # medium-high
    11: "Monolith?",      # low-medium
}                         # anything else shows as "Unknown (N)" (override: zone_messages_factions)
ZM_AREAS = {0: "Lesser Zone", 1: "Garbage", 2: "Chemical Plant", 3: "Wild Island", 4: "Zaton", 5: "Swamps",
            6: "SIRCAA",   # loc_07 (Orange/Square, /workspace/stalker_qa_orange/f14_6/findings.md)
            7: "Cooling Towers", 8: "Malachite", 9: "Chemical Plant", 10: "Rostok", 11: "Yantar", 12: "Orbita",
            13: "Cordon", 15: "Yaniv", 16: "Jupiter", 18: "Iron Forest", 21: "Cement Factory"}   # f14, "likely"

# kind -> (label, level, category). level 3 = alert, 2 = notable, 1 = info, 0 = chatter
ZM_KINDS = {
    "gefecht": ("Firefight reported", 3, "fight"),
    "zw_gefecht": ("Wanderer group in a fight", 3, "fight"),
    "notruf": ("Distress call", 3, "distress"),
    "todnetz": ("Death on the network", 3, "death"),
    "leiche": ("Body found", 2, "death"),
    "augenzeuge": ("Eyewitness: saw someone die", 2, "death"),
    "mutanten": ("Mutants sighted", 2, "mutant"),
    "feind": ("Enemy spotted", 2, "enemy"),
    "gehoert": ("Gunfire heard", 2, "fight"),
    "aktivitaet": ("Activity reported", 1, "info"),
    "bitte": ("Request for help (errand)", 1, "job"),
    "wissenschaft": ("Science report", 0, "chatter"),
    "geschwaetz": ("Chatter", 0, "chatter"),
    "geruecht": ("Rumour", 0, "chatter"),
    "uhr_morgen": ("Morning check-in", 0, "chatter"), "uhr_tag": ("Midday check-in", 0, "chatter"),
    "uhr_abend": ("Evening check-in", 0, "chatter"), "uhr_nacht": ("Night check-in", 0, "chatter"),
    "wetter_klar": ("Weather: clear", 0, "weather"), "wetter_wolken": ("Weather: clouding over", 0, "weather"),
    "wetter_nebel": ("Weather: fog", 0, "weather"), "wetter_vorboten": ("Weather: storm signs", 1, "weather"),
    "emi_da": ("Emission starting!", 3, "emission"), "emi_reaktion": ("Emission chatter", 0, "emission"),
    "emi_weg": ("Emission warning", 3, "emission"),   # comes ~185 s BEFORE emi_da, so it is the warning
    "zw_ankunft": ("Wanderer group arrived", 1, "wanderers"),
    "zw_aufbruch": ("Wanderer group set off", 1, "wanderers"),
    "fl_annahme": ("Faction job accepted", 1, "job"), "fl_abbruch": ("Faction job abandoned", 1, "job"),
    "fl_fertig": ("Faction job done", 1, "job"), "fl_zurueck": ("Faction job: report back", 1, "job"),
    "dlfr_krieg": ("War declared", 3, "war"), "dlfr_waffenstillstand": ("Ceasefire", 3, "peace"),
    "dlfr_ruhe_ende": ("Truce over", 2, "war"), "dlfr_misstrauen": ("Distrust grows", 2, "war"),
    "dlfr_entspannung": ("Tensions easing", 2, "peace"), "dlfr_buendnis": ("Alliance", 2, "peace"),
    "dlfr_reaktion_krieg": ("Reaction to the war", 0, "chatter"),
    "dlfr_reaktion_frieden": ("Reaction to the peace", 0, "chatter"),
    "antw_zustimmung": ("Reply: agrees", 0, "reply"), "antw_trauer": ("Reply: mourns", 0, "reply"),
    "antw_dank": ("Reply: thanks", 0, "reply"), "antw_warnung": ("Reply: warns", 0, "reply"),
    "antw_plauder": ("Reply: chats", 0, "reply"), "antw_haeme": ("Reply: gloats", 0, "reply"),
    "antw_konter": ("Reply: snaps back", 0, "reply"), "antw_kanal": ("Reply on the channel", 0, "reply"),
    "skif_kanal_a": ("Your answer on the faction channel", 0, "reply"),
}
# banners: these always; dlfr_krieg / dlfr_waffenstillstand only when the player's faction (3) is involved;
# todnetz only when linked to a live fight or 2+ within 200 m / 120 s; emi_da once per emission
ZM_TOAST = {"gefecht", "zw_gefecht", "notruf", "emi_weg", "emi_da"}
ZM_TOAST_PLAYER_FAC = 3
ZM_TOAST_MIN_GAP_S, ZM_TOAST_PER_MIN, ZM_TOAST_COOLDOWN_S = 20, 3, 180
# message -> fight distance gates (m); default ZM_LINK_M. Reject a different region unless within 100 m.
ZM_LINK_GATE = {"todnetz": 75, "gefecht": 200, "notruf": 200, "zw_gefecht": 200, "feind": 300, "mutanten": 300,
                "augenzeuge": 300, "gehoert": 500}
ZM_LINK_SAME_REGION_M = 100
ZM_ALERT_S = 600          # an important message counts as "happening now" this long (real seconds)
ZM_LINK_M = 300           # message <-> live fight: this close in metres ...
ZM_LINK_BEFORE_S, ZM_LINK_AFTER_S = 180, 240   # ... and within this time of the fight
ZM_CLUSTER_M, ZM_CLUSTER_S, ZM_CLUSTER_N = 200, 180, 3   # Square: report pairs are 3-51 m or 290 m+ apart
ZM_CLUSTER_KINDS = {"gefecht", "zw_gefecht", "todnetz", "leiche", "augenzeuge", "notruf"}
# radio-reported fights: a firefight / distress report (or 2+ deaths within 200 m / 120 s) with no logger fight nearby
ZM_RADIO_KINDS = {"gefecht", "zw_gefecht", "notruf"}
ZM_RADIO_MERGE_M, ZM_RADIO_MERGE_S = 200, 180   # further reports this close join the same radio fight
ZM_RADIO_EXPIRE_S = 300                          # ... which is dropped this long after its last report
ZM_RADIO_ACTIVE_S = 180


ZM_FIELDS_FACTIONS = {}


def load_zm_fields(cfg):
    """Optional drop-in table (zm_fields.json next to this script, or cfg zone_messages_fields):
    {"factions": {"0": "Loners", ...}, "kinds": {"gefecht": {"label": "...", "level"|"importance": 3, "cat": "fight"}, ...}}
    Entries there replace ZM_KINDS / ZM_FACTIONS_DEFAULT; anything missing keeps the built-in value."""
    global ZM_FIELDS_FACTIONS
    path = cfg.get("zone_messages_fields")
    q = expand(path) if path else HERE / "zm_fields.json"
    if not q.is_file():
        return
    try:
        d = json.loads(q.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[zm] {q.name}: {e!r}")
        return
    fac = d.get("factions") or d.get("faction_ids") or {}
    if isinstance(fac, dict):
        ZM_FIELDS_FACTIONS = {str(k): (v.get("name") if isinstance(v, dict) else v) for k, v in fac.items()}
    for k, v in (d.get("kinds") or {}).items():
        old = ZM_KINDS.get(k, (k, 1, "info"))
        if isinstance(v, str):
            ZM_KINDS[k] = (v, old[1], old[2])
        elif isinstance(v, dict):
            lvl = v.get("level", v.get("importance", old[1]))
            if isinstance(lvl, str):   # Gray's table: high / med / low; its labels are notes, so keep ours
                lvl = {"high": 3, "med": 2, "medium": 2}.get(lvl.lower(), min(old[1], 1))
                ZM_KINDS[k] = (old[0], lvl, old[2])
                continue
            try:
                lvl = max(0, min(3, int(lvl)))
            except (TypeError, ValueError):
                lvl = old[1]
            ZM_KINDS[k] = (v.get("label") or old[0], lvl, v.get("cat") or v.get("category") or old[2])


def _zm_int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def parse_zm_line(line: str):
    p = line.strip().split("|")
    if len(p) < 16 or not p[0].isdigit():
        return None
    tags = {}
    while len(p) > 16 and p[-1][:1] in "rzd" and p[-1][1:].isdigit():
        tags[p[-1][0]] = int(p[-1][1:])
        p.pop()
    n = [_zm_int(x) for x in p]
    if n[0] is None or n[1] is None:
        return None
    r = {"id": n[0], "secs": n[1], "hour": n[3], "minute": n[4], "kind": p[5].strip(), "variant": n[6],
         "speaker": n[7], "speaker_fac": n[8], "subject": n[9], "fac_a": n[10], "fac_b": n[11],
         "place_id": n[12], "f13": n[13], "area": n[14], "extra": n[15],
         "x": None, "y": None, "reply_to": tags.get("r"),
         "group": (tags["z"] & 0xFFFF) if "z" in tags else None, "d": tags.get("d")}
    if len(p) >= 18 and n[16] is not None and n[17] is not None:
        r["x"], r["y"] = n[16], n[17]
    if r["group"] is None and r["kind"].startswith("zw_") and r["speaker"]:
        r["group"] = r["speaker"] & 0xFFFF          # zw_* lines: the speaker id IS the Zone Wanderers group
    return r


def zm_label(kind: str):
    if kind in ZM_KINDS:
        return ZM_KINDS[kind]
    if kind.startswith("kanal_"):
        return (f"{kind[6:].capitalize()} channel", 1, "channel")
    if kind.startswith("dlfr_"):
        return ("Faction relations: " + kind[5:].replace("_", " "), 1, "war")
    if kind.startswith("emi"):
        return ("Emission: " + kind.split("_", 1)[-1], 2, "emission")
    return (kind.replace("_", " ").capitalize(), 1, "info")




# Optional game-text templates (kind or "kind:variant" -> string). History.txt itself only
# stores a kind + variant index, not the spoken radio line. If zm_text_templates.json is
# present next to the logger, we fill message["text"] with the real line; otherwise the
# client falls back to the summary in message["label"]. Tokens are filled from History,
# faction/forms tables, and the nearest known place (POI / area). Raw {tokens} are never
# left in the text: unknown values drop the surrounding clause or phrase.
class _ZmKeep(dict):
    """Leave unknown {tokens} as placeholders so the drop pass can remove the clause."""
    def __missing__(self, key):
        return "{" + key + "}"

_ZM_FORMS = None
_ZM_TEXT_TEMPLATES = None
_ZM_THEATERS = None
_ZM_TOKEN_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def _zm_json_beside(name):
    p = Path(__file__).with_name(name)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _zm_forms():
    global _ZM_FORMS
    if _ZM_FORMS is None:
        _ZM_FORMS = _zm_json_beside("en_filler_forms.json") or {}
    return _ZM_FORMS


def _zm_templates():
    global _ZM_TEXT_TEMPLATES
    if _ZM_TEXT_TEMPLATES is None:
        _ZM_TEXT_TEMPLATES = _zm_json_beside("zm_text_templates.json") or {}
    return _ZM_TEXT_TEMPLATES


def _zm_theaters():
    global _ZM_THEATERS
    if _ZM_THEATERS is None:
        names = {}
        data = _zm_json_beside("theaters.json")
        for t in (data.get("theaters") or []):
            if isinstance(t, dict) and t.get("id") and t.get("name"):
                names[str(t["id"])] = t["name"]
        _ZM_THEATERS = names
    return _ZM_THEATERS


def _zm_fac_form(idx, key):
    forms = _zm_forms().get(key) or []
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return ""
    if 0 <= i < len(forms):
        return forms[i] or ""
    return ""


def _zm_richtung(du, dv):
    """Compass from a place to the message. Map +v is south (image / CRS.Simple)."""
    dirs = _zm_forms().get("richtung") or [
        "north", "north-east", "east", "south-east",
        "south", "south-west", "west", "north-west",
    ]
    north, east = -float(dv), float(du)
    if north == 0 and east == 0:
        return ""
    ang = math.degrees(math.atan2(east, north))
    if ang < 0:
        ang += 360
    return dirs[int((ang + 22.5) // 45) % len(dirs)]


def _zm_poi_uv(tracker, place):
    if not place:
        return None
    name = place.get("name") if isinstance(place, dict) else None
    pois = getattr(tracker, "pois", None) or []
    for p in pois:
        if name and p.get("name") == name and p.get("u") is not None:
            return float(p["u"]), float(p["v"])
    return None


def _zm_wo(gebiet, ort, dist_m, richtung):
    forms = _zm_forms().get("wo_templates") or [
        "in {gebiet}, at {ort}",
        "in {gebiet}, near {ort}",
        "in {gebiet}, close to {ort}",
        "in {gebiet}, {richtung} of {ort}",
        "somewhere in {gebiet}",
    ]
    if gebiet and ort and gebiet.casefold() == ort.casefold():
        ort = None
    if gebiet and ort:
        if dist_m is not None and dist_m >= 250 and richtung:
            tmpl = forms[3] if len(forms) > 3 else "in {gebiet}, {richtung} of {ort}"
            return tmpl.format(gebiet=gebiet, ort=ort, richtung=richtung)
        if dist_m is not None and dist_m >= 80:
            tmpl = forms[1] if len(forms) > 1 else "in {gebiet}, near {ort}"
            return tmpl.format(gebiet=gebiet, ort=ort)
        tmpl = forms[0] if forms else "in {gebiet}, at {ort}"
        return tmpl.format(gebiet=gebiet, ort=ort)
    if gebiet:
        tmpl = forms[4] if len(forms) > 4 else "somewhere in {gebiet}"
        return tmpl.format(gebiet=gebiet)
    if ort:
        if dist_m is not None and dist_m >= 80:
            return f"near {ort}"
        return f"at {ort}"
    return ""


def _zm_durch(row):
    forms = _zm_forms().get("durch_templates") or [
        "That was the work of people {x}.", "It was {x}.", "No idea who or what did it.",
    ]
    fb = row.get("fac_b")
    x = ""
    if isinstance(fb, int) and fb >= 100:
        x = (_zm_forms().get("mutant_fallback") or "a mutant")
    elif isinstance(fb, int) and 0 <= fb < 100:
        x = _zm_fac_form(fb, "faction")
    if x:
        tmpl = forms[1] if len(forms) > 1 else "It was {x}."
        return tmpl.format(x=x)
    return forms[2] if len(forms) > 2 else ""


def _zm_ursache(row):
    forms = _zm_forms().get("ursache") or ["Gunshot wound", "Mutant attack", "Cause unknown"]
    fb = row.get("fac_b")
    if isinstance(fb, int) and fb >= 100:
        return forms[1] if len(forms) > 1 else "Mutant attack"
    if isinstance(fb, int) and 0 <= fb < 100 and _zm_fac_form(fb, "faction"):
        return forms[0] if forms else "Gunshot wound"
    return forms[2] if len(forms) > 2 else "Cause unknown"


def zm_drop_unfilled(text):
    """Remove leftover {tokens} together with their clause or phrase. No dangling words, no braces."""
    if not text:
        return text
    s = str(text)

    def drop_phrases(chunk):
        chunk = re.sub(
            r"(?:,\s*|\s+)?(?:\b(?:in|at|near|from|with|of|by|for|to|on|and|or)\s+)?\{[A-Za-z_][A-Za-z0-9_]*\}",
            "",
            chunk,
        )
        chunk = _ZM_TOKEN_RE.sub("", chunk)
        return chunk

    parts = re.split(r"(?<=[.!?])\s+", s)
    kept = []
    for p in parts:
        if not p.strip():
            continue
        if _ZM_TOKEN_RE.search(p):
            p2 = drop_phrases(p)
            p2 = re.sub(r"\s+", " ", p2).strip(" ,;:-")
            # Drop the sentence if the leftover is a fragment (no word, or a dangling preposition / colon).
            if not re.search(r"[A-Za-z0-9]", p2):
                continue
            if re.search(r"\b(?:in|at|near|from|with|of|by|for|to|on|and|or|the|a|an|I'm|Im)\s*$", p2, re.I):
                continue
            if re.search(r"[:;,]\s*$", p2) and len(re.findall(r"[A-Za-z0-9]+", p2)) < 3:
                continue
            kept.append(p2)
        else:
            kept.append(p.strip())
    s = " ".join(kept)
    s = drop_phrases(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" +([,;:])", r"\1", s)
    s = re.sub(r"([,;:]){2,}", r"\1", s)
    s = re.sub(r",\s*([.!?])", r"\1", s)
    s = re.sub(r"\s+([.!?])", r"\1", s)
    s = re.sub(r"([.!?]){2,}", r"\1", s)
    s = re.sub(r"(?<=[.!?])\s+([a-z])", lambda m: " " + m.group(1).upper(), s)
    s = s.strip(" ,;:-")
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s or None


def zm_spoken_text(row, label, speaker, area_name=None, place=None, uv=None, tracker=None):
    templates = _zm_templates()
    kind = row.get("kind") or ""
    var = row.get("variant")
    tmpl = templates.get(f"{kind}:{var}") or templates.get(kind)
    if not tmpl:
        return None

    fa, fb = row.get("fac_a"), row.get("fac_b")
    fb_fac = fb if isinstance(fb, int) and 0 <= fb < 100 else None
    fb_mut = isinstance(fb, int) and fb >= 100
    extra = row.get("extra")

    gebiet = area_name or ""
    if not gebiet and isinstance(place, dict):
        tid = place.get("theater")
        if tid:
            gebiet = _zm_theaters().get(str(tid), "") or str(tid).replace("_", " ").title()

    ort, dist_m, richtung = "", None, ""
    if isinstance(place, dict) and place.get("name"):
        dist_m = place.get("dist_m")
        try:
            dist_m = float(dist_m) if dist_m is not None else None
        except (TypeError, ValueError):
            dist_m = None
        # A very distant POI is not a useful "at {ort}".
        if dist_m is None or dist_m <= 1500:
            ort = place.get("name") or ""
            if uv and uv[0] is not None and uv[1] is not None:
                puv = _zm_poi_uv(tracker, place)
                if puv:
                    richtung = _zm_richtung(uv[0] - puv[0], uv[1] - puv[1])

    wo = _zm_wo(gebiet, ort, dist_m, richtung)

    fields = _ZmKeep(
        speaker=speaker or "",
        label=label or "",
        variant=var if var is not None else "",
    )

    def put(key, val):
        if val is None:
            return
        s = str(val).strip() if not isinstance(val, (int, float)) else val
        if s == "" or s == 0 or s == "0":
            # zahl stays unfilled when zero / missing; other empty strings stay as {token}.
            return
        fields[key] = s

    put("fra", _zm_fac_form(fa, "faction"))
    put("frb", _zm_fac_form(fb_fac, "faction"))
    put("fravon", _zm_fac_form(fa, "von_from"))
    put("frbvon", _zm_fac_form(fb_fac, "von_from"))
    put("taetervon", _zm_fac_form(fb_fac, "von_from"))
    put("opfervon", _zm_fac_form(fa, "von_from"))
    put("opferbei", _zm_fac_form(fa, "opferbei_with"))
    put("opferfr", _zm_fac_form(fa, "faction"))
    put("gebiet", gebiet)
    put("ort", ort)
    put("wo", wo)
    put("richtung", richtung)
    if extra not in (None, 0, "0"):
        put("zahl", extra)

    # Honest generics for fields History does not name. Prefer these over raw tokens
    # or ungrammatical clause-drops ("Man down! is dead").
    put("opfer", "a stalker")
    put("zrname", "someone")
    put("ding", "supplies")
    mut = (_zm_forms().get("mutant_fallback") or "a mutant") if (fb_mut or kind in ("mutanten", "jagd")) else ""
    put("mutant", mut)
    put("mutantakk", mut)
    put("durch", _zm_durch(row))
    put("ursache", _zm_ursache(row))

    try:
        text = str(tmpl).format_map(fields)
    except (IndexError, ValueError):
        text = str(tmpl)
    return zm_drop_unfilled(text)

class ZoneMessagesReader:
    """Reads the newest Zone Messages History.txt (only its first bytes; it is newest-first)."""

    def __init__(self, cfg, tracker):
        self.cfg, self.tr = cfg, tracker
        self.saves_dir = expand(cfg.get("zone_messages_dir") or DEFAULTS["zone_messages_dir"])
        fixed = cfg.get("zone_messages_file")
        self.fixed = expand(fixed) if fixed else None
        self.path = None
        self.scan_t = 0.0
        self.key = None
        self.rows = []
        self.mtime = None
        self.root = self.saves_dir.parent / "History.txt"   # ZoneMessagesAsi\History.txt (same format)
        self.store = {}          # id -> row: our own history, the file only keeps ~300 lines
        self.max_id = self.max_secs = None
        self.backlog = set()     # ids seen on startup / right after a save load: feed only, no banners
        self.reloads = 0         # "save epoch": alert ids carry it, so reused ids after a reload are new alerts
        self.last_path = None
        self.last_pt = None
        self.overflow_alert = None
        self.toast_dec = {}      # alert id -> True/False, decided once (rate limits)
        self.toast_log = []      # (wall time, incident key) of banners sent
        self.fight_alias = {}    # radio fight id -> logger fight that absorbed it (one banner per incident)
        self.overflow = []       # alert ids that lost the rate limit, merged into one "N new" banner
        load_zm_fields(cfg)
        # the confirmed table wins over the older zm_fields.json guesses; the user's config wins over both
        names = {}
        for k, v in ZM_FIELDS_FACTIONS.items():
            if str(k).lstrip("-").isdigit() and v and not str(v).lower().startswith("unknown"):
                names[int(k)] = str(v)
        names.update(ZM_FACTIONS_DEFAULT)
        for k, v in (cfg.get("zone_messages_factions") or {}).items():
            if str(k).lstrip("-").isdigit() and v:
                names[int(k)] = str(v)
        self.fac_names = names

    def fac(self, i):
        if i is None or i < 0:
            return None
        if i >= 100:
            return "Mutants"
        return self.fac_names.get(i, f"Unknown ({i})")   # ids past 11 aren't decoded yet

    def _find(self, now):
        if self.fixed:
            return self.fixed if self.fixed.is_file() else None
        try:
            dm = self.saves_dir.stat().st_mtime     # a new save folder changes this at once
        except OSError:
            dm = None
        if not (self.saves_best and now - self.scan_t < 30 and dm == getattr(self, "saves_dm", None)):
            self.scan_t, self.saves_dm = now, dm
            self.saves_best = self._scan_saves()
        # the root History.txt or the newest Saves\<n>\History.txt, whichever was written last
        best, bm = None, -1.0
        for h in (self.root, self.saves_best):
            try:
                m = h.stat().st_mtime if h else -1
            except OSError:
                continue
            if m > bm:
                best, bm = h, m
        self.path = best
        return best

    saves_best = None

    def _scan_saves(self):
        # ~120 save folders: stat each History.txt (cheap; this runs at most every 30 s) and take the newest
        try:
            dirs = [d for d in os.scandir(self.saves_dir) if d.is_dir()]
        except OSError:
            return None
        best, bm = None, -1.0
        try:
            for d in dirs:
                h = Path(d.path) / "History.txt"
                try:
                    m = h.stat().st_mtime
                except OSError:
                    continue
                if m > bm:
                    best, bm = h, m
        except OSError:
            return None
        return best

    def read(self, now):
        p = self._find(now)
        if not p:
            self.rows, self.mtime = [], None
            return self.rows
        try:
            st = p.stat()
        except OSError:
            return self.rows
        key = (str(p), st.st_mtime, st.st_size)
        if key == self.key:
            return self.rows
        try:
            with open(p, "rb") as f:
                raw = f.read(128 * 1024)
        except OSError:
            return self.rows
        text = decode_bytes(raw)
        lines = text.splitlines()
        if len(raw) == 128 * 1024 and lines:
            lines = lines[:-1]   # last line may be cut
        rows = [r for r in (parse_zm_line(l) for l in lines) if r]
        if not rows:
            return self.rows
        top_id = max(r["id"] for r in rows)
        top_secs = max(r["secs"] for r in rows)
        first = self.max_id is None
        pt = int(p.parent.name) if p.parent.name.isdigit() else None   # save folder name = its playtime
        reload_why = None
        if not first:
            if str(p) == self.last_path:
                # same file: content going backwards = an older save was loaded
                if top_id < self.max_id or top_secs < self.max_secs - 5:
                    reload_why = f"seq {self.max_id} -> {top_id}"
            else:
                # another file (new save folder or the root file): a lower folder playtime, or our newest line
                # missing / different although the new file covers that id range, means another timeline
                prev = self.store.get(self.max_id)
                by = {r["id"]: r for r in rows}
                if pt is not None and self.last_pt is not None and pt < self.last_pt:
                    reload_why = f"save {self.last_pt} -> {pt}"
                else:
                    # same timeline = every id we already know has the same line in the new file (a copy that
                    # lags behind, like the root file, is fine); any id with a different line = another timeline
                    clash = sum(1 for i, r in by.items() if i in self.store
                                and (self.store[i]["secs"] != r["secs"] or self.store[i]["kind"] != r["kind"]))
                    if clash:
                        reload_why = f"{Path(self.last_path).parent.name} -> {p.parent.name}"
        if reload_why:
            # an older save was loaded: start over; its backlog goes to the feed only
            self.store.clear(); self.toast_dec.clear(); self.overflow = []; self.fight_alias.clear()
            self.backlog = set(); self.overflow_alert = None
            self.reloads += 1
            first = True
            print(f"[zm] older save loaded ({reload_why}); rebuilding the message store")
        self.last_path, self.last_pt = str(p), pt
        for r in rows:
            if first:
                self.backlog.add(r["id"])
            self.store[r["id"]] = r
        self.max_id, self.max_secs = top_id, top_secs
        if len(self.store) > 1500:
            for i in sorted(self.store, key=lambda i: (self.store[i]["secs"], i))[:len(self.store) - 1200]:
                del self.store[i]
        rows = sorted(self.store.values(), key=lambda r: (r["secs"], r["id"]), reverse=True)
        self.rows, self.key, self.mtime = rows, key, st.st_mtime
        return rows

    ZM_PRIO = {"emi_da": 0, "emi_weg": 1, "notruf": 2, "gefecht": 2, "zw_gefecht": 2,
               "dlfr_krieg": 3, "dlfr_waffenstillstand": 3, "cluster": 4, "todnetz": 5}

    def _radio_fights(self, now, out, fights, by_id, region):
        """Turn radio reports into fights. Unlinked firefight/distress reports (and todnetz pairs) with a location
        seed 'radio' fights; later reports within 200 m / 3 min join them; a logger fight nearby absorbs them
        (source 'both'). Logger fights get the radio factions, report count and latest report."""
        tr = self.tr
        def dist(a, b):
            return math.hypot(a["u"] - b["u"], a["v"] - b["v"]) / tr.ppm
        roots = [m for m in out if not m.get("is_reply") and "u" in m and m["age_s"] <= 1800]
        # windows use each report's playtime (f2 secs); replies are not roots, so they never extend an incident
        def f14(o):     # the message's own region field; logger fights fall back to the map theater
            a = o.get("area_id")
            return a if isinstance(a, int) and a >= 0 else None
        def attacker(m):
            b = m["fac_ids"][1]
            return b is not None and b >= 0
        def seed(m):
            if m["kind"] == "notruf" and not attacker(m):
                # a distress call with no attacker is an event, unless another fight report is near (500 m / 10 min)
                return any(q is not m and (q["kind"] in ("gefecht", "zw_gefecht") or (q["kind"] == "notruf" and attacker(q)))
                           and abs(q["secs"] - m["secs"]) <= 600 and dist(q, m) <= 500 for q in roots)
            if m["kind"] in ZM_RADIO_KINDS:
                return True
            # a lone death never opens a fight (it can still join one below)
            return m["kind"] == "todnetz" and any(q is not m and q["kind"] == "todnetz" and "u" in q
                                                  and abs(q["secs"] - m["secs"]) <= 120 and dist(q, m) <= 200
                                                  for q in roots)
        logger = [f for f in fights if "u" in f]
        clusters = []
        for m in sorted((m for m in roots if "fight_id" not in m and seed(m)), key=lambda q: (q["secs"], q["id"])):
            best = None
            for c in clusters:
                d = dist(m, c)
                if (0 <= m["secs"] - c["last_secs"] <= ZM_RADIO_MERGE_S and d <= ZM_RADIO_MERGE_M
                        and not (f14(m) is not None and c["region"] is not None and f14(m) != c["region"]
                                 and d > ZM_LINK_SAME_REGION_M)):
                    best = c
                    break
            if best is None:
                best = {"msgs": [], "first": m, "start": m["t"], "last": m["t"], "u": m["u"], "v": m["v"],
                        "last_secs": m["secs"], "region": f14(m)}
                clusters.append(best)
            best["msgs"].append(m)
            best["last"] = max(best["last"], m["t"])
            best["last_secs"] = max(best["last_secs"], m["secs"])
            best["u"] = sum(q["u"] for q in best["msgs"]) / len(best["msgs"])
            best["v"] = sum(q["v"] for q in best["msgs"]) / len(best["msgs"])
        made = []
        for c in clusters:
            if now - c["last"] > ZM_RADIO_EXPIRE_S:
                continue
            rid = f"radio:{self.reloads}:{c['first']['id']}"
            # a logger fight close by (and close in time) takes these reports instead of a duplicate marker
            host = None
            for f in logger:
                # an active logger fight within 200 m, or one that overlaps the reports in time
                if dist(c, f) <= ZM_RADIO_MERGE_M and (f.get("active") or (
                        f["last"] >= c["start"] - ZM_RADIO_MERGE_S and f["start"] <= c["last"] + ZM_LINK_AFTER_S)):
                    host = f
                    break
            if host is not None:
                self.fight_alias[rid] = host["id"]
                for q in c["msgs"]:
                    q["fight_id"] = host["id"]
                    q["fight_active"] = bool(host.get("active"))
                    host["message_ids"].append(q["id"])
                continue
            facs = []
            for q in c["msgs"]:
                for i in q["fac_ids"][:2]:
                    nm = self.fac(i)
                    if nm and nm not in facs:
                        facs.append(nm)
            age = now - c["last"]
            f = {"id": rid, "source": "radio", "start": round(c["start"], 1), "last": round(c["last"], 1),
                 "age_s": round(age, 1), "elapsed_s": round(now - c["start"], 1), "active": age <= ZM_RADIO_ACTIVE_S,
                 "sides": [{"faction": nm, "fielded": 0, "lost": 0, "groups": 0} for nm in facs],
                 "fielded": 0, "lost": 0, "clashes": 0, "group_ids": [], "message_ids": [],
                 "text": " vs ".join(facs) if facs else "Fighting reported",
                 "u": round(c["u"], 1), "v": round(c["v"], 1), "pos_source": "radio"}
            poi = tr._poi(f["u"], f["v"])
            if poi:
                f["near"] = poi
            for q in c["msgs"]:
                q["fight_id"] = rid
                f["message_ids"].append(q["id"])
            made.append(f)
        # other unlinked reports near a radio fight (deaths, enemy sightings, ...) list under it too
        for m in roots:
            if "fight_id" in m:
                continue
            gate = ZM_LINK_GATE.get(m["kind"], ZM_LINK_M)
            for f in made:
                if (f["start"] - ZM_LINK_BEFORE_S <= m["t"] <= f["last"] + ZM_LINK_AFTER_S and dist(m, f) <= gate
                        and not (region(m) and region(f) and region(m) != region(f)
                                 and dist(m, f) > ZM_LINK_SAME_REGION_M)):
                    m["fight_id"] = f["id"]
                    f["message_ids"].append(m["id"])
                    break
        for m in out:   # replies follow their root
            src = by_id.get(m["reply_to"]) if m.get("is_reply") else None
            if src is not None and "fight_id" in src and m.get("fight_id") != src["fight_id"]:
                m["fight_id"] = src["fight_id"]
                for f in fights + made:
                    if f["id"] == src["fight_id"]:
                        f["message_ids"].append(m["id"])
        fights.extend(made)
        # radio summary on every fight
        for f in fights:
            f.setdefault("source", "logger")
            ids = list(dict.fromkeys(f.get("message_ids") or []))
            f["message_ids"] = ids
            ms = [by_id[i] for i in ids if i in by_id]
            if not ms:
                continue
            if f["source"] == "logger" and any(q["cat"] in ("fight", "death") or q["kind"] in ZM_RADIO_KINDS
                                               for q in ms):
                f["source"] = "both"
            have = {str(s["faction"]).rstrip("?") for s in f.get("sides") or []}
            humans = {str(v).rstrip("?") for v in self.fac_names.values()}
            mut_side = any(h not in humans for h in have)     # e.g. "Bloodsucker" already covers "Mutants"
            extra = []
            for q in ms:
                for i in q["fac_ids"][:2]:
                    nm = self.fac(i)
                    if nm == "Mutants" and mut_side:
                        continue
                    if nm and nm.rstrip("?") not in have and nm not in extra:
                        extra.append(nm)
            rms = [q for q in ms if not q.get("is_reply")] or ms   # replies don't make a fight look fresher
            latest = max(rms, key=lambda q: (q["t"], q["id"]))
            f["radio"] = {"count": len(rms), "latest_kind": latest["kind"], "latest_label": latest["label"],
                          "latest_age_s": latest["age_s"], "factions": extra if f["source"] != "radio" else []}
        fights.sort(key=lambda f: (not f["active"], -f["last"]))

    def _rate_limit(self, now, alerts):
        """Banner budget: 1 per 20 s, 3 per minute, 3-minute cooldown per incident. New alerts are decided most
        important first (emission > distress/firefight > war/peace > heavy fighting > deaths), newest first within a
        class. Decisions stick per alert id; budget losers are merged into one 'N new reports' banner later."""
        self.toast_log = [x for x in self.toast_log if now - x[0] < 600]
        def key(a):
            if a.get("incident"):
                return a["incident"]
            if a["kind"] in ("emi_weg", "emi_da"):
                return "emission:" + a["kind"]
            if a["kind"].startswith("dlfr_"):
                fa = a.get("fac_ids") or []
                return a["kind"] + ":" + "-".join(str(x) for x in sorted(x for x in fa if x is not None))
            if a.get("fight_id"):
                return "f:" + str(a["fight_id"])
            if a["kind"] == "todnetz" and a.get("subject"):
                return "death:" + str(a["subject"])
            pid = a.get("place_id")
            if pid is not None and pid >= 0:
                return "p:" + str(pid)
            return a["id"]
        def budget_ok():
            recent = [t for t, _ in self.toast_log if now - t < 60]
            return not recent or (now - max(recent) >= ZM_TOAST_MIN_GAP_S and len(recent) < ZM_TOAST_PER_MIN)
        for a in alerts:
            if a["id"] in self.toast_dec:
                a["toast"] = self.toast_dec[a["id"]]
        pending = [a for a in alerts if a["toast"] and a["id"] not in self.toast_dec]
        pending.sort(key=lambda a: (self.ZM_PRIO.get(a["kind"], 4), -a["t"]))
        for a in pending:
            k = key(a)
            ks = {k}
            if k.startswith("f:"):
                fid = k[2:]
                ks.add("f:" + self.fight_alias.get(fid, fid))
                ks.update("f:" + r for r, h in self.fight_alias.items() if h == fid)
            if any(kk in ks and now - t < ZM_TOAST_COOLDOWN_S for t, kk in self.toast_log):
                ok = False                       # same incident just bannered: feed only
            elif a["kind"] in ("emi_da", "emi_weg") or budget_ok():
                ok = True                        # "emission starting" always gets through
            else:
                ok = False
                self.overflow.append(a)
            self.toast_dec[a["id"]] = ok
            a["toast"] = ok
            if ok:
                self.toast_log.append((now, k))
        # flush the overflow as one banner once the budget allows
        if self.overflow and budget_ok():
            n = len(self.overflow)
            self.overflow_alert = {"id": f"more:{self.reloads}:{self.overflow[-1]['id']}", "kind": "more", "level": 3,
                                   "toast": True, "label": f"{n} new report{'s' if n > 1 else ''} - see the Radio list",
                                   "t": now, "age_s": 0.0, "factions": [],
                                   "msg_ids": [i for a in self.overflow for i in (a.get("msg_ids") or [])][:20]}
            self.overflow = []
            self.toast_log.append((now, "more"))
        oa = self.overflow_alert
        if oa and now - oa["t"] < ZM_ALERT_S:
            alerts.append(dict(oa, age_s=round(now - oa["t"], 1)))

    def build(self, now, fights, limit=60):
        rows = self.read(now)
        if not rows:
            return {"found": self.path is not None, "file": self.path.name if self.path else None}, [], []
        newest = rows[0]["secs"]
        # playtime seconds -> wall clock: the file was written at the save's playtime (folder name), which can be
        # later than its newest line
        src = self.path or self.fixed
        pt = int(src.parent.name) if src and src.parent.name.isdigit() else None
        anchor = max(newest, pt) if pt is not None and pt - newest < 3600 else newest
        offset = (self.mtime or now) - anchor
        tr = self.tr
        by_id = {}
        out = []
        for r in rows[: max(limit, 200)]:
            label, level, cat = zm_label(r["kind"])
            facs = []
            for i in (r["fac_a"], r["fac_b"], r["speaker_fac"]):
                nm = self.fac(i)
                if nm and nm not in facs:
                    facs.append(nm)
            k = r["kind"]
            if k in ("dlfr_krieg", "dlfr_waffenstillstand", "dlfr_buendnis", "dlfr_entspannung",
                     "dlfr_misstrauen", "dlfr_ruhe_ende") and self.fac(r["fac_a"]) and self.fac(r["fac_b"]):
                joiner = " vs " if k in ("dlfr_krieg", "dlfr_misstrauen", "dlfr_ruhe_ende") else " and "
                label = f"{label}: {self.fac(r['fac_a'])}{joiner}{self.fac(r['fac_b'])}"
            elif k == "gefecht" and self.fac(r["fac_a"]) and self.fac(r["fac_b"]):
                label = f"{label}: {self.fac(r['fac_a'])} vs {self.fac(r['fac_b'])}"
            elif k in ("todnetz", "leiche", "augenzeuge", "notruf") and self.fac(r["fac_a"]):
                label = f"{label} ({self.fac(r['fac_a'])})"
                if k in ("todnetz", "notruf") and self.fac(r["fac_b"]):
                    label += (" - killed by " if k == "todnetz" else " - attacked by ") + self.fac(r["fac_b"])
            elif k in ("feind", "gehoert") and self.fac(r["fac_b"]):
                label = f"{label}: {self.fac(r['fac_b'])}"
            t = offset + r["secs"]
            speaker = self.fac(r["speaker_fac"])
            m = {"id": r["id"], "kind": k, "label": label, "level": level, "cat": cat,
                 "toast": k in ZM_TOAST, "secs": r["secs"], "variant": r.get("variant"),
                 "game_time": f"{r['hour']:02d}:{r['minute']:02d}" if r["hour"] is not None and r["minute"] is not None else None,
                 "age_game_s": newest - r["secs"], "t": round(t, 1), "age_s": round(now - t, 1),
                 "factions": facs, "speaker_faction": speaker,
                 "fac_ids": [r["fac_a"], r["fac_b"], r["speaker_fac"]], "subject": r["subject"],
                 "reply_to": r["reply_to"], "group": r["group"], "place_id": r["place_id"],
                 "area": ZM_AREAS.get(r["area"]) if r["area"] is not None else None, "area_id": r["area"]}
            if r["x"] is not None:
                xm, ym = r["x"] / 100.0, r["y"] / 100.0
                u, v = tr._uv(xm, ym)
                m.update(x=round(xm, 1), y=round(ym, 1), u=u, v=v)
                poi = tr._poi(u, v)
                if poi:
                    m["near"] = poi
            spoken = zm_spoken_text(r, label, speaker, area_name=m.get("area"),
                                    place=m.get("near"), uv=(m.get("u"), m.get("v")), tracker=tr)
            if spoken:
                m["text"] = spoken
            by_id[r["id"]] = m
            out.append(m)
        # replies (rNNN) copy their thread root's location: not a new map point, they bump the root's reply count
        for m in out:
            src = by_id.get(m["reply_to"]) if m["reply_to"] is not None else None
            if src is not None:
                m["is_reply"] = True
                src["replies"] = src.get("replies", 0) + 1
                for kk in ("x", "y", "u", "v", "near"):
                    if kk in src and kk not in m:
                        m[kk] = src[kk]
                # location may have arrived from the thread root; refill the spoken line
                row0 = next((q for q in rows if q["id"] == m["id"]), None)
                if row0 is not None:
                    spoken = zm_spoken_text(row0, m["label"], m.get("speaker_faction"),
                                            area_name=m.get("area"), place=m.get("near"),
                                            uv=(m.get("u"), m.get("v")), tracker=tr)
                    if spoken:
                        m["text"] = spoken
                    else:
                        m.pop("text", None)
        # link to live fights (per-kind distance gates; a different region only counts within 100 m)
        for f in fights:
            f.setdefault("message_ids", [])
        def region(o):
            nr = o.get("near") or {}
            return nr.get("theater") if isinstance(nr, dict) else None
        for m in out:
            if m.get("is_reply"):
                continue
            gate = ZM_LINK_GATE.get(m["kind"], ZM_LINK_M)
            best, bd = None, float("inf")
            for f in fights:
                if not (f["start"] - ZM_LINK_BEFORE_S <= m["t"] <= f["last"] + ZM_LINK_AFTER_S):
                    continue
                if m["group"] is not None and m["group"] in {int(g) & 0xFFFF for g in (f.get("group_ids") or [])
                                                             if str(g).isdigit()} and (
                        "u" not in m or "u" not in f or math.hypot(m["u"] - f["u"], m["v"] - f["v"]) / tr.ppm <= 500):
                    best, bd = f, -1
                    break
                if "u" not in m or "u" not in f:
                    continue
                d = math.hypot(m["u"] - f["u"], m["v"] - f["v"]) / tr.ppm
                rm, rf = region(m), region(f)
                if rm and rf and rm != rf and d > ZM_LINK_SAME_REGION_M:
                    continue
                if d <= gate and d < bd:
                    best, bd = f, d
            if best is not None:
                m["fight_id"] = best["id"]
                m["fight_active"] = bool(best.get("active"))
                best["message_ids"].append(m["id"])
        for m in out:   # replies follow their root's fight
            src = by_id.get(m["reply_to"]) if m.get("is_reply") else None
            if src is not None and "fight_id" in src:
                m["fight_id"] = src["fight_id"]
                for f in fights:
                    if f["id"] == src["fight_id"]:
                        f["message_ids"].append(m["id"])
        self._radio_fights(now, out, fights, by_id, region)
        # which messages may raise a banner
        def toastable(m):
            k = m["kind"]
            if m.get("is_reply"):
                return False
            if k in ("dlfr_krieg", "dlfr_waffenstillstand"):
                return ZM_TOAST_PLAYER_FAC in (m["fac_ids"][0], m["fac_ids"][1])
            if k == "todnetz":
                if m.get("fight_active"):          # only for a fight that is still going
                    return True
                return "u" in m and any(q is not m and q["kind"] == "todnetz" and "u" in q
                                        and abs(q["secs"] - m["secs"]) <= 120
                                        and math.hypot(q["u"] - m["u"], q["v"] - m["v"]) / tr.ppm <= 200 for q in out)
            if k == "emi_da":   # once per emission
                return not any(q["kind"] == "emi_da" and 0 < m["secs"] - q["secs"] <= 900 for q in out)
            return k in ZM_TOAST
        for m in out:
            m["backlog"] = m["id"] in self.backlog
            m["toast"] = toastable(m) and not m["backlog"]
        # alerts: important things happening now
        alerts = []
        for m in out:
            if (m["level"] >= 3 or m["toast"]) and not m.get("is_reply") and m["age_s"] <= ZM_ALERT_S:
                a = {"id": f"msg:{self.reloads}:{m['id']}", "kind": m["kind"], "label": m["label"], "level": 3,
                     "toast": m["toast"], "t": m["t"], "age_s": m["age_s"], "msg_ids": [m["id"]],
                     "factions": m["factions"], "place_id": m.get("place_id"), "fac_ids": m["fac_ids"][:2],
                     "subject": m.get("subject"), "cat": m["cat"]}
                for kk in ("u", "v", "near", "fight_id"):
                    if kk in m:
                        a[kk] = m[kk]
                alerts.append(a)
        hot = [m for m in out if m["kind"] in ZM_CLUSTER_KINDS and "u" in m and m["age_s"] <= ZM_ALERT_S
               and not m.get("is_reply")]
        used = set()
        for m in sorted(hot, key=lambda q: -q["t"]):
            if m["id"] in used:
                continue
            grp = [q for q in hot if q["id"] not in used and abs(q["t"] - m["t"]) <= ZM_CLUSTER_S
                   and math.hypot(q["u"] - m["u"], q["v"] - m["v"]) / tr.ppm <= ZM_CLUSTER_M]
            if len(grp) < ZM_CLUSTER_N:
                continue
            used.update(q["id"] for q in grp)
            # one death is often reported 2-3 times (todnetz, leiche, augenzeuge): count distinct subjects
            seen_subj, reports, deaths = set(), 0, 0
            for q in grp:
                subj = q.get("subject")
                if q["cat"] == "death" and subj:
                    if subj in seen_subj:
                        continue
                    seen_subj.add(subj)
                    deaths += 1
                elif q["cat"] == "death":
                    deaths += 1
                reports += 1
            if reports < ZM_CLUSTER_N:
                continue
            u = sum(q["u"] for q in grp) / len(grp)
            v = sum(q["v"] for q in grp) / len(grp)
            facs = list(dict.fromkeys(f for q in grp for f in q["factions"]))
            fid = next((q["fight_id"] for q in grp if "fight_id" in q), None)
            places = [q.get("place_id") for q in grp if q.get("place_id") is not None and q.get("place_id") >= 0]
            if fid:
                ikey = "f:" + str(fid)
            elif places:
                ikey = "p:" + str(max(set(places), key=places.count))
            else:
                cell = ZM_CLUSTER_M * tr.ppm
                ikey = f"c:{int(u // cell)}:{int(v // cell)}"
            a = {"id": f"cluster:{self.reloads}:{ikey}", "kind": "cluster", "incident": ikey,
                 "label": f"Heavy fighting: {reports} reports" + (f", {deaths} dead" if deaths else ""),
                 "level": 3, "cat": "cluster",
                 "toast": any(q["cat"] == "fight" for q in grp)
                          and any(not q["backlog"] and now - q["t"] <= 120 for q in grp),
                 "t": max(q["t"] for q in grp),
                 "age_s": round(now - max(q["t"] for q in grp), 1), "msg_ids": sorted(q["id"] for q in grp),
                 "factions": facs, "u": round(u, 1), "v": round(v, 1)}
            poi = tr._poi(u, v)
            if poi:
                a["near"] = poi
            if fid:
                a["fight_id"] = fid
            alerts.append(a)
        self._rate_limit(now, alerts)
        alerts.sort(key=lambda a: -a["t"])
        age = now - self.mtime if self.mtime else None
        src = self.path or self.fixed
        info = {"found": True, "file": src.name,   # basename only: live.json is on the LAN, no user paths
                "save": "root" if src == self.root else src.parent.name, "reloads": self.reloads,
                "newest_id": rows[0]["id"], "newest_secs": newest,
                "file_age_s": None if age is None else round(age),
                "time_note": "t/age_s are estimated from the file time and playtime seconds"}
        return info, out[:limit], alerts[:20]


def make_live_server(cfg, port: int, serve_dir: Path, bind="0.0.0.0"):
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit, unquote

    tracker = LiveTracker(cfg, serve_dir=serve_dir)
    root = Path(serve_dir).resolve()

    class Handler(SimpleHTTPRequestHandler):
        server_version = "ZoneHeat/1"

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, fmt, *args):
            pass  # quiet: the phone polls every 3 s

        def end_headers(self):
            p = urlsplit(self.path).path
            if p.endswith(".json") or p.endswith(".html") or p == "/":
                self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _allowed(self):
            p = unquote(urlsplit(self.path).path)
            if p in ("", "/"):
                return True
            parts = [x for x in p.split("/") if x]
            if any(x.startswith(".") or x.lower() in ("zoneheat_state", "discovery") for x in parts):
                return False
            name = parts[-1].lower() if parts else ""
            if name in ("logger_config.json", "calib_points.json", "locations.json"):
                return False
            return Path(name).suffix in LIVE_SERVE_EXT

        def do_GET(self):
            p = urlsplit(self.path).path
            if p == "/live.json":
                return self._send(200, tracker.snapshot_bytes(), "application/json; charset=utf-8")
            if p == "/ping":
                return self._send(200, b"ok", "text/plain")
            if not self._allowed():
                return self._send(404, b"not found", "text/plain")
            return super().do_GET()

        def do_HEAD(self):
            p = urlsplit(self.path).path
            if p == "/live.json":
                return self._send(200, b"", "application/json")
            if not self._allowed():
                return self._send(404, b"", "text/plain")
            return super().do_HEAD()

        def list_directory(self, path):
            self._send(404, b"not found", "text/plain")
            return None

    srv = ThreadingHTTPServer((bind, int(port)), Handler)
    srv.daemon_threads = True
    return srv, tracker


def start_live_server(cfg, port: int, serve_dir: Path | None = None):
    import threading
    serve_dir = Path(serve_dir) if serve_dir else expand(cfg["output"]).parent
    try:
        srv, tracker = make_live_server(cfg, port, serve_dir)
    except OSError as e:
        print(f"[live] could not start the server on port {port}: {e} (another copy running?)")
        return None
    th = threading.Thread(target=srv.serve_forever, name="zoneheat-live", daemon=True)
    th.start()
    ip = lan_ip()
    print(f"[live] serving {serve_dir} on port {port}")
    print(f"[live] on your phone (same Wi-Fi):  http://{ip}:{port}/")
    print(f"[live] live feed:                   http://{ip}:{port}/live.json")
    if not tracker.log_tail.path.exists():
        print(f"[live] waiting for {tracker.log_tail.path} (starts when the game runs Zone Wanderers)")
    return srv


def cmd_serve(cfg, port: int, serve_dir=None):
    srv = start_live_server(cfg, port, serve_dir)
    if not srv:
        return
    print("[live] Ctrl+C stops")
    while True:
        time.sleep(3600)


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
    ap.add_argument("--logs-dir", help="folder with ZoneWanderers_Events.txt / ZoneWanderersAsi.log "
                                       "(default %%LOCALAPPDATA%%\\Stalker2\\Saved)")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="update data.json every interval_seconds (default)")
    r.add_argument("--push-every", type=float, default=0, help="git commit+push data.json every N minutes (>= 6)")
    r.add_argument("--serve", type=int, nargs="?", const=8787, default=0, metavar="PORT",
                   help="also serve the map + /live.json on your home Wi-Fi (default port 8787)")
    r.add_argument("--serve-dir", help="folder to serve (default: the folder data.json is written to)")
    sv = sub.add_parser("serve", help="only serve the map + /live.json (no data.json updates)")
    sv.add_argument("--port", type=int, default=8787)
    sv.add_argument("--serve-dir")
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
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    cfg = load_config(args.config)
    if args.logs_dir:
        ld = Path(args.logs_dir)
        cfg["wanderers_events_file"] = str(ld / "ZoneWanderers_Events.txt")
        cfg["wanderers_log_file"] = str(ld / "ZoneWanderersAsi.log")
        if (ld / "ZoneMessagesAsi" / "Saves").is_dir():
            cfg["zone_messages_dir"] = str(ld / "ZoneMessagesAsi" / "Saves")
    try:
        if args.cmd in (None, "run"):
            cmd_run(cfg, push_every=getattr(args, "push_every", 0), serve=getattr(args, "serve", 0),
                    serve_dir=getattr(args, "serve_dir", None))
        elif args.cmd == "serve":
            cmd_serve(cfg, args.port, args.serve_dir)
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
