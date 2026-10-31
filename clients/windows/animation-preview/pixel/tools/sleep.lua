-- Add the sleep clip to body.aseprite (V3-P86 PR B): the head nods down over about a
-- second while the eyes go half then fully closed, then a slow 6 s breath with the head
-- 1-2 px lower and three pastel z's growing and fading as they rise beside it. Frames before the loop play once; the
-- tag's user data "loopFrom=N" (1-based) says where the loop starts and the hidden @eyes
-- layer's cel data forces the eyes for that frame. Re-running replaces the clip.
-- Moving parts down opens see-through gaps where they met the still base; each frame's
-- enclosed gaps are filled with the rest pose's pixels on a bottom "fill" layer.
local P = app.params
local spr = app.open(P.body)
-- z fill and outline (himari-32.gpl 33 and 34, added for the z's: the character has no
-- blue). Soft pastels so they sit with the character (owner review 2026-10-31).
local Z, ZLINE = 33, 34
-- palette=<gpl> copies the palette into body.aseprite and faces=<file> (export.lua wants both equal).
if P.palette then
  local pal = Palette { fromFile = P.palette }
  spr:setPalette(pal)
  if P.faces then local f = app.open(P.faces); f:setPalette(pal); f:saveAs(P.faces); f:close() end
  app.activeSprite = spr
end

-- Offsets are relative to the rest pose (idle frame 1), down is positive.
local INTRO = {
  { ms = 400, upper = 0, hair = 0, halo = 0, eyes = "half" },
  { ms = 300, upper = 1, hair = 0, halo = 0, eyes = "half" },
  { ms = 300, upper = 2, hair = 1, halo = 1, eyes = "closed" },
}
-- One breath in 6 s: 3 s with the head at its lowest, 3 s one pixel up. The hair tail
-- follows 500 ms later and the halo 1 s later, in the same direction.
local UPPER = { 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1 }
local STEP = 500
local function lag(t, n) local out = {} for i = 1, #t do out[i] = t[(i - n - 1) % #t + 1] end return out end
local HAIR, HALO = lag(UPPER, 1), lag(UPPER, 2)

-- Each z lives one loop (12 steps): it appears small and faint beside the head, grows
-- from 3 to 7 px as it rises and drifts right, then fades out at the top. Three of them,
-- 4 steps apart, so "zzz" is always on screen. Glyphs are slanted with offset bars, and
-- the outline skips diagonals, so corners read round.
local GLYPHS = {
  [3] = { "XX.", ".X.", ".XX" },
  [4] = { "XXX.", "..X.", ".X..", ".XXX" },
  [5] = { "XXXX.", "...X.", "..X..", ".X...", ".XXXX" },
  [6] = { "XXXXX.", "....X.", "...X..", "..X...", ".X....", ".XXXXX" },
  [7] = { "XXXXXX.", ".....X.", "....X..", "...X...", "..X....", ".X.....", ".XXXXXX" },
}
local SIZES = { 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 7, 7 }
local function zImage(step)
  local glyph, size = GLYPHS[SIZES[step + 1]], SIZES[step + 1]
  local img = Image(size + 2, size + 2, ColorMode.INDEXED)
  local stroke = {}
  for y, row in ipairs(glyph) do for x = 1, #row do if row:sub(x, x) == "X" then stroke[#stroke + 1] = { x, y } end end end
  -- Steps 1-9 have an outline. Without one the first and last two read faint on any
  -- desktop (a paler colour would glow on dark ones); the very last is also dithered.
  local solid = step >= 1 and step <= 9
  if solid then
    for _, p in ipairs(stroke) do
      for _, d in ipairs({ { 1, 0 }, { -1, 0 }, { 0, 1 }, { 0, -1 } }) do img:drawPixel(p[1] + d[1], p[2] + d[2], ZLINE) end
    end
  end
  for _, p in ipairs(stroke) do
    if solid then img:drawPixel(p[1], p[2], Z)
    elseif step ~= 11 or (p[1] + p[2]) % 2 == 0 then img:drawPixel(p[1], p[2], Z) end
  end
  return img
end
local function zAt(step)
  return zImage(step), Point(146 + math.floor(step * 1.3 + .5), 48 - math.floor(step * 3 + .5) - SIZES[step + 1])
end


local function find(list, name) for _, l in ipairs(list) do if l.name == name then return l end end end
local function layer(name, hidden)
  local l = find(spr.layers, name)
  if not l then l = spr:newLayer(); l.name = name; l.isVisible = not hidden end
  return l
