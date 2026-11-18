# Zone Heat — S.T.A.L.K.E.R. 2 conflict & enemy-group heat map

```
site\                      -> becomes your GitHub Pages repo
  index.html               the page (iPhone-friendly)
  data.schema.json         exact format of data.json
  logger_to_json.py        reads DLFR + ZoneSensor, writes data.json every 60 s (Python 3.9+, no installs)
  .gitignore
ZoneSensor\                UE4SS Lua mod (optional, Phase 2: groups near you + exact death positions)
  enabled.txt
  Scripts\main.lua
HEATMAP_PLAN.md            revised plan + answers to the review questions
```

All commands are for **PowerShell**, one per line.

## 1. Try the page right now
Double-click `site\index.html`. With no data.json next to it, it shows built-in **sample data** on a placeholder map.

## 2. Put it on GitHub Pages
1. On github.com, create a repository, for example `zone-heat`. Pages on a free account needs it to be **public**. data.json only contains game data.
2. Repository → Settings → Pages → Source: *Deploy from a branch*, Branch: `main` / `(root)`.
3. On the PC (Git for Windows installed):
   ```powershell
   cd $HOME\Documents
   git clone https://github.com/<you>/zone-heat.git
   Copy-Item <unzipped>\site\* .\zone-heat\ -Recurse
   Copy-Item <unzipped>\site\.gitignore .\zone-heat\
   cd .\zone-heat
   git add .
   git commit -m "Zone heat page"
   git push
   ```
4. On the iPhone, open `https://<you>.github.io/zone-heat/`, then Share → **Add to Home Screen**.

## 3. Map image
Take a screenshot of your fully revealed PDA map, crop it to the map, and save it as `map.jpg` in the repo folder. Commit and push it. To use a different file or URL, change `MAP_IMAGE_URL` near the top of the `<script>` in `index.html`. Use your own screenshot rather than a community-made map, because the repo is public.

## 4. Phase 0: see what DLFR writes
Play a few minutes with DLFR active. For a richer log, set `Log=1` in `DLFR.ini`. Then:
```powershell
cd $HOME\Documents\zone-heat
python logger_to_json.py discover --watch 600
```
Keep playing during the 10 minutes: get near a fight, open the PDA Chronicle, sleep, quick-save. Then read `discovery\discovery_report.md`, or send it to me. The first run also creates `logger_config.json` (settings) and `locations.json` (place names → map positions).

## 5. Run the logger
```powershell
python logger_to_json.py run --push-every 10
```
- `data.json` is rewritten every 60 s. Every 10 min it is committed and pushed. GitHub limits Pages rebuilds to roughly 10 per hour, so the phone lags a few minutes.
- **Live on home Wi-Fi with no pushes:** in a second PowerShell window run the command below, then open `http://<PC-IP>:8765` on the phone. When Windows asks, allow Python on **private** networks.
  ```powershell
  cd $HOME\Documents\zone-heat
  python -m http.server 8765
  ```
  To find the PC's IP: `ipconfig`, then look for the IPv4 address.
- Two setups: set `"setup": "Main"` or `"Unchained"` in `logger_config.json`. Alternatively, keep two config files and use `--config`.

## 6. Put DLFR place names on the map
DLFR events that carry only a name (e.g. "Rostok") need a position once:
1. On the page, tap **✛**, then tap the place.
2. Copy the `place` command, change the name, and run it on the PC:
   ```powershell
   python logger_to_json.py place "Rostok" 1380 2100
   ```
Old events at that name appear on the map on the next update. The Wars tab lists events that still have no position.

## 7. ZoneSensor (groups near you)
1. Use **one** UE4SS. Ultra Plus installs its own; keep only that `dwmapi.dll`. On patch 2.1 also apply the UE4SS signature fix (Nexus 3095) or use the 2.1 UE4SS build (Nexus 3096).
2. Copy the `ZoneSensor` folder to `...\S.T.A.L.K.E.R. 2 Heart of Chornobyl\Stalker2\Binaries\Win64\ue4ss\Mods\`.
3. In game, stand next to an NPC and press **Ctrl+F7**. Open `%LOCALAPPDATA%\Stalker2\Saved\ZoneSensor_probe.txt`. Starred lines are candidate faction/alive/combat properties. Put the real names at the top of `Scripts\main.lua` in `FactionProps`, `DeadProps` and `CombatProps`, then restart the game. **Ctrl+F8** pauses or resumes logging.
4. Calibrate: stand on a landmark, tap the same spot on the page with ✛, copy the `calib-add` command and run it. Repeat at 4–6 spots spread across the map.
   ```powershell
   python logger_to_json.py calib-add "Rostok gate" 1380 2100
   python logger_to_json.py calib-show
   ```
   If the error is above 1%, `calib-undo` and redo the worst point.

## data.json in short
Positions are pixels on your map image (u right, v down). Times are Unix seconds. The full definition is in `data.schema.json`.
```json
{
  "schema": "zoneheat/1", "generated": 1795022170.5, "setup": "Unchained",
  "map": {"width": 3000, "height": 3000, "calibrated": true, "pixels_per_meter": 1.8, "labels": {"Rostok": [1380, 2100]}},
  "conflicts": [{"t": 1795022100, "u": 1391.2, "v": 2088.0, "w": 2.0, "kind": "death", "factions": ["Duty", "Freedom"],
                 "location": "Rostok", "dead": 2, "text": "...", "source": "DLFRAsi/state.json"}],
  "groups":    [{"t": 1795022170, "u": 1020.1, "v": 1904.5, "r": 45.6, "n": 6, "faction": "Bandits", "mutant": false,
                 "combat": true, "classes": [["BP_AI_Human_C", 6]]}],
  "groups_t": 1795022170,
  "actors":    [{"u": 1018.0, "v": 1900.2, "faction": "Bandits", "kind": "human", "cls": "BP_AI_Human_C"}],
  "fronts":    [{"rank": 1, "name": "Rostok", "u": 1380, "v": 2100, "factions": ["Duty", "Freedom"], "heat": 9}],
  "wars":      [{"pair": "Duty vs Freedom", "dead": 6, "events": 2, "last": 1795022100}],
  "player":    {"u": 922.2, "v": 1966.7, "t": 1795022170},
  "trail":     [[900.0, 2000.0], [922.2, 1966.7]],
  "unplaced":  [{"t": 1795022100, "kind": "death", "factions": ["Bandits", "Loners"], "location": null, "dead": 1, "text": "..."}]
}
```
