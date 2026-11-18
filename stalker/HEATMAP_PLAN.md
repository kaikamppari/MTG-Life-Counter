# S.T.A.L.K.E.R. 2 Conflict & Enemy-Group Heat Map — Build Plan (rev. 2, 2026-11-18)

## Goal
A heat map of the Zone showing (a) where fights between factions are happening and (b) where groups of hostile NPCs/mutants are. **Output: a web page (index.html on GitHub Pages) that reads data.json and works on iPhone** (pan/zoom, layer toggles, faction filter, time-window slider). Nothing is drawn in-game.

## What changed from rev. 1 (review summary)
1. **Output is a web page, not PNG/PDF.** `render_heatmap.py` (Phase 4) and the live-view loop (Phase 5) become `index.html` + `logger_to_json.py`. The heat, decay, filters and clustering display happen in the browser; the logger only prepares data.
2. **The enemy-group layer is local, not Zone-wide.** Stalker 2 only keeps NPCs as real actors in a bubble around the player. For patch 1.1, community testing put spawning at about 60–80 m and despawning at about 100–120 m; A-Life radius mods (e.g. Roadside Panic, 800 m) widen that. Everything outside the bubble lives in the native offline simulation, which UE4SS reflection can't enumerate. So ZoneSensor shows groups *around you*, and coverage grows as you travel (the page draws your trail so you can see where data exists). The Zone-wide picture comes from DLFR. Rev. 1 assumed `FindAllOf` would see the whole Zone; it won't.
3. **ZoneSensor can also feed the conflict layer.** Deaths it observes, where an actor goes from alive to dead between snapshots, have real XYZ. They go into the heat layer as `death_seen` events, which fixes DLFR's likely lack of coordinates near the player.
4. **UE4SS on patch 2.1 needs the signature fix.** Patch 2.1 changed the exe. Stock UE4SS fails with `Was unable to find AOB for 'ProcessLocalScriptFunction'` / `'GUObjectArray'`. Use Nexus 3095 (signature fix) or 3096 (prebuilt UE4SS for 2.1). Zone IFF (Nexus 3242) states it needs UE4SS 3.0.1 experimental from 2026-10-09 or later.
5. **One UE4SS install, not two.** Ultra Plus ships its own UE4SS. Its author's advice is to let the Ultra Plus Mod Manager install UE4SS, then copy other mods' folders into its `ue4ss\Mods`. The current setup has UE4SS disabled (dwmapi.dll renamed), so before Phase 2: restore exactly one dwmapi.dll (Ultra Plus's), apply the 2.1 signature fix, then add ZoneSensor.
6. **DLFR's format is unknown and moving.** DLFR v1.2.4 was uploaded on 05 Nov 2026 and last updated 17 Nov 2026. The reader is therefore adaptive: it diffs JSON/INI/KV/CSV snapshots and tails logs. Phase 0 discovery prints the real structure so rules can be tightened. DLFR also comes in a UE4SS edition that logs to `DLFR.log` instead of `DLFRAsi.log`; both paths are watched. `Log=1` in `DLFR.ini` enables the log.
7. **Hotkeys:** F9 is quick-load in Stalker 2, so ZoneSensor uses Ctrl+F7 (probe) and Ctrl+F8 (pause).

## Setup facts (Kai's PC)
- Windows, Steam, Stalker 2 (UE5), patch 2.1. User path C:\Users\kaika.
- Two mod setups: Main (story) and Unchained (Zone Unchained sandbox, playing Bandits). Every event is tagged with `setup`, and data.json only includes the configured setup.
- ASI loader `version.dll` with Faction Life and DLFR (Nexus 3266). DLFR state lives in `%LOCALAPPDATA%\Stalker2\Saved\DLFRAsi`, its log in `...\Saved\DLFRAsi.log`.
- UE4SS is currently disabled (see item 5 above).
- Read-only everywhere. No save edits. Stalker 2 has no anti-cheat.

## Phase 0 — Discovery (done: `logger_to_json.py discover`)
- `python logger_to_json.py discover --watch 600` while playing near a fight. Open the PDA Chronicle, sleep and quick-save during the watch.
- Output: `discovery\discovery_report.md` (format, encoding, key paths, faction and place mentions, coordinate-like values, what changed and how) plus copies in `discovery\samples`.
- Decision: positions present → calibrate and done. Names only → place each name once via the page's pick tool. Neither → add DLFR's own place names (listed under "other capitalised names") to `locations.json`.

## Phase 1 — Conflict layer from DLFR (done: built into `logger_to_json.py`)
- Every 60 s: structured files are diffed. A death/kill counter going up becomes a `death` event; a fight/war/front counter going up becomes a `fight` event; a new Chronicle-style line naming a faction becomes a `chronicle` event; `front`/`hotspot` entries become the hottest fronts.
- Guards: list re-sorting is neutralised by keying lists by name/pair; total-plus-parts counters (e.g. `deaths24h` + `lossesDuty` + `lossesFreedom`) are counted once; the first sight of a file is a baseline, so there's no history flood.
- Logs are tailed. `log_rules` regexes in `logger_config.json` come first, with a keyword fallback.
- History goes to `zoneheat_state\events.jsonl`; `data.json` is rebuilt from it every cycle. Positions resolve at build time, so placing a name later retroactively places old events.
- Known limitation: rolling "per 24 h" counters that drop and rise inside one 60 s cycle undercount slightly.

## Phase 2 — Enemy-group layer (starter done: `ZoneSensor` UE4SS Lua mod)
- Every 5 s, on the game thread: `FindAllOf("Character")`, then location (`K2_GetActorLocation`), class, kind (human/mutant from class name), faction / alive / combat via candidate property lists. One JSON line per snapshot, plus `death` lines, go to `%LOCALAPPDATA%\Stalker2\Saved\ZoneSensor_snap.jsonl`, rotated at 100 MB.
- **Next step:** Ctrl+F7 next to an NPC writes `ZoneSensor_probe.txt`, which contains a class histogram, the class chain, and every property with its value (faction/combat/alive-like names starred). Put the real names into `CONFIG.FactionProps` / `DeadProps` / `CombatProps`. Also read Zone IFF's `Scripts\main.lua` (plain text), since it already reads the true faction from memory.
- Option B (ASI/C++) only if UE4SS proves unusable. Option C (screen capture) dropped.

## Phase 3 — Map calibration (done: `calib-add` / `calib-undo` / `calib-show`)
- Map image: your own screenshot of the fully revealed PDA map, saved as `map.jpg` in the site folder, so the page and the logger use the same pixel space.
- Stand on a landmark in-game, then tap the same spot in the page's ✛ pick mode. The page gives the `calib-add "name" u v` command, and the logger takes your world position from ZoneSensor. A least-squares affine fit runs from 3 points; 4–6 spread to the map corners is best. `calib-show` reports RMS error and flags the worst point; the target is under 1% of map width.
- Without ZoneSensor: `--world X Y`, with X/Y from any position readout (e.g. a teleport mod), checked with the `XTeleportTo X Y Z` console command.

## Phase 4 — Web page (done: `index.html`)
- Leaflet (CDN, SRI-pinned) with CRS.Simple over the map image. Falls back to a generated placeholder grid if `MAP_IMAGE_URL` can't load, and to built-in sample data if `data.json` can't load.
- Layers: conflict heat (weight × 0.5^(age/half-life), half-life selectable from 5 min to never), groups (circle radius = cluster extent, dashed = mutants, ⚔ = in combat, faded if the snapshot is over 3 min old), single NPCs, DLFR fronts, player and trail.
- Controls: time-window slider (15 min – 7 days), faction chips, heat strength, Wars tab (deaths by pair, including events without a position), Data tab (reload, open a data.json file, sample), pick mode.
- iPhone: safe-area insets, no input zoom, bottom sheet, Add to Home Screen works as a full-screen app. Settings persist in localStorage.

## Phase 5 — Getting data to the phone
- **GitHub Pages:** `python logger_to_json.py run --push-every 10` commits and pushes only `data.json` every 10 min. GitHub rate-limits Pages rebuilds (roughly 10/hour; the logger enforces at least 6 min between pushes). Expect the page to be 1–10 min behind. The page cache-busts its fetch.
- **Same Wi-Fi, truly live:** in the site folder, `python -m http.server 8765`, then open `http://<PC-IP>:8765` on the phone. It updates every 60 s with no pushes.
- **Offline:** Data tab → open a data.json file from iCloud Drive (point `output` in `logger_config.json` at your iCloud Drive folder).

## Phase 6 — Validation
- 30 min in a busy area: hot spots should match the DLFR Chronicle hot fronts and fights you see. Groups should match what's visible; count NPCs at a camp against the circle label.
- FPS impact: compare ZoneSensor on and off (Ctrl+F8) in the same spot, target < 2%. If it's higher, raise `IntervalMs` to 10000 or set `MaxRadiusM`.
- Check both Main and Unchained. Use a separate `logger_config.json` per setup (`--config`) or switch `"setup"`.

## Parameters (tuned from rev. 1)
| Parameter | rev. 1 | now | why |
|---|---|---|---|
| DBSCAN eps | 50 m | 35 m | 50 m merges two squads that are fighting into one blob |
| min_samples | 5 | 3 | Stalker 2 squads/packs are often 3–5; 5 hides most of them |
| clustering key | all hostiles together | per faction, mutants separate | keeps opposing squads apart; their proximity is the "fight" signal |
| heat half-life | 2 h in-game / 20 min real | 60 min real (selectable) | in-game clock isn't in the data; 20 min wiped most DLFR events |
| heat weight | deaths + fights | deaths = count, fights 0.5, chronicle 0.5 + dead | deaths dominate; fronts heating up still register |
| place-name events | — | jittered ±0.6% of map width | many events at "Rostok" form a blob, not one pixel |

## Deliverables (status)
1. `logger_to_json.py`: Phase 0 discovery, Phase 1 reader, ZoneSensor ingest, clustering, calibration, data.json writer, optional git push. Stdlib only. ✅
2. `ZoneSensor\enabled.txt`, `ZoneSensor\Scripts\main.lua`: starter, tested against a mocked UE4SS API, not in-game. ✅ (property names to confirm)
3. `index.html`: the page. ✅
4. `data.schema.json`: the exact data.json schema (JSON Schema 2020-12). ✅
5. `README.md`: Windows/PowerShell steps. ✅

## Answers to the review questions
**Q1: Which UE5 classes/properties hold NPC faction and combat state?**
Confirmed from community documentation: humans use the `BP_AI_Human` blueprint (class instance name `BP_AI_Human_C`). Most human NPCs inherit from the `NPCBase` template, and the native parent of character prototypes is `Agent`. In the config data, factions are plain strings (`GeneralNPC_<Faction>_<Role>` prototypes with a `Faction` key), and relation levels are Enemy / Disaffection / Neutral / Friend. The runtime property that holds an actor's current faction, and any in-combat flag, are **not** publicly documented for 2.1. Get them three ways: ZoneSensor's Ctrl+F7 probe dump, UE4SS Live View on a selected NPC, and Zone IFF's Lua source (it reads the true faction from memory). ZoneSensor scans the engine base `Character` class and classifies by class name, so it works before those names are known.

**Q2: Best way to run a Lua mod alongside Ultra Plus's bundled UE4SS?**
Use Ultra Plus's UE4SS as the only one. Apply the patch 2.1 signature fix to it, then copy the `ZoneSensor` folder into its `Stalker2\Binaries\Win64\ue4ss\Mods\`. `enabled.txt` turns it on; alternatively add `ZoneSensor : 1` to `mods.txt`. Don't keep a second dwmapi.dll. After Ultra Plus updates, check that `ue4ss\Mods\ZoneSensor` and the signature files are still there.

**Q3: Existing mods/tools to build on?**
No mod exports Zone-wide NPC positions. The closest building blocks:
- **Zone IFF** (Nexus 3242): Lua, reads an NPC's real faction from memory. Copy its faction read.
- **Spawn Menu v1.7** (Nexus 2734): Lua, has a minimap with the PDA's markers. Its source shows how to read marker/positions.
- **Faction Relations – Live PDA Tab** (Nexus 2557): a small UE4SS C++ core that reads live relations, useful for a "hostile to me" filter later.
- **DLFR** itself: the Zone-wide conflict source.
- **Wand** desktop app: live player location on its Stalker 2 map (player only, closed source).

**Q4: Clustering/heat improvements?**
See the parameter table above. Also: cluster per faction and treat nearby opposing clusters as fights; add ZoneSensor-observed deaths with real coordinates; resolve positions at render time so late calibration fixes old data; show coverage (your trail) so empty areas read as "no data" rather than "safe".

## Next steps
1. Run `discover --watch 600` and send `discovery_report.md`. That turns the adaptive DLFR parsing into exact rules.
2. Fix UE4SS (item 5 above), install ZoneSensor, press Ctrl+F7 next to an NPC, and send `ZoneSensor_probe.txt`. That fills in faction/alive/combat names.
3. Screenshot the PDA map to `map.jpg`, calibrate 4–6 points, and place the DLFR place names.
