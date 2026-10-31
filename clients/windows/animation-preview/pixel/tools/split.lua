-- Split the cleaned neutral sprite into animation parts.
-- Regions are drawn for the 205x206 neutral pose; colours decide the halo and hair tail.
local P = app.params
local src = app.open(P["in"])
local img = src.cels[1].image
local W, H = src.width, src.height

local function inside(poly, x, y)
  local c, j = false, #poly
  for i = 1, #poly do
    local xi, yi, xj, yj = poly[i][1], poly[i][2], poly[j][1], poly[j][2]
    if (yi > y) ~= (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi then c = not c end
    j = i
  end
  return c
end
local function rect(x0, y0, x1, y1) return { { x0, y0 }, { x1 + 1, y0 }, { x1 + 1, y1 + 1 }, { x0, y1 + 1 } } end
local function set(t) local s = {}; for _, v in ipairs(t) do s[v] = true end; return s end

-- Palette indices of himari-32.gpl.
local PINK = set { 15, 17, 19, 21, 22, 23, 25, 26, 28 }
local CREAM = set { 31, 32 }
local HAIR = set { 10, 14, 20, 24, 27, 29, 30, 18 }

local halo = { { 86, 0 }, { 150, 0 }, { 150, 42 }, { 136, 42 }, { 128, 30 }, { 112, 26 }, { 96, 34 }, { 86, 34 } }
local eyes = { rect(104, 54, 113, 59), rect(119, 60, 128, 64) }
local mouth = rect(110, 64, 118, 68)
local tail = { { 78, 62 }, { 97, 72 }, { 92, 84 }, { 76, 88 }, { 70, 100 }, { 68, 118 }, { 64, 140 }, { 34, 140 }, { 34, 96 }, { 50, 74 } }
local body = { { 84, 18 }, { 152, 18 }, { 152, 74 }, { 154, 82 }, { 154, 108 }, { 140, 110 }, { 128, 112 }, { 118, 117 }, { 100, 119 }, { 88, 118 },
               { 72, 112 }, { 70, 100 }, { 76, 96 }, { 86, 86 }, { 84, 70 }, { 80, 60 }, { 80, 40 } }
local backrest = rect(68, 82, 88, 95)

local NAMES = { "base", "hair tail", "underlay", "body", "eyes", "mouth", "halo" }
local layerOf = {}
local function classify(x, y, v)
  if v == 0 then return nil end
  if inside(halo, x, y) and (PINK[v] or CREAM[v]) then return "halo" end
  for _, e in ipairs(eyes) do if inside(e, x, y) then return "eyes" end end
  if inside(mouth, x, y) then return "mouth" end
  if inside(tail, x, y) and HAIR[v] and not inside(backrest, x, y) then return "hair tail" end
  if inside(body, x, y) and not inside(backrest, x, y) then return "body" end
  return "base"
end
for y = 0, H - 1 do
  for x = 0, W - 1 do layerOf[y * W + x] = classify(x, y, img:getPixel(x, y)) end
end
local function part(x, y) if x < 0 or y < 0 or x >= W or y >= H then return nil end; return layerOf[y * W + x] end
-- Direction each moving part travels in the idle motion: the upper body lifts and the hair
-- tail follows it up a moment later.
local MOVES = { body = -1, eyes = -1, mouth = -1, ["hair tail"] = -1 }
local GROUP = { body = "upper", eyes = "upper", mouth = "upper", ["hair tail"] = "tail" }

-- A moving part reveals what lies behind its trailing seam; repeat its two seam rows
-- underneath so a 1-2 px move never opens a transparent gap against another part.
local images = {}
for _, n in ipairs(NAMES) do images[n] = Image(W, H, ColorMode.INDEXED) end
for y = 0, H - 1 do
  for x = 0, W - 1 do
    local n = part(x, y)
    if n then images[n]:drawPixel(x, y, img:getPixel(x, y)) end
    if n and MOVES[n] then
      for d = 1, 2 do
        local behind = part(x, y - MOVES[n] * d)
        if behind and GROUP[behind] ~= GROUP[n] then images["underlay"]:drawPixel(x, y, img:getPixel(x, y)); break end
      end
    end
  end
end

local out = Sprite(W, H, ColorMode.INDEXED)
out:setPalette(src.palettes[1])
out.transparentColor = 0
local first = out.layers[1]
local upper
for i, n in ipairs(NAMES) do
  local layer
  if n == "body" then upper = out:newGroup(); upper.name = "upper (breathing)" end
  if i == 1 then layer = first else layer = out:newLayer() end
  layer.name = n
  if n == "body" or n == "eyes" or n == "mouth" then layer.parent = upper end
  out:newCel(layer, 1, images[n], Point(0, 0))
end
out:saveAs(P.out)

-- Debug view: each part in its own tint.
local tint = { base = { 90, 90, 110 }, ["hair tail"] = { 80, 170, 255 }, underlay = { 255, 200, 0 }, body = { 120, 220, 120 },
               eyes = { 255, 60, 60 }, mouth = { 255, 0, 255 }, halo = { 255, 150, 0 } }
local dbg = Image(W, H, ColorMode.RGB)
local pal = src.palettes[1]
for y = 0, H - 1 do
  for x = 0, W - 1 do
    local n = part(x, y)
    if n then
      local c, t = pal:getColor(img:getPixel(x, y)), tint[n]
      dbg:drawPixel(x, y, app.pixelColor.rgba((c.red + t[1] * 2) // 3, (c.green + t[2] * 2) // 3, (c.blue + t[3] * 2) // 3, 255))
    end
  end
end
local ds = Sprite(W, H, ColorMode.RGB)
ds.cels[1].image = dbg
ds:resize(W * 5, H * 5)
ds:saveAs(P.debug)
