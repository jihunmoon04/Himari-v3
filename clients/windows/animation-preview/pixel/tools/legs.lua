-- Replace the legs of the rest pose in body.aseprite's idle frames with clean/poses/legs.png
-- (owner choice 2026-10-31: longer thighs, knees at the seat edge). The box is in rest-pose
-- coordinates; legs are still, so they go to the base layer, and the upper body's lap pixels
-- that the new thighs cover move there too. Run after sources.lua only=body, before clips.lua:
--   aseprite -b --script-param body=clean/body.aseprite --script-param conv=clean/poses/legs.png
--     --script-param neutral=clean/neutral.png --script-param log=legs.txt --script tools/legs.lua
local P = app.params
local s = app.open(P.body)
local conv = Image{ fromFile = P.conv }; local neu = Image{ fromFile = P.neutral }
-- Below NEWBELOW only pixels neutral already had are kept: the screenshot edge left stray dots there.
local X0, Y0, X1, Y1, NEWBELOW = 100, 106, 176, 195, 186
local function L(name, layers) for _, l in ipairs(layers or s.layers) do if l.name == name then return l end end end
local base, under, body = L("base"), L("underlay"), L("body", L("upper (breathing)").layers)
local log = io.open(P.log, "w")
local moved, set = 0, 0
for f = 1, 6 do
  local bi, ui, yi = base:cel(f).image:clone(), under:cel(f).image:clone(), body:cel(f).image:clone()
  for y = Y0, Y1 do for x = X0, X1 do
    local v = conv:getPixel(x, y)
    if y >= NEWBELOW and neu:getPixel(x, y) == 0 then v = 0 end
    if bi:getPixel(x, y) ~= v then set = set + 1 end
    bi:drawPixel(x, y, v)
    if ui:getPixel(x, y) ~= 0 then ui:drawPixel(x, y, v) end
    if yi:getPixel(x, y) ~= 0 and v ~= neu:getPixel(x, y) then yi:drawPixel(x, y, 0); moved = moved + 1 end
  end end
  base:cel(f).image = bi; under:cel(f).image = ui; body:cel(f).image = yi
end
-- The upper body's lap edge (row above the box) now rests on thighs moved to base: copy it
-- to the underlay so breathing up 1px leaves no gap there, and close rest-pose pinholes.
local rest = Image(s.width, s.height, ColorMode.INDEXED); rest:drawSprite(s, 1)
for f = 1, 6 do
  local bi, ui = base:cel(f).image:clone(), under:cel(f).image:clone()
  for y = Y0 - 4, Y1 do for x = X0, X1 do
    local inner = rest:getPixel(x, y) == 0 and rest:getPixel(x - 1, y) ~= 0 and rest:getPixel(x + 1, y) ~= 0 and rest:getPixel(x, y - 1) ~= 0 and rest:getPixel(x, y + 1) ~= 0
    if inner then bi:drawPixel(x, y, rest:getPixel(x - 1, y)) end
    if y < Y0 and bi:getPixel(x, y) == 0 and ui:getPixel(x, y) == 0 and rest:getPixel(x, y) ~= 0 then ui:drawPixel(x, y, rest:getPixel(x, y)) end
  end end
  base:cel(f).image = bi; under:cel(f).image = ui
end
-- Gaps: pixels transparent in a breathing frame but opaque around in the rest pose.
for f = 1, 6 do
  local flat = Image(s.width, s.height, ColorMode.INDEXED); flat:drawSprite(s, f)
  local holes = 0
  for y = 1, s.height - 2 do for x = 1, s.width - 2 do
    if flat:getPixel(x, y) == 0 and flat:getPixel(x - 1, y) ~= 0 and flat:getPixel(x + 1, y) ~= 0 and flat:getPixel(x, y - 1) ~= 0 and flat:getPixel(x, y + 1) ~= 0 then holes = holes + 1 end
  end end
  log:write(string.format("frame %d holes %d\n", f, holes))
end
log:write(string.format("base set %d, body moved %d\n", set, moved))
log:close()
s:saveAs(P.out or P.body)
