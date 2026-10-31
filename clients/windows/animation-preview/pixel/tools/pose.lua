-- Convert one generated pose image (requests/) to the neutral pixel grid: find the grid
-- offset that best matches neutral, downsample with the medoid rule (as mood.lua), map to
-- himari-32.gpl, align, and write the whole converted sprite plus a diff map against
-- neutral (changed pixels bright, unchanged dimmed) to decide what to composite.
-- Usage: aseprite -b --script-param in=requests/big-motions-v1/yawn-hand-2.png
--   --script-param neutral=clean/neutral.png --script-param out=x.png --script-param diff=d.png --script tools/pose.lua
-- A smaller copy (a pasted screenshot) takes its dot size and where neutral's (0, 0) falls:
--   --script-param sx=2.24 --script-param sy=2.24 --script-param ox=-52 --script-param oy=-7
-- and bg=corner turns the top-left colour into transparency.
local P = app.params
local SX, SY = tonumber(P.sx) or 6.02, tonumber(P.sy) or 6.255 -- neutral grid (README "만든 방법")
local OX, OY = tonumber(P.ox) or -SX, tonumber(P.oy) or -SY
local pc = app.pixelColor

local base = app.open(P.neutral)
local nimg = base.cels[1].image
local pal = base.palettes[1]
local W, H = base.width, base.height

local function lin(c) c = c / 255; return c <= 0.04045 and c / 12.92 or ((c + 0.055) / 1.055) ^ 2.4 end
local function f(t) return t > 0.008856 and t ^ (1 / 3) or 7.787 * t + 16 / 116 end
local function lab(r, g, b)
  r, g, b = lin(r), lin(g), lin(b)
  local x = (r * .4124 + g * .3576 + b * .1805) / .95047
  local y = r * .2126 + g * .7152 + b * .0722
  local z = (r * .0193 + g * .1192 + b * .9505) / 1.08883
  local fx, fy, fz = f(x), f(y), f(z)
  return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)
end
local plab = {}
-- Only the original 32 colours: the z blues are not character colours.
for i = 1, 32 do local c = pal:getColor(i); plab[i] = { lab(c.red, c.green, c.blue) } end
local cache = {}
local function nearest(p)
  if pc.rgbaA(p) < 128 then return 0 end
  local r, g, b = pc.rgbaR(p), pc.rgbaG(p), pc.rgbaB(p)
  local key = (r << 16) | (g << 8) | b
  if cache[key] then return cache[key] end
  local L, A, B = lab(r, g, b)
  local bi, bd = 1, math.huge
  for i, q in ipairs(plab) do
    local d = (L - q[1]) ^ 2 + (A - q[2]) ^ 2 + (B - q[3]) ^ 2
    if d < bd then bi, bd = i, d end
  end
  cache[key] = bi
  return bi
end

local src = app.open(P["in"])
local img = src.cels[1].image
if src.colorMode ~= ColorMode.RGB then fail("expected an RGB image") end
local SW, SH = src.width, src.height
if P.bg == "corner" then
  img = img:clone()
  local k = img:getPixel(0, 0)
  for it in img:pixels() do
    local p = it()
    if math.abs(pc.rgbaR(p) - pc.rgbaR(k)) + math.abs(pc.rgbaG(p) - pc.rgbaG(k)) + math.abs(pc.rgbaB(p) - pc.rgbaB(k)) <= 30 then it(0) end
  end
end

-- Quick downsample (cell centre) for the offset search; medoid for the result.
local function sample(ox, oy, medoid)
  local x0, y0 = ox, oy
  local cols, rows = math.ceil((SW - x0) / SX), math.ceil((SH - y0) / SY)
  local small = {}
  for j = 0, rows - 1 do for i = 0, cols - 1 do
    local left, top = x0 + i * SX, y0 + j * SY
    local v = 0
    if medoid then
      local samples, opaque = {}, 0
      for y = math.max(0, math.floor(top + SY * .25 + .5)), math.min(SH - 1, math.floor(top + SY * .75 - .5)) do
        for x = math.max(0, math.floor(left + SX * .25 + .5)), math.min(SW - 1, math.floor(left + SX * .75 - .5)) do
          local p = img:getPixel(x, y); samples[#samples + 1] = p
          if pc.rgbaA(p) >= 128 then opaque = opaque + 1 end
        end
      end
      if #samples > 0 and opaque * 2 > #samples then
        local best, bestd = nil, math.huge
        for _, a in ipairs(samples) do
          if pc.rgbaA(a) >= 128 then
            local d = 0
            for _, b in ipairs(samples) do
              d = d + math.abs(pc.rgbaR(a) - pc.rgbaR(b)) + math.abs(pc.rgbaG(a) - pc.rgbaG(b)) + math.abs(pc.rgbaB(a) - pc.rgbaB(b))
            end
            if d < bestd then best, bestd = a, d end
          end
        end
        v = nearest(best)
      end
    else
      local x, y = math.floor(left + SX / 2), math.floor(top + SY / 2)
      if x >= 0 and y >= 0 and x < SW and y < SH then v = nearest(img:getPixel(x, y)) end
    end
    small[j * cols + i] = v
  end end
  return function(x, y) if x < 0 or y < 0 or x >= cols or y >= rows then return 0 end return small[y * cols + x] end
end
local function score(at)
  local bdx, bdy, best = 0, 0, -1
  for dy = -3, 3 do for dx = -3, 3 do
    local same = 0
    for y = 0, H - 1, 2 do for x = 0, W - 1, 2 do
      local v = nimg:getPixel(x, y)
      if v ~= 0 and at(x + dx, y + dy) == v then same = same + 1 end
    end end
    if same > best then bdx, bdy, best = dx, dy, same end
  end end
  return best, bdx, bdy
end
local bo = { score = -1 }
for ox = OX, OX + SX - 0.01, SX / 12 do for oy = OY, OY + SY - 0.01, SY / 12 do
  local s, dx, dy = score(sample(ox, oy, false))
  if s > bo.score then bo = { score = s, ox = ox, oy = oy, dx = dx, dy = dy } end
end end
local at = sample(bo.ox, bo.oy, true)
local s, dx, dy = score(at)

local out = Image(W, H, ColorMode.INDEXED)
local diff = Image(W, H, ColorMode.RGB)
local changed = 0
for y = 0, H - 1 do for x = 0, W - 1 do
  local v, n = at(x + dx, y + dy), nimg:getPixel(x, y)
  out:drawPixel(x, y, v)
  local c = v ~= 0 and pal:getColor(v) or Color(255, 255, 255)
  if v == n then
    c = Color(math.floor(c.red * .25 + 191), math.floor(c.green * .25 + 191), math.floor(c.blue * .25 + 191))
  else changed = changed + 1; if v == 0 then c = Color(255, 0, 0) end end
  diff:drawPixel(x, y, c.rgbaPixel)
end end
local o = Sprite(W, H, ColorMode.INDEXED); o:setPalette(pal); o.transparentColor = 0; o.cels[1].image = out; o:saveAs(P.out)
local d = Sprite(W, H, ColorMode.RGB); d.cels[1].image = diff; d:resize(W * 3, H * 3); d:saveAs(P.diff)
local log = io.open(P.log, "w")
log:write(string.format("grid offset %.2f,%.2f shift %d,%d match %d (of opaque/4) changed %d\n", bo.ox, bo.oy, dx, dy, s, changed))
log:close()
