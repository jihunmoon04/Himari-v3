-- Build every clip after idle in body.aseprite from the rest pose's part layers
-- (V3-P86): sleep (PR B), then the extras (owner request 2026-10-31): a yawn before
-- dozing and the occasional glance, ahoge sway and halo twinkle. A stretch after waking
-- waits for art with raised arms (owner review: shoulders alone did not read as one). Re-running replaces all of them; it refuses when another clip lies after idle.
--   * Offsets are relative to the rest pose (idle frame 1), down is positive.
--   * The hidden @eyes and @mouth layers' cel data force that frame's eyes or mouth.
--   * A tag's user data "loopFrom=N" (1-based) makes a loop play its first N-1 frames once.
--   * Moving parts opens see-through gaps where they met the still base; each frame's
--     enclosed gaps are filled with the rest pose's pixels on a bottom "fill" layer.
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

local CLIPS = {}
local function frame(ms, upper, hair, halo, extra)
  local f = { ms = ms, upper = upper, hair = hair, halo = halo }
  for k, v in pairs(extra or {}) do f[k] = v end
  return f
end

-- sleep: nod off over 1 s, then one breath in 6 s: 3 s with the head at its lowest, 3 s
-- one pixel up. The hair tail follows 500 ms later and the halo 1 s later.
do
  local frames = {
    frame(400, 0, 0, 0, { eyes = "half" }),
    frame(300, 1, 0, 0, { eyes = "half" }),
    frame(300, 2, 1, 1, { eyes = "closed" }),
  }
  local UPPER = { 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1 }
  local function lag(t, n) local out = {} for i = 1, #t do out[i] = t[(i - n - 1) % #t + 1] end return out end
  local HAIR, HALO = lag(UPPER, 1), lag(UPPER, 2)
  for i = 1, #UPPER do frames[#frames + 1] = frame(500, UPPER[i], HAIR[i], HALO[i], { eyes = "closed", step = i - 1 }) end
  CLIPS[#CLIPS + 1] = { name = "sleep", loopFrom = 4, frames = frames }
end

-- yawn (before dozing): the head tips back a pixel, eyes shut, mouth wide open behind
-- the hand on the cheek, then back.
CLIPS[#CLIPS + 1] = { name = "yawn", frames = {
  frame(200, 0, 0, 0, { eyes = "half" }),
  frame(250, -1, 0, -1, { eyes = "closed", mouth = "small" }),
  frame(1100, -1, -1, -1, { eyes = "closed", mouth = "wide" }),
  frame(250, 0, -1, 0, { eyes = "closed", mouth = "small" }),
  frame(300, 0, 0, 0, { eyes = "half" }),
  frame(100, 0, 0, 0),
} }

-- glance: the idle breath, looking left for 1.65 s and then right for 1.25 s.
CLIPS[#CLIPS + 1] = { name = "glance", frames = {
  frame(600, 0, 0, 0),
  frame(650, 0, 0, 0, { eyes = "left" }),
  frame(250, -1, 0, -1, { eyes = "left" }),
  frame(750, -1, -1, -1, { eyes = "left" }),
  frame(1000, -1, -1, -2, { eyes = "right" }),
  frame(250, 0, -1, -2, { eyes = "right" }),
  frame(500, 0, 0, -1),
  frame(100, 0, 0, 0),
} }

-- ahoge: the loop of hair on top sways right, left and right again, settling.
CLIPS[#CLIPS + 1] = { name = "ahoge", frames = {
  frame(100, 0, 0, 0),
  frame(120, 0, 0, 0, { ahoge = 1 }),
  frame(120, 0, 0, 0, { ahoge = -1 }),
  frame(140, 0, 0, 0, { ahoge = 1 }),
  frame(100, 0, 0, 0),
} }

-- twinkle: the halo's star flashes longer rays and glints, twice.
CLIPS[#CLIPS + 1] = { name = "twinkle", frames = {
  frame(100, 0, 0, 0),
  frame(90, 0, 0, 0, { twinkle = 1 }),
  frame(140, 0, 0, 0, { twinkle = 2 }),
  frame(90, 0, 0, 0, { twinkle = 1 }),
  frame(120, 0, 0, 0),
  frame(90, 0, 0, 0, { twinkle = 1 }),
  frame(100, 0, 0, 0),
} }

-- Each z lives one sleep loop (12 steps): it appears small and faint beside the head,
-- grows from 3 to 7 px as it rises and drifts right, then fades out at the top. Three of
-- them, 4 steps apart, so "zzz" is always on screen. Glyphs are slanted with offset bars,
-- and the outline skips diagonals, so corners read round.
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

-- Ahoge rows (canvas, rest pose) that sway; the rows below stay rooted on the head.
local AHOGE = { x0 = 108, x1 = 128, y0 = 20, y1 = 25 }
local function sway(img, pos, dx)
  local out = img:clone()
  for y = AHOGE.y0, AHOGE.y1 do
    for x = AHOGE.x0, AHOGE.x1 do out:drawPixel(x - pos.x, y - pos.y, 0) end
    for x = AHOGE.x0, AHOGE.x1 do
      local v = img:getPixel(x - pos.x, y - pos.y)
      if v ~= 0 then out:drawPixel(x + dx - pos.x, y - pos.y, v) end
    end
  end
  return out
