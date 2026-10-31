local outf = io.open(app.params["out"], "w"); function out(s) outf:write(s, "\n"); outf:flush() end
-- Estimate the pseudo-pixel grid of an upscaled sprite from edge periodicity.
local path = app.params["in"]
local spr = app.open(path)
local cel = spr.cels[1]
local img = cel.image
local W, H = spr.width, spr.height
local pc = app.pixelColor
local function px(x, y)
  local ix, iy = x - cel.position.x, y - cel.position.y
  if ix < 0 or iy < 0 or ix >= img.width or iy >= img.height then return 0 end
  return img:getPixel(ix, iy)
end
local function diff(a, b)
  return math.abs(pc.rgbaR(a) - pc.rgbaR(b)) + math.abs(pc.rgbaG(a) - pc.rgbaG(b))
       + math.abs(pc.rgbaB(a) - pc.rgbaB(b)) + math.abs(pc.rgbaA(a) - pc.rgbaA(b))
end
local colp, rowp = {}, {}
for x = 0, W - 2 do colp[x] = 0 end
for y = 0, H - 2 do rowp[y] = 0 end
for y = 0, H - 1 do
  for x = 0, W - 1 do
    local p = px(x, y)
    if x < W - 1 then colp[x] = colp[x] + diff(p, px(x + 1, y)) end
    if y < H - 1 then rowp[y] = rowp[y] + diff(p, px(x, y + 1)) end
  end
end
local function best(profile, n)
  local mean = 0
  for i = 0, n - 1 do mean = mean + profile[i] end
  mean = mean / n
  local results = {}
  for s100 = 500, 1400 do
    local s = s100 / 100
    local bs, bo = -1, 0
    for o4 = 0, math.floor(s * 4) do
      local o = o4 / 4
      local sum, k, cnt = 0, 0, 0
      while true do
        local i = math.floor(o + k * s + 0.5) - 1
        if i >= n then break end
        if i >= 0 then sum = sum + profile[i]; cnt = cnt + 1 end
        k = k + 1
      end
      local score = sum / math.max(1, cnt) / mean
      if score > bs then bs, bo = score, o end
    end
    results[#results + 1] = { s = s, score = bs, o = bo }
  end
  table.sort(results, function(a, b) return a.score > b.score end)
  for i = 1, 8 do out(string.format("  s=%.2f offset=%.2f score=%.3f", results[i].s, results[i].o, results[i].score)) end
end
out(string.format("size %dx%d cel %d,%d %dx%d", W, H, cel.position.x, cel.position.y, img.width, img.height))
out("columns:"); best(colp, W - 1)
out("rows:"); best(rowp, H - 1)



