--[[
  ZoneSensor - STARTER UE4SS Lua mod for the Zone heat map (S.T.A.L.K.E.R. 2)

  READ-ONLY: it never spawns, edits relations, or changes any actor. Every few
  seconds it lists the characters the game currently has loaded (humans and
  mutants in the "online" bubble around the player), and appends one JSON line
  per snapshot to:
      %LOCALAPPDATA%\Stalker2\Saved\ZoneSensor_snap.jsonl
  logger_to_json.py reads that file and turns it into data.json for the page.

  IMPORTANT LIMIT: Stalker 2 only keeps NPCs as real actors near the player
  (a few hundred metres, more with A-Life radius mods). Everything else lives
  in the native "offline" A-Life simulation that UE4SS can't see by reflection.
  So this layer shows groups AROUND YOU; it builds a Zone-wide picture only as
  you move around. DLFR's data covers the rest of the Zone.

  STARTER STATUS: class/property names for faction, alive and combat state are
  NOT confirmed for patch 2.1. The mod tries a list of likely names (CONFIG
  below) and records nil when none match. Use the probe hotkey (Ctrl+F7) next
  to an NPC: it writes ZoneSensor_probe.txt with the real class chain and
  every property name/value, so you can put the right names into CONFIG.
  Tip: the "Zone IFF" mod (Nexus 3242) is a UE4SS Lua mod that reads an NPC's
  true faction from memory - its Scripts\main.lua shows exactly which
  property/function it uses. Copy that approach into readFaction().

  Hotkeys (F9 is quick-load in Stalker 2, so it is avoided):
      Ctrl+F7  probe nearest character + class histogram -> ZoneSensor_probe.txt
      Ctrl+F8  pause / resume logging
]]

local CONFIG = {
    IntervalMs   = 5000,          -- snapshot period; 5000-10000 is plenty
    ScanClass    = "Character",   -- engine base class (ACharacter); humans and mutants derive from it
    MaxActors    = 400,           -- safety cap per snapshot
    MaxRadiusM   = 0,             -- 0 = everything loaded; else only within N metres of the player
    MaxFileMB    = 100,           -- rotate snap file to .old above this size
    IncludeOther = false,         -- also log characters we can't classify as human/mutant

    -- Candidate property names, tried in order. Replace with the real ones
    -- from the probe output. Each may be on the actor itself or on its Controller.
    FactionProps = { "Faction", "FactionSID", "FactionName", "CurrentFaction", "GroupSID", "Group", "Community" },
    DeadProps    = { "bIsDead", "bDead", "IsDead", "bIsDying" },          -- true = dead
    AliveProps   = { "bIsAlive", "IsAlive" },                             -- true = alive
    HealthProps  = { "Health", "CurrentHealth", "HP" },                   -- <= 0 = dead
    CombatProps  = { "bIsInCombat", "bInCombat", "IsInCombat", "bCombat", "CombatState" },

    -- Class-name keywords used to tell mutants from humans (case-insensitive).
    MutantWords = { "bloodsucker", "flesh", "boar", "snork", "dog", "pseudodog", "controller", "chimera",
                    "burer", "poltergeist", "pseudogiant", "giant", "rat", "rodent", "tushkan", "zombie",
                    "bayun", "cat", "mutant", "deer", "spider" },
    HumanWords  = { "human", "npc", "stalker", "soldier", "bandit" },
}

local SAVED     = (os.getenv("LOCALAPPDATA") or ".") .. "\\Stalker2\\Saved\\"
local OUT_FILE  = SAVED .. "ZoneSensor_snap.jsonl"
local PROBE_FILE = SAVED .. "ZoneSensor_probe.txt"

local enabled = true
local busy = false
local prevAlive = {}      -- actor id -> true/false from the previous snapshot
local lastSizeCheck = 0

local okHelpers, UEHelpers = pcall(require, "UEHelpers")

---------------------------------------------------------------------------
-- small helpers
---------------------------------------------------------------------------
local function log(msg) print("[ZoneSensor] " .. tostring(msg) .. "\n") end