end
-- A four-point sparkle that pops above the halo's right end (canvas, rest pose): small
-- at level 1, rays two pixels long at level 2, while the star's own rays grow a pixel.
local SX, SY = 131, 10
local TWINKLE = {
  [1] = { { SX, SY, 32 }, { SX - 1, SY, 25 }, { SX + 1, SY, 25 }, { SX, SY - 1, 25 }, { SX, SY + 1, 25 } },
  [2] = { { SX, SY, 32 }, { SX - 1, SY, 32 }, { SX + 1, SY, 32 }, { SX, SY - 1, 32 }, { SX, SY + 1, 32 },
          { SX - 2, SY, 25 }, { SX + 2, SY, 25 }, { SX, SY - 2, 25 }, { SX, SY + 2, 25 },
          { 110, 6, 32 }, { 110, 5, 25 }, { 109, 23, 32 }, { 109, 24, 25 } },
}
local function twinkle(img, pos, level)
  -- The halo cel is trimmed; draw on a canvas-sized copy so new pixels fit.
  local out = Image(spr.width, spr.height, ColorMode.INDEXED)
  for it in img:pixels() do if it() ~= 0 then out:drawPixel(pos.x + it.x, pos.y + it.y, it()) end end
  for _, p in ipairs(TWINKLE[level]) do out:drawPixel(p[1], p[2], p[3]) end
  return out, Point(0, 0)
end

local function find(list, name) for _, l in ipairs(list) do if l.name == name then return l end end end
local function layer(name, hidden)
  local l = find(spr.layers, name)
  if not l then l = spr:newLayer(); l.name = name; l.isVisible = not hidden end
  return l
end

-- Drop the old clips: every frame after idle. Frames added right after a tag join that
-- tag, so idle's end is put back once the new frames are in.
local idleTag = find(spr.tags, "idle")
local owned = {}
for _, c in ipairs(CLIPS) do owned[c.name] = true end
local idleEnd = idleTag.toFrame.frameNumber
for _, t in ipairs(spr.tags) do
  if t ~= idleTag and owned[t.name] then idleEnd = math.min(idleEnd, t.fromFrame.frameNumber - 1) end
end
for _, t in ipairs(spr.tags) do
  if t ~= idleTag and t.toFrame.frameNumber > idleEnd and not owned[t.name] then
    error("clips: " .. t.name .. " lies after idle and is not built here", 0)
  end
end
-- Every frame about to be deleted must belong to a clip built here; untagged frames
-- could be hand work (PR #145 review).
for n = idleEnd + 1, #spr.frames do
  local covered = false
  for _, t in ipairs(spr.tags) do
    if owned[t.name] and t.fromFrame.frameNumber <= n and n <= t.toFrame.frameNumber then covered = true end
  end
  if not covered then error("clips: frame " .. n .. " after idle belongs to no clip built here", 0) end
end
for i = #spr.tags, 1, -1 do if spr.tags[i] ~= idleTag then spr:deleteTag(spr.tags[i]) end end
while #spr.frames > idleEnd do spr:deleteFrame(#spr.frames) end
local zLayer, eyesLayer, mouthLayer, fillLayer = layer("zzz"), layer("@eyes", true), layer("@mouth", true), layer("fill")
fillLayer.stackIndex = 1
local own = { [zLayer] = true, [eyesLayer] = true, [mouthLayer] = true, [fillLayer] = true }
local upperBody = find(find(spr.layers, "upper (breathing)").layers, "body")
local haloLayer = find(spr.layers, "halo")

local function place(l, dst, dy, f)
  if l.isGroup then for _, c in ipairs(l.layers) do place(c, dst, dy, f) end; return end
  local cel = l:cel(1)
  if not cel then return end
  local img, pos = cel.image:clone(), cel.position
  if l == upperBody and f.ahoge then img = sway(img, pos, f.ahoge) end
  if l == haloLayer and f.twinkle then img, pos = twinkle(img, pos, f.twinkle) end
  spr:newCel(l, dst, img, Point(pos.x, pos.y + dy))
end
local function mark(l, n, value)
  if not value then return end
  local dot = Image(1, 1, ColorMode.INDEXED); dot:drawPixel(0, 0, 1)
  spr:newCel(l, n, dot, Point(0, 0)).data = value
end

local function flat(n) local img = Image(spr.spec); img:drawSprite(spr, n); return img end
local rest = flat(1)
-- Enclosed gaps: transparent pixels the outside cannot reach through transparent pixels.
local function fillGaps(n)
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
  return holes
end

local log = io.open(P.log, "w")
local spans = {}
for _, clip in ipairs(CLIPS) do
  local first = #spr.frames + 1
  for i, f in ipairs(clip.frames) do
    local n = first + i - 1
    spr:newEmptyFrame(n)
    spr.frames[n].duration = f.ms / 1000
    for _, l in ipairs(spr.layers) do
      local dy = ({ ["upper (breathing)"] = f.upper, ["hair tail"] = f.hair, halo = f.halo, ["@face"] = f.upper })[l.name]
      if not own[l] then place(l, n, dy or 0, f) end
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
    mark(eyesLayer, n, f.eyes)
    mark(mouthLayer, n, f.mouth)
    log:write(string.format("%s frame %d: filled %d enclosed pixels\n", clip.name, i, fillGaps(n)))
  end
  spans[#spans + 1] = { clip, first, first + #clip.frames - 1 }
end
log:close()
idleTag.toFrame = spr.frames[idleEnd]
for _, s in ipairs(spans) do
  local tag = spr:newTag(s[2], s[3]); tag.name = s[1].name
  if s[1].loopFrom then tag.data = "loopFrom=" .. s[1].loopFrom else tag.repeats = 1 end
end
spr:saveAs(P.body)
