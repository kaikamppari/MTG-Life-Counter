#!/usr/bin/env python3
"""route_api.py - click-to-go endpoints for the Zone Heat logger server (route runner M3/M6).

Endpoints (JSON in/out):
  POST /api/route/goal    {"x": cm, "y": cm}  or  {"u": px, "v": px}   (+ optional "name", "snap": bool)
                          -> writes <route_dir>/goal.json atomically. The runner NEVER starts walking
                             from this alone: goal.json has "armed": false; Kai arms in-game (hotkey).
  POST /api/route/stop    {"reason": "..."} (body optional) -> creates <route_dir>/STOP atomically.
  GET  /api/route/status  -> {"state", "goal", "stop_requested", "runner": <status.json or null>, "plan"}
  POST /api/route/status  same as GET (body ignored). Nothing on these endpoints ever sets armed:true.
  Goal POST may include start_x/start_y (UE cm) or start_u/start_v (map px). If a start is given,
  Gray's planner is called and the response/status include plan.{ok,length_m,eta_s,waypoints,pixels}.

File IPC (all writes are temp file + os.replace, so readers never see half a file):
  goal.json    written here, read by the runner
  STOP         written here (and by the runner's own hotkey), the runner deletes it when it re-arms
  status.json  written by the runner only; this module just reads it

route_dir: logger_config.json "route_dir", else env ZONEROUTE_DIR, else %LOCALAPPDATA%\\ZoneRoute
(on non-Windows: ~/.zoneroute). Map px <-> world uses the logger's own calibration (calib_points.json,
the same least-squares affine as logger_to_json.fit_affine) and its inverse.

Security: the handlers run inside the logger's existing server, so they bind exactly where the logger
binds (it decides; this module never opens a socket except in serve_for_tests(), which defaults to
127.0.0.1). POST requires Content-Type application/json and <= 4 KB; no CORS preflight is answered, so
other web origins can't POST here from a browser. Stdlib only.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

MAX_BODY = 4096
WORLD_MIN, WORLD_MAX = -100_000.0, 900_000.0       # UE cm; the Zone is about 0..812,900
NAME_RE = re.compile(r"^[\w .,'()\-/]{0,80}$", re.UNICODE)


class BadRequest(Exception):
    pass


def _fit_affine(points):
    """Least squares u,v = A [x y 1]. Same model as logger_to_json.fit_affine (normal equations)."""
    def solve3(M, b):
        M = [row[:] + [b[i]] for i, row in enumerate(M)]
        for c in range(3):
            p = max(range(c, 3), key=lambda r: abs(M[r][c]))
            if abs(M[p][c]) < 1e-12:
                raise ValueError("calibration points are degenerate")
            M[c], M[p] = M[p], M[c]
            for r in range(3):
                if r != c:
                    f = M[r][c] / M[c][c]
                    M[r] = [M[r][k] - f * M[c][k] for k in range(4)]
        return [M[i][3] / M[i][i] for i in range(3)]
    # centre the data for numerical stability, then shift back
    mx = sum(p["world"][0] for p in points) / len(points); my = sum(p["world"][1] for p in points) / len(points)
    ata = [[0.0] * 3 for _ in range(3)]; atu = [0.0] * 3; atv = [0.0] * 3
    for p in points:
        row = (p["world"][0] - mx, p["world"][1] - my, 1.0)
        for i in range(3):
            for j in range(3):
                ata[i][j] += row[i] * row[j]
            atu[i] += row[i] * p["pixel"][0]; atv[i] += row[i] * p["pixel"][1]
    a, b, c = solve3(ata, atu); d, e, f = solve3(ata, atv)
    return [[a, b, c - a * mx - b * my], [d, e, f - d * mx - e * my]]


def world_to_px(m, x, y):
    (a, b, c), (d, e, f) = m
    return a * x + b * y + c, d * x + e * y + f


def px_to_world(m, u, v):
    (a, b, c), (d, e, f) = m
    det = a * e - b * d
    if abs(det) < 1e-15:
        raise ValueError("calibration is degenerate")
    du, dv = u - c, v - f
    return (e * du - b * dv) / det, (-d * du + a * dv) / det


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data); fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def default_route_dir(cfg=None) -> Path:
    if cfg and cfg.get("route_dir"):
        return Path(os.path.expandvars(os.path.expanduser(cfg["route_dir"])))
    if os.environ.get("ZONEROUTE_DIR"):
        return Path(os.environ["ZONEROUTE_DIR"])
    la = os.environ.get("LOCALAPPDATA")
    return Path(la) / "ZoneRoute" if la else Path.home() / ".zoneroute"


class RouteAPI:
    def __init__(self, route_dir, calib_points=None, map_size=None, places=None, matrix=None):
        self.dir = Path(route_dir)
        self.m = matrix or (_fit_affine(calib_points) if calib_points and len(calib_points) >= 3 else None)
        self.map_size = tuple(map_size) if map_size else None          # (W, H) px of map.jpg
        self.places = places or []                                     # [{"sid","x","y"}] UE cm, for snapping

    # ---- construction from the logger's config (used by the patch) ----
    @classmethod
    def from_logger(cls, cfg, expand, map_size=None):
        pts = []
        try:
            p = expand(cfg["calib_points_file"])
            if p.exists():
                pts = json.loads(p.read_text(encoding="utf-8")).get("points", [])
        except Exception:
            pts = []
        places = []
        pp = Path(cfg.get("route_places_file", "")) if cfg.get("route_places_file") else None
        if pp and pp.exists():
            places = json.loads(pp.read_text(encoding="utf-8")).get("items", [])
        return cls(default_route_dir(cfg), pts, map_size, places)

    # ---- request handling (pure: no sockets) ----
    def handle(self, method, path, headers=None, body=b""):
        """Returns (http_code, json_bytes)."""
        try:
            if path == "/api/route/status" and method in ("GET", "HEAD", "POST"):
                return 200, self._json(self.status())
            if method != "POST":
                return 405, self._json({"ok": False, "error": "method not allowed"})
            if path not in ("/api/route/goal", "/api/route/stop"):
                return 404, self._json({"ok": False, "error": "not found"})
            ctype = (headers or {}).get("Content-Type", "") if headers is not None else ""
            if body and not str(ctype).lower().startswith("application/json"):
                return 415, self._json({"ok": False, "error": "Content-Type must be application/json"})
            data = self._parse(body)
            if path == "/api/route/goal":
                return 200, self._json(self.set_goal(data))
            return 200, self._json(self.stop(data))
        except BadRequest as e:
            return 400, self._json({"ok": False, "error": str(e)})
        except Exception as e:                                         # never crash the logger
            return 500, self._json({"ok": False, "error": f"internal: {type(e).__name__}"})

    def _json(self, obj):
        return json.dumps(obj, separators=(",", ":")).encode("utf-8")

    def _parse(self, body):
        if not body:
            return {}
        if len(body) > MAX_BODY:
            raise BadRequest("body too large")
        try:
            d = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BadRequest("body is not valid JSON")
        if not isinstance(d, dict):
            raise BadRequest("body must be a JSON object")
        return d

    @staticmethod
    def _num(d, k):
        v = d.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise BadRequest(f"'{k}' must be a finite number")
        return float(v)

    def set_goal(self, d):
        has_xy, has_uv = "x" in d or "y" in d, "u" in d or "v" in d
        if has_xy == has_uv:
            raise BadRequest("give either world x,y (UE cm) or map u,v (px), not both/neither")
        name = d.get("name", "")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise BadRequest("'name' must be up to 80 plain characters")
        if has_uv:
            u, v = self._num(d, "u"), self._num(d, "v")
            if self.map_size and not (0 <= u <= self.map_size[0] and 0 <= v <= self.map_size[1]):
                raise BadRequest("u,v outside the map image")
            if not self.m:
                raise BadRequest("no map calibration (need 3+ calib points) - send world x,y instead")
            x, y = px_to_world(self.m, u, v); source = "map_px"
        else:
            x, y = self._num(d, "x"), self._num(d, "y"); source = "world"
            u, v = world_to_px(self.m, x, y) if self.m else (None, None)
        if not (WORLD_MIN <= x <= WORLD_MAX and WORLD_MIN <= y <= WORLD_MAX):
            raise BadRequest("goal outside the Zone")
        snapped = None
        if d.get("snap") and self.places:
            best = min(self.places, key=lambda p: (p["x"] - x) ** 2 + (p["y"] - y) ** 2)
            dist_m = math.hypot(best["x"] - x, best["y"] - y) / 100.0
            if dist_m <= 60.0:
                x, y = float(best["x"]), float(best["y"]); snapped = {"sid": best.get("sid"), "moved_m": round(dist_m, 1)}
        # Always armed:false. A client "armed" field is ignored; HTTP can never arm the runner.
        if d.get("armed") is True:
            pass  # explicitly discarded
        goal = {"id": f"g{int(time.time() * 1000)}", "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "x": round(x, 1), "y": round(y, 1), "units": "UE cm",
                "u": None if u is None else round(u, 1), "v": None if v is None else round(v, 1),
                "name": name, "source": source, "snapped": snapped, "armed": False,
                "note": "runner plans only; Kai arms the walk in-game"}
        atomic_write(self.dir / "goal.json", json.dumps(goal, indent=1).encode("utf-8"))
        plan = self._plan_for(d, (x, y))
        if plan is not None:
            atomic_write(self.dir / "plan.json", json.dumps(plan, indent=1).encode("utf-8"))
        elif (self.dir / "plan.json").exists():
            try:
                (self.dir / "plan.json").unlink()
            except OSError:
                pass
        out = {"ok": True, "goal": goal}
        if plan is not None:
            out["plan"] = plan
        return out

    def stop(self, d):
        reason = d.get("reason", "stop requested from the map")
        if not isinstance(reason, str) or len(reason) > 200:
            raise BadRequest("'reason' must be a string up to 200 chars")
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "reason": reason}
        atomic_write(self.dir / "STOP", json.dumps(rec).encode("utf-8"))
        return {"ok": True, "stop": rec}

    def status(self):
        def read(p):
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        runner = read(self.dir / "status.json")
        goal = read(self.dir / "goal.json")
        stop = (self.dir / "STOP").exists()
        state = (runner or {}).get("state") or ("goal_set" if goal else "idle")
        plan = read(self.dir / "plan.json")
        # If the runner already has a path and we have a calibration, also expose pixels for the map.
        if plan is None and runner and runner.get("path") and self.m:
            plan = self._plan_from_waypoints(runner["path"])
        return {"ok": True, "state": state, "goal": goal, "stop_requested": stop, "runner": runner,
                "calibrated": self.m is not None, "plan": plan}


    # ---- optional plan (Gray's planner). Never required for writing the goal. ----
    WALK_M_S = 2.0          # used only for the map's ETA; the runner has its own speed model
    _planner = None
    _planner_tried = False

    def _start_xy(self, d):
        """Optional start in the goal body: start_x/start_y (cm) or start_u/start_v (px)."""
        has_xy = "start_x" in d or "start_y" in d
        has_uv = "start_u" in d or "start_v" in d
        if has_xy and has_uv:
            raise BadRequest("give either start_x,start_y or start_u,start_v, not both")
        if has_xy:
            return self._num(d, "start_x"), self._num(d, "start_y")
        if has_uv:
            if not self.m:
                return None
            su, sv = self._num(d, "start_u"), self._num(d, "start_v")
            return px_to_world(self.m, su, sv)
        return None

    def _get_planner(self):
        if self._planner_tried:
            return self._planner
        self._planner_tried = True
        import sys
        cands = []
        env = os.environ.get("ZONEROUTE_PLANNER_DIR")
        if env:
            cands.append(Path(env))
        cands.extend([
            Path("/workspace/stalker2/route_runner"),
            Path(__file__).resolve().parent,
            Path(__file__).resolve().parent / "route_runner",
        ])
        for d in cands:
            if d and (d / "planner.py").is_file():
                sys.path.insert(0, str(d))
                try:
                    import planner as _pl
                    self._planner = _pl.Planner()
                    return self._planner
                except Exception:
                    self._planner = None
                    continue
        return None

    def _waypoints_px(self, waypoints):
        if not self.m or not waypoints:
            return []
        out = []
        for pt in waypoints:
            if not pt or len(pt) < 2:
                continue
            u, v = world_to_px(self.m, float(pt[0]), float(pt[1]))
            out.append([round(u, 1), round(v, 1)])
        return out

    def _plan_from_waypoints(self, waypoints, extra=None):
        length = 0.0
        for a, b in zip(waypoints, waypoints[1:]):
            length += math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1])) / 100.0
        rec = {"ok": True, "reason": "", "waypoints": waypoints,
               "pixels": self._waypoints_px(waypoints),
               "length_m": round(length, 1),
               "eta_s": round(length / self.WALK_M_S) if length else 0}
        if extra:
            rec.update(extra)
        return rec

    def _plan_for(self, d, goal_xy):
        try:
            start = self._start_xy(d)
        except BadRequest:
            raise
        except Exception:
            return {"ok": False, "reason": "could not read start position"}
        if not start:
            return None
        pl = self._get_planner()
        if pl is None:
            # Straight-line preview so the map still has a polyline.
            wp = [[round(start[0], 1), round(start[1], 1)], [round(goal_xy[0], 1), round(goal_xy[1], 1)]]
            rec = self._plan_from_waypoints(wp)
            rec["reason"] = "straight line (planner unavailable)"
            rec["preview"] = True
            return rec
        try:
            res = pl.plan(start, goal_xy)
        except Exception as e:
            return {"ok": False, "reason": f"planner failed: {type(e).__name__}"}
        if not res or not res.get("ok"):
            return {"ok": False, "reason": (res or {}).get("reason") or "no path"}
        rec = self._plan_from_waypoints(res["waypoints"], {
            "straight_m": res.get("straight_m"),
            "ratio": res.get("ratio"),
            "time_s": res.get("time_s"),
        })
        rec["length_m"] = res.get("length_m", rec["length_m"])
        rec["eta_s"] = round(rec["length_m"] / self.WALK_M_S) if rec.get("length_m") else 0
        return rec


def serve_for_tests(api: RouteAPI, port=0, bind="127.0.0.1"):
    """Tiny standalone server (tests only). Returns (server, thread). Call server.shutdown() after."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _do(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                code, body = 413, b'{"ok":false,"error":"body too large"}'
            else:
                code, body = api.handle(method, self.path.split("?")[0], self.headers, self.rfile.read(n) if n else b"")
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

    srv = ThreadingHTTPServer((bind, port), H); srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    return srv, th
