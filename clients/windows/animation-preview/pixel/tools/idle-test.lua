-- Build the idle breathing loop from the part layers: one breath every 4 s, about 2 s up
-- and 2 s down. The upper body lifts 1 px, the hair tail follows 250 ms later in the same
-- direction and the halo floats by up to 2 px a little behind. Reports pixels that become
-- transparent while moving.
local P = app.params
local spr = app.open(P["in"])
local OFFSETS = {
  ["upper (breathing)"] = { 0, -1, -1, -1, 0, 0 },
  ["hair tail"] = { 0, 0, -1, -1, -1, 0 },
  ["halo"] = { 0, -1, -1, -2, -2, -1 },
}
local DURATION = { 1250, 250, 750, 1000, 250, 500 }
for _ = 2, #DURATION do spr:newFrame() end
for i, d in ipairs(DURATION) do spr.frames[i].duration = d / 1000 end

local function move(layer, dy)
  local function apply(l)
    if l.isGroup then for _, c in ipairs(l.layers) do apply(c) end; return end
    for i, off in ipairs(dy) do
      local cel = l:cel(i)
      if cel then cel.position = Point(cel.position.x, cel.position.y + off) end
    end
  end
  apply(layer)
end
for _, layer in ipairs(spr.layers) do
  if OFFSETS[layer.name] then move(layer, OFFSETS[layer.name]) end
end
local tag = spr:newTag(1, #DURATION); tag.name = "idle"
spr:saveAs(P.out)

local log = io.open(P.log, "w")
local function flat(frame)
  local img = Image(spr.spec)
  img:drawSprite(spr, frame)
  return img
end
local rest = flat(1)
local halo
for _, l in ipairs(spr.layers) do if l.name == "halo" then halo = l:cel(1).image end end
for f = 2, #DURATION do
  local img, holes = flat(f), 0
  for y = 0, spr.height - 1 do
    for x = 0, spr.width - 1 do
      -- A hole: opaque at rest, transparent now, and not just vacated by a part's outer edge.
      -- The floating halo is drawn over empty sky, so its own vacated pixels do not count.
      if rest:getPixel(x, y) ~= 0 and img:getPixel(x, y) == 0 and y < spr.height - 1 and rest:getPixel(x, y + 1) ~= 0
         and y > 0 and rest:getPixel(x, y - 1) ~= 0 and halo:getPixel(x, y) == 0 then holes = holes + 1 end
    end
  end
  log:write(string.format("frame %d: interior holes %d\n", f, holes))
end
log:close()

app.command.ExportSpriteSheet { ui = false, type = SpriteSheetType.HORIZONTAL, textureFilename = P.sheet, splitLayers = false }
spr:saveCopyAs(P.gif)
