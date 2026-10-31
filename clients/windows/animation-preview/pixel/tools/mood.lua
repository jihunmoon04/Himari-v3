-- Convert one generated mood image to the neutral pixel sprite: downsample on its own grid,
-- map to the shared palette, align to neutral, then keep only the face so that the halo,
-- hair, body and wheelchair stay identical across moods.
local P = app.params
local sx, ox, sy, oy = tonumber(P.sx), tonumber(P.ox), tonumber(P.sy), tonumber(P.oy)
local pc = app.pixelColor

local base = app.open(P.neutral)
local nimg = base.cels[1].image
local pal = base.palettes[1]
local W, H = base.width, base.height

-- Palette in CIELAB for nearest-colour mapping.
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
for i = 1, #pal - 1 do local c = pal:getColor(i); plab[i] = { lab(c.red, c.green, c.blue) } end
local cache = {}
local function nearest(r, g, b)
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

-- Downsample: medoid of each cell's inner pixels (same rule as downsample.lua).
local src = app.open(P["in"])
local img = src.cels[1].image
local SW, SH = src.width, src.height
ox = ox % sx; oy = oy % sy
local x0, y0 = ox - sx, oy - sy
local cols, rows = math.ceil((SW - x0) / sx), math.ceil((SH - y0) / sy)
local function dist(a, b)
  return math.abs(pc.rgbaR(a) - pc.rgbaR(b)) + math.abs(pc.rgbaG(a) - pc.rgbaG(b))
       + math.abs(pc.rgbaB(a) - pc.rgbaB(b)) + math.abs(pc.rgbaA(a) - pc.rgbaA(b)) * 2
end
local small = {}
for j = 0, rows - 1 do
  for i = 0, cols - 1 do
    local left, top = x0 + i * sx, y0 + j * sy
    local samples, opaque = {}, 0
    for y = math.max(0, math.floor(top + sy * .25 + .5)), math.min(SH - 1, math.floor(top + sy * .75 - .5)) do
      for x = math.max(0, math.floor(left + sx * .25 + .5)), math.min(SW - 1, math.floor(left + sx * .75 - .5)) do
        local p = img:getPixel(x, y)
        samples[#samples + 1] = p
        if pc.rgbaA(p) >= 128 then opaque = opaque + 1 end
      end
    end
    local v = 0
    if #samples > 0 and opaque * 2 > #samples then
      local best, bestd = nil, math.huge
      for _, a in ipairs(samples) do
        if pc.rgbaA(a) >= 128 then
          local d = 0
          for _, b in ipairs(samples) do d = d + dist(a, b) end
          if d < bestd then best, bestd = a, d end
        end
      end
      v = nearest(pc.rgbaR(best), pc.rgbaG(best), pc.rgbaB(best))
    end
    small[j * cols + i] = v
  end
end
local function at(x, y) if x < 0 or y < 0 or x >= cols or y >= rows then return 0 end; return small[y * cols + x] end

-- Align to neutral by the shift with the most identical pixels.
local bdx, bdy, best = 0, 0, -1
for dy = -4, 4 do
  for dx = -4, 4 do
    local same = 0
    for y = 0, H - 1, 2 do
      for x = 0, W - 1, 2 do
        local v = nimg:getPixel(x, y)
        if v ~= 0 and at(x + dx, y + dy) == v then same = same + 1 end
      end
    end
    if same > best then bdx, bdy, best = dx, dy, same end
  end
end

-- Face region: brows, eyes, cheeks and mouth, above the hand at the chin.
local function face(x, y) return x >= 101 and x <= 133 and y >= 47 and y <= 72 and not (x >= 129 and y >= 66) end
local out = Image(W, H, ColorMode.INDEXED)
local changed = 0
for y = 0, H - 1 do
  for x = 0, W - 1 do
    local v = nimg:getPixel(x, y)
    if face(x, y) then
      local m = at(x + bdx, y + bdy)
      if m ~= 0 then if m ~= v then changed = changed + 1 end; v = m end
    end
    out:drawPixel(x, y, v)
  end
end
local s = Sprite(W, H, ColorMode.INDEXED)
s:setPalette(pal); s.transparentColor = 0
s.cels[1].image = out
s:saveAs(P.out)
local log = io.open(P.log, "w")
log:write(string.format("grid %dx%d shift %d,%d match %d face pixels changed %d\n", cols, rows, bdx, bdy, best, changed))
log:close()
