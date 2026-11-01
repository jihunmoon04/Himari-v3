-- Export body.aseprite and faces.aseprite to the app's character.png and character.json
-- (docs/companion-animation-v0.1.md §3), plus reference renders for the UI tests.
-- Fails on anything the app cannot rely on: palette, rest pose, face marker, mood list.
local P = app.params
local FX, FY, FW, FH = 101, 47, 33, 26
local MOODS = { "neutral", "happy", "excited", "sad", "surprised", "thinking", "embarrassed", "worried" }
local function fail(msg) error("export: " .. msg, 0) end

local palette = Palette { fromFile = P.palette }
local function checkPalette(spr)
  local p = spr.palettes[1]
  if #p ~= #palette then fail(spr.filename .. " palette has " .. #p .. " colours, expected " .. #palette) end
  for i = 1, #p - 1 do
    if p:getColor(i).rgbaPixel ~= palette:getColor(i).rgbaPixel then fail(spr.filename .. " palette differs at index " .. i) end
  end
  if spr.colorMode ~= ColorMode.INDEXED then fail(spr.filename .. " is not indexed") end
end

local function layers(spr)
  local out = {}
  local function walk(list, prefix)
    for _, l in ipairs(list) do
      local name = prefix .. l.name
      if l.isGroup then walk(l.layers, name .. "/") else out[name] = l end
    end
  end
  walk(spr.layers, "")
  return out
end
-- A layer's cel drawn on a canvas-sized image (cels are trimmed when saved).
local function celImage(spr, layer, frame)
  local img = Image(spr.width, spr.height, ColorMode.INDEXED)
  local cel = layer:cel(frame)
  if cel then img:drawImage(cel.image, cel.position) end
  return img
end
local function crop(img, x, y, w, h)
  local out = Image(w, h, ColorMode.INDEXED)
  out:drawImage(img, Point(-x, -y))
  return out
end
local function bounds(img)
  local x0, y0, x1, y1 = math.huge, math.huge, -1, -1
  for it in img:pixels() do
    if it() ~= 0 then
      x0, y0 = math.min(x0, it.x), math.min(y0, it.y)
      x1, y1 = math.max(x1, it.x), math.max(y1, it.y)
    end
  end
  if x1 < 0 then return nil end
  return x0, y0, x1 - x0 + 1, y1 - y0 + 1
end