local function isValid(o)
    if o == nil then return false end
    local ok, v = pcall(function() return o:IsValid() end)
    return ok and v == true
end

local function str(v)
    if v == nil then return nil end
    local t = type(v)
    if t == "string" or t == "number" or t == "boolean" then return v end
    if t == "userdata" or t == "table" then
        local ok, s = pcall(function() return v:ToString() end)          -- FName / FString / FText
        if ok and type(s) == "string" then return s end
        ok, s = pcall(function() return v:GetFName():ToString() end)     -- UObject
        if ok and type(s) == "string" then return s end
    end
    return tostring(v)
end

local function readProp(obj, name)
    local ok, v = pcall(function() return obj[name] end)
    if not ok or v == nil then return nil end
    if type(v) == "userdata" then
        local okv, valid = pcall(function() return v:IsValid() end)
        if okv and valid == false then return nil end
    end
    return v
end

local function firstProp(objs, names)
    for _, o in ipairs(objs) do
        if o then
            for _, n in ipairs(names) do
                local v = readProp(o, n)
                if v ~= nil then return v, n end
            end
        end
    end
    return nil
end

local function className(obj)
    local ok, s = pcall(function() return obj:GetClass():GetFName():ToString() end)
    return ok and s or "?"
end

local function classify(cls)
    local low = string.lower(cls)
    for _, w in ipairs(CONFIG.MutantWords) do
        if string.find(low, w, 1, true) then return "mutant" end
    end
    for _, w in ipairs(CONFIG.HumanWords) do
        if string.find(low, w, 1, true) then return "human" end
    end
    return "other"
end

local function jsonStr(s)
    s = tostring(s)
    s = s:gsub('[%c"\\]', function(c)
        if c == '"' then return '\\"' elseif c == "\\" then return "\\\\"
        elseif c == "\n" then return "\\n" elseif c == "\r" then return "\\r" elseif c == "\t" then return "\\t"
        else return string.format("\\u%04x", string.byte(c)) end
    end)
    return '"' .. s .. '"'
end

local function jsonVal(v)
    local t = type(v)
    if v == nil then return "null"
    elseif t == "boolean" then return v and "true" or "false"
    elseif t == "number" then
        if v ~= v or v == math.huge or v == -math.huge then return "null" end
        if math.floor(v) == v then return string.format("%d", v) end
        return string.format("%.1f", v)
    else return jsonStr(v) end
end

local function getPlayer()
    if okHelpers and UEHelpers and UEHelpers.GetPlayer then
        local ok, p = pcall(UEHelpers.GetPlayer)
        if ok and isValid(p) then return p end
    end
    local pc = FindFirstOf("PlayerController")
    if isValid(pc) then
        local p = readProp(pc, "Pawn")
        if isValid(p) then return p end
    end
    return nil
end

local function location(actor)
    local ok, loc = pcall(function() return actor:K2_GetActorLocation() end)
    if ok and loc then return loc.X, loc.Y, loc.Z end
    return nil
end

local function actorId(actor)
    local ok, a = pcall(function() return actor:GetAddress() end)
    if ok and a then return string.format("%x", a) end
    return str(actor:GetFName())
end

---------------------------------------------------------------------------
-- per-actor reads (edit these once the probe shows the real names)
---------------------------------------------------------------------------
local function readFaction(actor, controller)
    local v = firstProp({ actor, controller }, CONFIG.FactionProps)
    return v ~= nil and str(v) or nil
end

local function readAlive(actor, controller)
    local d = firstProp({ actor, controller }, CONFIG.DeadProps)
    if type(d) == "boolean" then return not d end
    local a = firstProp({ actor, controller }, CONFIG.AliveProps)
    if type(a) == "boolean" then return a end
    local h = firstProp({ actor }, CONFIG.HealthProps)
    if type(h) == "number" then return h > 0 end
    return nil
end

