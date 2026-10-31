-- One-time migration: build the animation sources body.aseprite and faces.aseprite
-- from the PR #141 outputs. After this the two .aseprite files are the originals.
local P = app.params
local FX, FY, FW, FH = 101, 47, 33, 26
local function inHand(x, y) return x >= 129 and y >= 66 end

-- body.aseprite: the idle test plus a hidden @face marker that follows the upper body.
-- only=body rebuilds just this file, keeping faces.aseprite and its hand edits.
local body = app.open(P.idle)
local upperBody
for _, l in ipairs(body.layers) do
  if l.name == "upper (breathing)" then for _, c in ipairs(l.layers) do if c.name == "body" then upperBody = c end end end
end
local marker = body:newLayer(); marker.name = "@face"; marker.isVisible = false
for i = 1, #body.frames do
  local img = Image(1, 1, ColorMode.INDEXED); img:drawPixel(0, 0, 1)
  body:newCel(marker, i, img, Point(FX, FY + upperBody:cel(i).position.y - upperBody:cel(1).position.y))
end
body:saveAs(P.body)
if P.only == "body" then return end

-- faces.aseprite: one frame per mood on the full canvas, a hidden @body reference
-- and eye/mouth variants that hold only the pixels that differ from the face.
local neutral = app.open(P.neutral)
local nimg = neutral.cels[1].image
local W, H = neutral.width, neutral.height
local moods = { "neutral", "happy", "excited", "sad", "surprised", "thinking", "embarrassed", "worried" }
local faces = Sprite(W, H, ColorMode.INDEXED)
faces:setPalette(neutral.palettes[1]); faces.transparentColor = 0
for _ = 2, #moods do faces:newEmptyFrame() end
local ref = faces.layers[1]; ref.name = "@body"; ref.isVisible = false
local face = faces:newLayer(); face.name = "face"
local function crop(img)
  local out = Image(W, H, ColorMode.INDEXED)
  for y = FY, FY + FH - 1 do for x = FX, FX + FW - 1 do
    if not inHand(x, y) then out:drawPixel(x, y, img:getPixel(x, y)) end
  end end
  return out
end
for i, m in ipairs(moods) do
  faces:newCel(ref, i, nimg:clone(), Point(0, 0))
  local src = m == "neutral" and neutral or app.open(string.format(P.moods, m))
  faces:newCel(face, i, crop(src.cels[1].image), Point(0, 0))
  local tag = faces:newTag(i, i); tag.name = m
end
local function variant(group, name, file)
  local layer = faces:newLayer(); layer.name = name; layer.parent = group
  local v = app.open(file).cels[1].image
  local out = Image(W, H, ColorMode.INDEXED)
  for y = FY, FY + FH - 1 do for x = FX, FX + FW - 1 do
    local p = v:getPixel(x, y)
    if p ~= nimg:getPixel(x, y) then out:drawPixel(x, y, p) end
  end end
  faces:newCel(layer, 1, out, Point(0, 0))
end
local eyes = faces:newGroup(); eyes.name = "eyes"
variant(eyes, "half", P.frames .. "/blink-half.png")
variant(eyes, "closed", P.frames .. "/blink-closed.png")
local mouth = faces:newGroup(); mouth.name = "mouth"
variant(mouth, "small", P.frames .. "/talk-small.png")
variant(mouth, "wide", P.frames .. "/talk-wide.png")
faces:saveAs(P.faces)