-- Sheet: identical images are stored once.
local pieces, byKey = {}, {}
local function add(img)
  local key = img.width .. "x" .. img.height .. ":" .. img.bytes
  if not byKey[key] then
    pieces[#pieces + 1] = { img = img }
    byKey[key] = pieces[#pieces]
  end
  return byKey[key]
end

-- Body clips.
local body = app.open(P.body)
checkPalette(body)
local W, H = body.width, body.height
local bodyLayers = layers(body)
local marker = bodyLayers["@face"] or fail("body.aseprite has no @face layer")
-- Optional: a cel's user data forces that frame's eyes or mouth.
local eyesMarker, mouthMarker = bodyLayers["@eyes"], bodyLayers["@mouth"]
local clips, restKey = {}, nil
local function flatten(frame)
  local img = Image(W, H, ColorMode.INDEXED)
  img:drawSprite(body, frame) -- hidden layers (the @ markers) are not drawn
  return img
end
for _, tag in ipairs(body.tags) do
  local frames = {}
  -- A loop may play some opening frames once: user data "loopFrom=N" (1-based) on the tag.
  local raw = (tag.data or ""):match("loopFrom=(%S*)")
  local loopFrom = raw == nil and 1 or (raw:match("^%d+$") and tonumber(raw))
  if not loopFrom then fail(tag.name .. " has a malformed loopFrom: " .. raw) end
  if loopFrom < 1 or loopFrom > tag.frames or (loopFrom > 1 and tag.repeats ~= 0) then fail(tag.name .. " has a bad loopFrom") end
  for f = tag.fromFrame.frameNumber, tag.toFrame.frameNumber do
    local cel = marker:cel(f) or fail("frame " .. f .. " of " .. tag.name .. " has no @face marker")
    local img = flatten(f)
    frames[#frames + 1] = { piece = add(img), ms = math.floor(body.frames[f].duration * 1000 + .5),
                            face = { cel.position.x - FX, cel.position.y - FY }, img = img }
    -- "own" on the face marker: the frame is a drawing with its own face; draw no mood face.
    if cel.data == "own" then frames[#frames].ownFace = true end
    for key, l in pairs({ eyes = eyesMarker, mouth = mouthMarker }) do
      local forced = l:cel(f)
      if forced and forced.data ~= "" then frames[#frames][key] = forced.data end
    end
  end
  clips[#clips + 1] = { name = tag.name, loop = tag.repeats == 0, loopFrom = loopFrom - 1, frames = frames }
  if tag.name == "idle" then restKey = frames[1].piece end
end
if not restKey then fail("body.aseprite has no idle tag") end
for i, a in ipairs(body.tags) do
  for j = i + 1, #body.tags do
    local b = body.tags[j]
    if a.fromFrame.frameNumber <= b.toFrame.frameNumber and b.fromFrame.frameNumber <= a.toFrame.frameNumber then fail(a.name .. " and " .. b.name .. " share frames") end
  end
end
for _, c in ipairs(clips) do
  if c.frames[1].piece ~= restKey then fail(c.name .. " does not start from the rest pose (idle frame 1)") end
  -- A one-shot hands back to idle frame 1, so it must also end there (a loop wraps to its own start).
  if not c.loop and c.frames[#c.frames].piece ~= restKey then fail(c.name .. " does not end on the rest pose (idle frame 1)") end
end

-- Faces.
local faces = app.open(P.faces)
checkPalette(faces)
local faceLayers = layers(faces)
local faceLayer = faceLayers["face"] or fail("faces.aseprite has no face layer")
local moods = {}
for _, tag in ipairs(faces.tags) do
  local f = tag.fromFrame.frameNumber
  local mood = { name = tag.name, piece = add(crop(celImage(faces, faceLayer, f), FX, FY, FW, FH)), eyes = {}, mouth = {} }
  local names = {} -- sorted so the sheet layout is the same on every run
  for name in pairs(faceLayers) do names[#names + 1] = name end
  table.sort(names)
  for _, name in ipairs(names) do
    local layer = faceLayers[name]
    local group, variant = name:match("^(%a+)/(%a+)$")
    if (group == "eyes" or group == "mouth") and layer:cel(f) then
      local full = celImage(faces, layer, f)
      local x, y, w, h = bounds(full)
      if x then mood[group][variant] = { piece = add(crop(full, x, y, w, h)), at = { x - FX, y - FY } } end
    end
  end
  moods[tag.name] = mood
end
for _, m in ipairs(MOODS) do if not moods[m] then fail("faces.aseprite has no " .. m .. " tag") end end

-- Shelf packing, 1024 px wide.
local x, y, rowH, sheetW = 0, 0, 0, 0
for _, p in ipairs(pieces) do
  if x + p.img.width > 1024 then x, y, rowH = 0, y + rowH, 0 end
  p.x, p.y = x, y
  x = x + p.img.width; rowH = math.max(rowH, p.img.height); sheetW = math.max(sheetW, x)
end
local sheet = Sprite(sheetW, y + rowH, ColorMode.INDEXED)
sheet:setPalette(palette); sheet.transparentColor = 0
local sheetImg = Image(sheetW, y + rowH, ColorMode.INDEXED)
for _, p in ipairs(pieces) do sheetImg:drawImage(p.img, Point(p.x, p.y)) end
sheet.cels[1].image = sheetImg
sheet:saveAs(app.fs.joinPath(P.out, "character.png"))

-- character.json, written by hand for a stable, readable diff.
local function src(p) return string.format("[%d, %d, %d, %d]", p.x, p.y, p.img.width, p.img.height) end
local lines = { "{", '  "version": 1,', string.format('  "canvas": [%d, %d],', W, H), '  "sheet": "character.png",',
  string.format('  "face": { "origin": [%d, %d], "size": [%d, %d] },', FX, FY, FW, FH), '  "clips": {' }
for ci, c in ipairs(clips) do
  local from = c.loopFrom > 0 and string.format(', "loopFrom": %d', c.loopFrom) or ""
  lines[#lines + 1] = string.format('    "%s": { "loop": %s%s, "frames": [', c.name, tostring(c.loop), from)
  for fi, f in ipairs(c.frames) do
    local eyes = (f.eyes and string.format(', "eyes": "%s"', f.eyes) or "") .. (f.mouth and string.format(', "mouth": "%s"', f.mouth) or "")
      .. (f.ownFace and ', "ownFace": true' or "")
    lines[#lines + 1] = string.format('      { "src": %s, "ms": %d, "face": [%d, %d]%s }%s', src(f.piece), f.ms, f.face[1], f.face[2], eyes, fi < #c.frames and "," or "")
  end
  lines[#lines + 1] = "    ] }" .. (ci < #clips and "," or "")
end
lines[#lines + 1] = "  },"
lines[#lines + 1] = '  "faces": {'
local function variants(set)
  local names = {}
  for n in pairs(set) do names[#names + 1] = n end
  table.sort(names)
  local parts = {}
  for _, n in ipairs(names) do
    local v = set[n]
    parts[#parts + 1] = string.format('"%s": { "src": %s, "at": [%d, %d] }', n, src(v.piece), v.at[1], v.at[2])
  end
  return "{ " .. table.concat(parts, ", ") .. " }"
end
for mi, name in ipairs(MOODS) do
  local m = moods[name]
  local extra = ""
  if next(m.eyes) then extra = extra .. ', "eyes": ' .. variants(m.eyes) end
  if next(m.mouth) then extra = extra .. ', "mouth": ' .. variants(m.mouth) end
  lines[#lines + 1] = string.format('    "%s": { "src": %s%s }%s', name, src(m.piece), extra, mi < #MOODS and "," or "")
end
lines[#lines + 1] = "  }"
lines[#lines + 1] = "}"
local json = io.open(app.fs.joinPath(P.out, "character.json"), "w")
json:write(table.concat(lines, "\n"), "\n")
json:close()

-- Reference renders for the UI tests: body frame, face at its marker, then eyes and mouth.
local function render(clip, frame, mood, eyes, mouth)
  local c
  for _, k in ipairs(clips) do if k.name == clip then c = k end end
  local f = c.frames[frame]
  local img = f.img:clone()
  local ox, oy = FX + f.face[1], FY + f.face[2]
  if f.ownFace then mood, eyes, mouth = nil, nil, nil end
  if mood then img:drawImage(moods[mood].piece.img, Point(ox, oy)) end
  for _, v in ipairs({ eyes and moods[mood].eyes[eyes] or false, mouth and moods[mood].mouth[mouth] or false }) do
    if v then img:drawImage(v.piece.img, Point(ox + v.at[1], oy + v.at[2])) end
  end
  local s = Sprite(W, H, ColorMode.INDEXED)
  s:setPalette(palette); s.transparentColor = 0
  s.cels[1].image = img
  return s
end
local refs = {
  { "idle-1-neutral", "idle", 1, "neutral" },
  { "idle-2-happy", "idle", 2, "happy" },
  { "idle-1-neutral-closed-wide", "idle", 1, "neutral", "closed", "wide" },
  { "sleep-4-neutral-closed", "sleep", 4, "neutral", "closed" },
  { "yawn-2-happy", "yawn", 2, "happy" },
  { "glance-2-sad-left", "glance", 2, "sad", "left" },
  { "ahoge-2-neutral", "ahoge", 2, "neutral" },
  { "twinkle-3-neutral", "twinkle", 3, "neutral" },
  -- stretch: a blink on a face moved by (-1, 0), a mood face at (-1, 1) over new legs, an
  -- own-face frame (the mood must not show) and the lifted upper body.
  { "stretch-4-sad-closed", "stretch", 4, "sad", "closed" },
  { "stretch-5-worried", "stretch", 5, "worried" },
  { "stretch-8-happy", "stretch", 8, "happy" },
  { "stretch-9-neutral", "stretch", 9, "neutral" },
}
for _, r in ipairs(refs) do
  local s = render(r[2], r[3], r[4], r[5], r[6])
  s:saveAs(app.fs.joinPath(P.refs, r[1] .. ".png")); s:close()
end
print(string.format("export: %d pieces, sheet %dx%d, %d clips, %d moods", #pieces, sheetW, y + rowH, #clips, #MOODS))