end

-- Drop an old sleep clip (always the frames after idle). Frames added right after a tag
-- join that tag, so idle's end is put back once the new frames are in.
local idleTag, oldSleep = find(spr.tags, "idle"), find(spr.tags, "sleep")
local idleEnd = oldSleep and oldSleep.fromFrame.frameNumber - 1 or idleTag.toFrame.frameNumber
if oldSleep then spr:deleteTag(oldSleep) end
while #spr.frames > idleEnd do spr:deleteFrame(#spr.frames) end
local zLayer, eyesLayer, fillLayer = layer("zzz"), layer("@eyes", true), layer("fill")
fillLayer.stackIndex = 1
local marker = find(spr.layers, "@face")

local frames = {}
for _, f in ipairs(INTRO) do frames[#frames + 1] = f end
local loopFrom = #frames + 1
for i = 1, #UPPER do
  frames[#frames + 1] = { ms = STEP, upper = UPPER[i], hair = HAIR[i], halo = HALO[i], eyes = "closed", step = i - 1 }
end

local first = #spr.frames + 1
local function place(l, src, dst, dy)
  if l.isGroup then for _, c in ipairs(l.layers) do place(c, src, dst, dy) end; return end
  local cel = l:cel(src)
  if cel then spr:newCel(l, dst, cel.image:clone(), Point(cel.position.x, cel.position.y + dy)) end
end
for i, f in ipairs(frames) do
  local n = first + i - 1
  spr:newEmptyFrame(n)
  spr.frames[n].duration = f.ms / 1000
  for _, l in ipairs(spr.layers) do
    local dy = ({ ["upper (breathing)"] = f.upper, ["hair tail"] = f.hair, halo = f.halo, ["@face"] = f.upper })[l.name]
    if l ~= zLayer and l ~= eyesLayer and l ~= fillLayer then place(l, 1, n, dy or 0) end
  end
  if f.step then
    local zs = Image(spr.spec)
    for k = 0, 2 do
      local img, pos = zAt((f.step + 4 * k) % 12)
      -- Pixel by pixel: drawImage drops some palette indices (see README).
      for it in img:pixels() do if it() ~= 0 then zs:drawPixel(pos.x + it.x, pos.y + it.y, it()) end end
    end
    spr:newCel(zLayer, n, zs, Point(0, 0))
  end
  local dot = Image(1, 1, ColorMode.INDEXED); dot:drawPixel(0, 0, 1)
  spr:newCel(eyesLayer, n, dot, Point(0, 0)).data = f.eyes
end
-- Enclosed gaps: transparent pixels the outside cannot reach through transparent pixels.
local function flat(frame) local img = Image(spr.spec); img:drawSprite(spr, frame); return img end
local rest = flat(1)
local log = io.open(P.log, "w")
for i = 2, #frames do
  local n = first + i - 1
  local img, W, H = flat(n), spr.width, spr.height
  local seen, stack = {}, {}
  local function push(x, y)
    local k = y * W + x
    if x >= 0 and y >= 0 and x < W and y < H and not seen[k] and img:getPixel(x, y) == 0 then seen[k] = true; stack[#stack + 1] = k end
  end
  for x = 0, W - 1 do push(x, 0); push(x, H - 1) end
  for y = 0, H - 1 do push(0, y); push(W - 1, y) end
  while #stack > 0 do
    local k = table.remove(stack); local x, y = k % W, k // W
    push(x + 1, y); push(x - 1, y); push(x, y + 1); push(x, y - 1)
  end
  local fill, holes = Image(spr.spec), 0
  for y = 0, H - 1 do for x = 0, W - 1 do
    if img:getPixel(x, y) == 0 and not seen[y * W + x] and rest:getPixel(x, y) ~= 0 then
      fill:drawPixel(x, y, rest:getPixel(x, y)); holes = holes + 1
    end
  end end
  if holes > 0 then spr:newCel(fillLayer, n, fill, Point(0, 0)) end
  log:write(string.format("sleep frame %d: filled %d enclosed pixels\n", i, holes))
end
log:close()
idleTag.toFrame = spr.frames[idleEnd]
local tag = spr:newTag(first, first + #frames - 1); tag.name = "sleep"; tag.data = "loopFrom=" .. loopFrom
spr:saveAs(P.body)
