-- Redraw the neutral eye and mouth variants in faces.aseprite (owner review 2026-10-31):
-- the half lid follows the slant of the open lash line, the closed eye is a plain lash
-- arc on skin, and the open mouth grows from the middle of the closed mouth line, whose
-- ends (112,65) and (116,67) stay put. Each variant layer keeps only pixels that differ
-- from the neutral face. Indices refer to himari-32.gpl (see face.lua).
local P = app.params
local spr = app.open(P.faces)
local SKIN = 31

local function layer(path)
  local function find(list, name) for _, l in ipairs(list) do if l.name == name then return l end end end
  local group, name = path:match("^(%a+)/(%a+)$")
  if group then return find(find(spr.layers, group).layers, name) end
  return find(spr.layers, path)
end
local base = Image(spr.spec)
base:drawImage(layer("face"):cel(1).image, layer("face"):cel(1).position)

local function row(edits, y, x0, x1, v) for x = x0, x1 do edits[#edits + 1] = { x, y, v } end end
local VARIANTS = {}

local half = {}
row(half, 54, 104, 110, SKIN); row(half, 55, 104, 112, SKIN); half[#half + 1] = { 104, 55, 3 }
row(half, 56, 105, 110, 1); row(half, 56, 111, 113, SKIN)
row(half, 57, 111, 112, 1)
row(half, 60, 121, 127, SKIN)
half[#half + 1] = { 120, 61, 3 }; row(half, 61, 121, 124, 1); row(half, 61, 125, 127, SKIN)
for _, e in ipairs({ { 125, 62, 1 }, { 126, 62, 2 }, { 127, 62, 2 } }) do half[#half + 1] = e end
VARIANTS["eyes/half"] = half

-- The old closed eye minus the lid-shadow fill (index 16) that made it read as a bowl.
local closed = {}
local old = Image(spr.spec)
old:drawImage(base); old:drawImage(layer("eyes/closed"):cel(1).image, layer("eyes/closed"):cel(1).position)
for y = 50, 66 do for x = 101, 133 do
  local v = old:getPixel(x, y)
  closed[#closed + 1] = { x, y, v == 16 and SKIN or v }
end end
VARIANTS["eyes/closed"] = closed

-- The open eye's outer lash tail reads too long on a lowered or closed lid: drop its last
-- two pixels on each eye (owner review 2026-10-31).
for _, edits in ipairs({ half, closed }) do
  for _, e in ipairs({ { 113, 57, SKIN }, { 114, 57, SKIN }, { 127, 62, SKIN }, { 128, 62, SKIN } }) do edits[#edits + 1] = e end
end

VARIANTS["mouth/small"] = {
  { 112, 65, 22 }, { 113, 65, 17 }, { 114, 65, SKIN },
  { 113, 66, 7 }, { 114, 66, 11 }, { 115, 66, 22 },
  { 115, 67, 28 }, { 116, 67, 22 } }
VARIANTS["mouth/wide"] = {
  { 112, 65, 22 }, { 113, 65, 17 }, { 114, 65, 17 }, { 115, 65, 22 },
  { 112, 66, 11 }, { 113, 66, 4 }, { 114, 66, 7 }, { 115, 66, 11 }, { 116, 66, 22 },
  { 113, 67, 11 }, { 114, 67, 21 }, { 115, 67, 17 }, { 116, 67, 22 } }

for path, edits in pairs(VARIANTS) do
  local full = base:clone()
  for _, e in ipairs(edits) do full:drawPixel(e[1], e[2], e[3]) end
  local diff = Image(spr.spec)
  for y = 0, spr.height - 1 do for x = 0, spr.width - 1 do
    local v = full:getPixel(x, y)
    if v ~= base:getPixel(x, y) then diff:drawPixel(x, y, v) end
  end end
  spr:newCel(layer(path), 1, diff, Point(0, 0))
end
spr:saveAs(P.faces)