local function readCombat(actor, controller)
    local c = firstProp({ controller, actor }, CONFIG.CombatProps)
    if type(c) == "boolean" then return c end
    if type(c) == "number" then return c > 0 end
    return nil
end

---------------------------------------------------------------------------
-- file output
---------------------------------------------------------------------------
local function appendLine(line)
    local f = io.open(OUT_FILE, "a")
    if not f then log("cannot open " .. OUT_FILE) return end
    f:write(line, "\n")
    local now = os.time()
    if now - lastSizeCheck > 60 then
        lastSizeCheck = now
        local size = f:seek("end") or 0
        f:close()
        if size > CONFIG.MaxFileMB * 1024 * 1024 then
            os.remove(OUT_FILE .. ".old")
            os.rename(OUT_FILE, OUT_FILE .. ".old")
            log("rotated snapshot file")
        end
        return
    end
    f:close()
end

---------------------------------------------------------------------------
-- snapshot (runs on the game thread)
---------------------------------------------------------------------------
local function snapshot()
    local player = getPlayer()
    local px, py, pz
    if player then px, py, pz = location(player) end
    local list = FindAllOf(CONFIG.ScanClass) or {}
    local parts, deaths = {}, {}
    local n = 0
    local seen = {}
    local maxR2 = (CONFIG.MaxRadiusM > 0 and px) and (CONFIG.MaxRadiusM * 100) ^ 2 or nil
    for _, actor in ipairs(list) do
        if n >= CONFIG.MaxActors then break end
        if isValid(actor) and actor ~= player then
            local x, y, z = location(actor)
            if x and not (maxR2 and ((x - px) ^ 2 + (y - py) ^ 2) > maxR2) then
                local cls = className(actor)
                local kind = classify(cls)
                if kind ~= "other" or CONFIG.IncludeOther then
                    local controller = readProp(actor, "Controller")
                    local id = actorId(actor)
                    local alive = readAlive(actor, controller)
                    local fac = readFaction(actor, controller)
                    local combat = readCombat(actor, controller)
                    seen[id] = true
                    n = n + 1
                    parts[#parts + 1] = string.format(
                        '{"id":%s,"cls":%s,"kind":%s,"fac":%s,"x":%s,"y":%s,"z":%s,"alive":%s,"combat":%s}',
                        jsonStr(id), jsonStr(cls), jsonStr(kind), jsonVal(fac),
                        jsonVal(math.floor(x)), jsonVal(math.floor(y)), jsonVal(math.floor(z)),
                        jsonVal(alive), jsonVal(combat))
                    -- death = alive in the previous snapshot, dead now
                    if alive == false and prevAlive[id] == true then
                        deaths[#deaths + 1] = string.format(
                            '{"t":%d,"type":"death","id":%s,"cls":%s,"kind":%s,"fac":%s,"x":%s,"y":%s,"z":%s}',
                            os.time(), jsonStr(id), jsonStr(cls), jsonStr(kind), jsonVal(fac),
                            jsonVal(math.floor(x)), jsonVal(math.floor(y)), jsonVal(math.floor(z)))
                    end
                    if alive ~= nil then prevAlive[id] = alive end
                end
            end
        end
    end
    for id in pairs(prevAlive) do if not seen[id] then prevAlive[id] = nil end end

    local playerJson = "null"
    if px then
        playerJson = string.format('{"x":%s,"y":%s,"z":%s}', jsonVal(math.floor(px)), jsonVal(math.floor(py)), jsonVal(math.floor(pz)))
    end
    for _, d in ipairs(deaths) do appendLine(d) end
    appendLine(string.format('{"t":%d,"type":"snap","player":%s,"n":%d,"actors":[%s]}',
        os.time(), playerJson, n, table.concat(parts, ",")))
end

---------------------------------------------------------------------------
-- probe: dump class chain + properties of the nearest character
---------------------------------------------------------------------------
local INTERESTING = { "faction", "group", "relation", "team", "combat", "alive", "dead", "health",
                      "state", "squad", "sid", "guid", "attitude", "enemy", "target", "community", "alife" }

