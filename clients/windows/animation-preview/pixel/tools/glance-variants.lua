-- Draw the glance eye variants (eyes/left, eyes/right) in faces.aseprite: the iris of
-- each eye moves one pixel sideways inside the lashes, and the eye white (index 30) fills
-- the side it leaves. Happy has none (its eyes are closed in a smile).
-- Each variant layer keeps only pixels that differ from the mood face.
-- Usage: aseprite -b --script-param faces=clean/faces.aseprite --script tools/glance-variants.lua
local P = app.params
local spr = app.open(P.faces)
local WHITE = 30

-- Iris and highlight colours; whites the iris may move onto.
local IRIS, OPEN = {}, {}
for _, i in ipairs({ 4, 7, 8, 9, 11, 13, 15, 16, 17, 18, 19, 21, 23, 25, 32 }) do IRIS[i] = true; OPEN[i] = true end
OPEN[29] = true; OPEN[30] = true
local BOXES = { { x0 = 103, x1 = 114, y0 = 52, y1 = 59 }, { x0 = 119, x1 = 129, y0 = 57, y1 = 64 } }

local function find(list, name) for _, l in ipairs(list) do if l.name == name then return l end end end
local function layer(path)
  local group, name = path:match("^(%a+)/(%a+)$")
  local g = find(spr.layers, group)
  local l = find(g.layers, name)
  if not l then l = spr:newLayer(); l.name = name; l.parent = g end
  return l
end
-- Image:drawImage maps index 32 to 0 here, so cels are copied pixel by pixel.
local function paste(dst, cel)
  if not cel then return end
  for it in cel.image:pixels() do if it() ~= 0 then dst:drawPixel(cel.position.x + it.x, cel.position.y + it.y, it()) end end
end

local function glance(base, dx)
  local out = base:clone()
  for _, b in ipairs(BOXES) do
    for y = b.y0, b.y1 do
      local moved = {}
      for x = b.x0, b.x1 do
        local v = base:getPixel(x, y)
        if IRIS[v] then
          if out:getPixel(x, y) == v and not moved[x] then out:drawPixel(x, y, WHITE) end
          local tx = x + dx
          if tx >= b.x0 and tx <= b.x1 and OPEN[base:getPixel(tx, y)] then out:drawPixel(tx, y, v); moved[tx] = true end
        end
      end
    end
  end
  return out
end

local layers = { left = layer("eyes/left"), right = layer("eyes/right") }
for _, tag in ipairs(spr.tags) do
  if tag.name ~= "happy" then
    local f = tag.fromFrame.frameNumber
    local base = Image(spr.spec)
    paste(base, find(spr.layers, "@body"):cel(f))
    paste(base, find(spr.layers, "face"):cel(f))
    for name, dx in pairs({ left = -1, right = 1 }) do
      local full, diff = glance(base, dx), Image(spr.spec)
      for y = 0, spr.height - 1 do for x = 0, spr.width - 1 do
        local v = full:getPixel(x, y)
        if v ~= base:getPixel(x, y) then diff:drawPixel(x, y, v) end
      end end
      spr:newCel(layers[name], f, diff, Point(0, 0))
    end
  end
end
spr:saveAs(P.out or P.faces)
