-- Draw blink and talk frames into the eyes and mouth layers of neutral-parts.aseprite.
-- Indices refer to himari-32.gpl: 31 skin, 1/2 lash, 3 soft lash, 16/26 lid shadow,
-- 22/28 lip, 4/7 mouth interior, 11/17 lip shade, 21/25 tongue.
local P = app.params
local spr = app.open(P["in"])
local SKIN, LASH, LASH2, SOFT, SHADOW = 31, 1, 2, 3, 16

local function layer(name)
  for _, l in ipairs(spr.layers) do
    if l.name == name then return l end
    if l.isGroup then for _, c in ipairs(l.layers) do if c.name == name then return c end end end
  end
end
local eyes, mouth = layer("eyes"), layer("mouth")
local HAIR = { [14] = true, [20] = true, [24] = true, [26] = true, [27] = true, [29] = true, [30] = true }

-- Cover the eye with skin between two rows, keeping hair strands that cross it.
local function lid(img, x0, x1, y0, y1, keepHairFrom)
  for y = y0, y1 do
    for x = x0, x1 do
      local v = img:getPixel(x, y)
      if v ~= 0 and not (HAIR[v] and x >= keepHairFrom) then img:drawPixel(x, y, SKIN) end
    end
  end
end
local function put(img, pts) for _, p in ipairs(pts) do img:drawPixel(p[1], p[2], p[3]) end end

local function half(img)
  -- Left eye: the upper lid drops two rows to y56, iris below stays.
  lid(img, 104, 112, 54, 55, 111)
  put(img, { { 104, 55, SOFT }, { 105, 56, LASH }, { 106, 56, LASH }, { 107, 56, LASH }, { 108, 56, LASH }, { 109, 56, LASH },
             { 110, 56, LASH }, { 111, 56, LASH2 }, { 112, 56, LASH2 }, { 113, 57, LASH2 } })
  -- Right eye: the upper lid drops to y61.
  lid(img, 121, 127, 60, 60, 128)
  put(img, { { 120, 61, SOFT }, { 121, 61, LASH }, { 122, 61, LASH }, { 123, 61, LASH }, { 124, 61, LASH }, { 125, 61, LASH },
             { 126, 61, LASH }, { 127, 61, LASH2 }, { 128, 62, LASH } })
end

local function closed(img)
  -- A soft downward arc on the lower lid line, with a shadow row above it.
  lid(img, 105, 112, 54, 59, 111)
  put(img, { { 104, 54, SKIN }, { 104, 56, SOFT },
             { 105, 57, LASH2 }, { 106, 58, LASH }, { 107, 58, LASH }, { 108, 58, LASH }, { 109, 58, LASH }, { 110, 58, LASH },
             { 111, 57, LASH }, { 112, 57, LASH2 }, { 113, 56, LASH2 },
             { 106, 57, SHADOW }, { 107, 57, SHADOW }, { 108, 57, SHADOW }, { 109, 57, SHADOW }, { 110, 57, SHADOW } })
  lid(img, 120, 127, 60, 64, 127)
  put(img, { { 120, 61, SOFT },
             { 121, 62, LASH2 }, { 122, 63, LASH }, { 123, 63, LASH }, { 124, 63, LASH }, { 125, 63, LASH },
             { 126, 62, LASH }, { 127, 62, LASH2 }, { 128, 62, LASH },
             { 122, 62, SHADOW }, { 123, 62, SHADOW }, { 124, 62, SHADOW }, { 125, 62, SHADOW } })
end

local function mouthSmall(img)
  lid(img, 110, 118, 64, 68, 999)
  put(img, { { 112, 65, 22 }, { 113, 65, 17 }, { 114, 65, 17 }, { 115, 66, 22 },
             { 113, 66, 7 }, { 114, 66, 11 }, { 116, 67, 22 } })
end
local function mouthWide(img)
  lid(img, 110, 118, 64, 68, 999)
  put(img, { { 112, 65, 22 }, { 113, 65, 17 }, { 114, 65, 17 }, { 115, 65, 22 },
             { 112, 66, 11 }, { 113, 66, 4 }, { 114, 66, 7 }, { 115, 66, 11 }, { 116, 66, 22 },
             { 113, 67, 11 }, { 114, 67, 21 }, { 115, 67, 22 } })
end

-- Frames 1-4 blink (open, half, closed, half), 5-7 talk (small, wide, small); frame 1 is the rest pose.
local plan = {
  { name = "open" }, { eyes = half }, { eyes = closed }, { eyes = half },
  { mouth = mouthSmall }, { mouth = mouthWide }, { mouth = mouthSmall },
}
local DURATION = { 2600, 60, 90, 60, 120, 140, 120 }
for i = 2, #plan do spr:newFrame(1) end
for i, f in ipairs(plan) do
  spr.frames[i].duration = DURATION[i] / 1000
  for _, pair in ipairs({ { eyes, f.eyes }, { mouth, f.mouth } }) do
    if pair[2] then
      local cel = pair[1]:cel(i)
      local img = cel.image:clone()
      pair[2](img)
      cel.image = img
    end
  end
end
local blink = spr:newTag(1, 4); blink.name = "blink"
local talk = spr:newTag(5, 7); talk.name = "talk"; talk.aniDir = AniDir.PING_PONG
spr:saveAs(P.out)

-- Per-frame PNGs for the app, plus a 6x close-up strip of the face for review.
for i = 1, #plan do
  local img = Image(spr.spec); img:drawSprite(spr, i)
  local s = Sprite(spr.spec); s:setPalette(spr.palettes[1]); s.cels[1].image = img
  s:saveAs(string.format(P.frames, i)); s:close()
end
