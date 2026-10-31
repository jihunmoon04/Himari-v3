-- Downsample an upscaled pseudo-pixel sprite to its grid: each cell takes the
-- medoid colour of its inner pixels, so anti-aliased cell borders are ignored.
local P = app.params
local sx, ox, sy, oy = tonumber(P.sx), tonumber(P.ox), tonumber(P.sy), tonumber(P.oy)
local src = app.open(P["in"])
local img = src.cels[1].image
local W, H = src.width, src.height
local pc = app.pixelColor
ox = ox % sx; oy = oy % sy
local x0, y0 = ox - sx, oy - sy
local cols = math.ceil((W - x0) / sx)
local rows = math.ceil((H - y0) / sy)
local out = Image(cols, rows, ColorMode.RGB)
local function dist(a, b)
  return math.abs(pc.rgbaR(a) - pc.rgbaR(b)) + math.abs(pc.rgbaG(a) - pc.rgbaG(b))
       + math.abs(pc.rgbaB(a) - pc.rgbaB(b)) + math.abs(pc.rgbaA(a) - pc.rgbaA(b)) * 2
end
for j = 0, rows - 1 do
  for i = 0, cols - 1 do
    local left, top = x0 + i * sx, y0 + j * sy
    local xa, xb = math.floor(left + sx * .25 + .5), math.floor(left + sx * .75 - .5)
    local ya, yb = math.floor(top + sy * .25 + .5), math.floor(top + sy * .75 - .5)
    local samples, opaque = {}, 0
    for y = math.max(0, ya), math.min(H - 1, yb) do
      for x = math.max(0, xa), math.min(W - 1, xb) do
        local p = img:getPixel(x, y)
        samples[#samples + 1] = p
        if pc.rgbaA(p) >= 128 then opaque = opaque + 1 end
      end
    end
    local color = 0
    if #samples > 0 and opaque * 2 > #samples then
      local best, bestd = nil, math.huge
      for _, a in ipairs(samples) do
        if pc.rgbaA(a) >= 128 then
          local d = 0
          for _, b in ipairs(samples) do d = d + dist(a, b) end
          if d < bestd then best, bestd = a, d end
        end
      end
      color = pc.rgba(pc.rgbaR(best), pc.rgbaG(best), pc.rgbaB(best), 255)
    end
    out:drawPixel(i, j, color)
  end
end
local spr = Sprite(cols, rows, ColorMode.RGB)
spr.cels[1].image = out
spr:saveAs(P.out)
-- Nearest-neighbour preview at roughly the original size.
local scale = tonumber(P.scale or "6")
spr:resize(cols * scale, rows * scale)
spr:saveAs(P.preview)