local function isInteresting(name)
    local low = string.lower(name)
    for _, w in ipairs(INTERESTING) do if string.find(low, w, 1, true) then return true end end
    return false
end

local function dumpObject(f, obj, title)
    f:write("== ", title, ": ", str(obj:GetFullName()) or "?", "\n")
    local cls = obj:GetClass()
    local depth = 0
    while isValid(cls) and depth < 12 do
        f:write("  class: ", str(cls:GetFName()) or "?", "\n")
        pcall(function()
            cls:ForEachProperty(function(prop)
                local name = prop:GetFName():ToString()
                local ok, v = pcall(function() return obj[name] end)
                local val = ok and str(v) or "<err>"
                if type(v) == "userdata" and isValid(v) then
                    local okc, c = pcall(function() return v:GetClass():GetFName():ToString() end)
                    if okc then val = val .. "  (" .. c .. ")" end
                end
                f:write(isInteresting(name) and "   * " or "     ", name, " = ", tostring(val), "\n")
            end)
        end)
        local ok, sup = pcall(function() return cls:GetSuperStruct() end)
        if not ok then break end
        cls = sup
        depth = depth + 1
    end
end

local function probe()
    local f = io.open(PROBE_FILE, "w")
    if not f then log("cannot write probe file") return end
    local player = getPlayer()
    local px, py, pz
    if player then px, py, pz = location(player) end
    f:write("ZoneSensor probe ", os.date(), "\nplayer: ", tostring(px), ", ", tostring(py), ", ", tostring(pz), "\n\n")

    local list = FindAllOf(CONFIG.ScanClass) or {}
    local hist, nearest, best = {}, nil, math.huge
    for _, a in ipairs(list) do
        if isValid(a) and a ~= player then
            local cls = className(a)
            hist[cls] = (hist[cls] or 0) + 1
            local x, y = location(a)
            if x and px then
                local d = (x - px) ^ 2 + (y - py) ^ 2
                if d < best then best, nearest = d, a end
            end
        end
    end
    f:write("== class histogram of loaded '", CONFIG.ScanClass, "' instances (", #list, ")\n")
    for cls, c in pairs(hist) do f:write(string.format("  %4d  %s  [%s]\n", c, cls, classify(cls))) end
    f:write("\n")
    if nearest then
        f:write(string.format("nearest character at %.0f m\n", math.sqrt(best) / 100))
        dumpObject(f, nearest, "ACTOR")
        local controller = readProp(nearest, "Controller")
        if isValid(controller) then dumpObject(f, controller, "CONTROLLER") end
        local ps = readProp(nearest, "PlayerState")
        if isValid(ps) then dumpObject(f, ps, "PLAYERSTATE") end
    else
        f:write("no character found near the player\n")
    end
    if player then dumpObject(f, player, "PLAYER PAWN") end
    f:close()
    log("probe written to " .. PROBE_FILE)
end

---------------------------------------------------------------------------
-- wiring
---------------------------------------------------------------------------
RegisterKeyBind(Key.F7, { ModifierKey.CONTROL }, function()
    ExecuteInGameThread(function()
        local ok, err = pcall(probe)
        if not ok then log("probe failed: " .. tostring(err)) end
    end)
end)

RegisterKeyBind(Key.F8, { ModifierKey.CONTROL }, function()
    enabled = not enabled
    log(enabled and "logging resumed" or "logging paused")
end)

LoopAsync(CONFIG.IntervalMs, function()
    if enabled and not busy then
        busy = true
        ExecuteInGameThread(function()
            local ok, err = pcall(snapshot)
            if not ok then log("snapshot failed: " .. tostring(err)) end
            busy = false
        end)
    end
    return false -- keep looping
end)

log("loaded; writing " .. OUT_FILE .. " every " .. CONFIG.IntervalMs .. " ms (Ctrl+F7 probe, Ctrl+F8 pause)")
