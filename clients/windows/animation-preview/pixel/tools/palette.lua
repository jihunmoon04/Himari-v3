-- Palette reduction by weighted k-means in CIELAB. Farthest-point seeding keeps
-- small but distinct areas (skin, eyes, ribbons) from merging into large ones (hair).
local P = app.params
local K = tonumber(P.colors)
local outf = io.open(P.log, "w")
local spr = app.open(P["in"])
local img = spr.cels[1].image
local pc = app.pixelColor

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

local colors, index = {}, {}
for it in img:pixels() do
  local v = it()
  if pc.rgbaA(v) > 0 then
    local key = v & 0xffffff
    local c = index[key]
    if not c then
      local r, g, b = pc.rgbaR(v), pc.rgbaG(v), pc.rgbaB(v)
      local L, A, B = lab(r, g, b)
      c = { r = r, g = g, b = b, L = L, A = A, B = B, n = 0 }
      index[key] = c; colors[#colors + 1] = c
    end
    c.n = c.n + 1
  end
end
local function d2(c, m) local a, b, e = c.L - m.L, c.A - m.A, c.B - m.B; return a * a + b * b + e * e end

-- Seed with the most common colour, then repeatedly the colour farthest from all seeds,
-- weighted lightly by frequency so single noisy pixels do not become palette entries.
table.sort(colors, function(a, b) return a.n > b.n end)
local centers = { { L = colors[1].L, A = colors[1].A, B = colors[1].B } }
local near = {}
for i, c in ipairs(colors) do near[i] = d2(c, centers[1]) end
while #centers < K do
  local best, bi = -1, 1
  for i, c in ipairs(colors) do
    local s = near[i] * math.min(c.n, 40) ^ 0.5
    if s > best then best, bi = s, i end
  end
  local m = { L = colors[bi].L, A = colors[bi].A, B = colors[bi].B }
  centers[#centers + 1] = m
  for i, c in ipairs(colors) do near[i] = math.min(near[i], d2(c, m)) end
end

local assign = {}
for iter = 1, 30 do
  local sums = {}
  for k = 1, K do sums[k] = { L = 0, A = 0, B = 0, r = 0, g = 0, b = 0, n = 0 } end
  local changed = 0
  for i, c in ipairs(colors) do
    local bk, bd = 1, math.huge
    for k, m in ipairs(centers) do local d = d2(c, m); if d < bd then bk, bd = k, d end end
    if assign[i] ~= bk then changed = changed + 1 end
    assign[i] = bk
    local s = sums[bk]
    s.L = s.L + c.L * c.n; s.A = s.A + c.A * c.n; s.B = s.B + c.B * c.n
    s.r = s.r + c.r * c.n; s.g = s.g + c.g * c.n; s.b = s.b + c.b * c.n; s.n = s.n + c.n
  end
  for k = 1, K do
    local s = sums[k]
    if s.n > 0 then centers[k] = { L = s.L / s.n, A = s.A / s.n, B = s.B / s.n, r = s.r / s.n, g = s.g / s.n, b = s.b / s.n, n = s.n } end
  end
  if changed == 0 then outf:write("converged after ", iter, " iterations\n"); break end
end

-- Palette: index 0 transparent, then centres from dark to light.
local order = {}
for k = 1, K do if centers[k].n then order[#order + 1] = k end end
table.sort(order, function(a, b) return centers[a].L < centers[b].L end)
local slot = {}
local pal = Palette(#order + 1)
pal:setColor(0, Color { r = 0, g = 0, b = 0, a = 0 })
for i, k in ipairs(order) do
  local m = centers[k]
  slot[k] = i
  pal:setColor(i, Color { r = math.floor(m.r + .5), g = math.floor(m.g + .5), b = math.floor(m.b + .5), a = 255 })
  outf:write(string.format("%2d #%02x%02x%02x  %6d px\n", i, math.floor(m.r + .5), math.floor(m.g + .5), math.floor(m.b + .5), m.n))
end
local lookup = {}
for i, c in ipairs(colors) do lookup[(c.b << 16) | (c.g << 8) | c.r] = slot[assign[i]] end

local out = Sprite(spr.width, spr.height, ColorMode.INDEXED)
out:setPalette(pal)
out.transparentColor = 0
local q = Image(spr.width, spr.height, ColorMode.INDEXED)
for y = 0, spr.height - 1 do
  for x = 0, spr.width - 1 do
    local v = img:getPixel(x, y)
    q:drawPixel(x, y, pc.rgbaA(v) > 0 and lookup[(pc.rgbaB(v) << 16) | (pc.rgbaG(v) << 8) | pc.rgbaR(v)] or 0)
  end
end
out.cels[1].image = q
out:saveAs(P.out)
out:resize(out.width * 6, out.height * 6)
out:saveAs(P.preview)
outf:close()
