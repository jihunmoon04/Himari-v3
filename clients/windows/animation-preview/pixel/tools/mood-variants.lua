-- Draw eye (half, closed) and mouth (small, wide) variants for the mood faces in
-- faces.aseprite, following the neutral ones the owner approved (neutral-variants.lua).
--   Eyes are derived from each mood's own lash line, column by column:
--   * half: the lid drops about half the eye height, keeping the lash slant
--   * closed: a lash arc on the lowest row of the eye, its two ends one row higher
--   * columns at the outer (right) end that are only lash are dropped (no long tail)
--   Mouths are small templates placed on each mood's mouth.
-- Each variant layer keeps only pixels that differ from the mood face.
-- Usage: aseprite -b --script-param faces=clean/faces.aseprite [--script-param only=sad] --script tools/mood-variants.lua
local P = app.params
local spr = app.open(P.faces)
local SKIN = 31

local LASH = {}
for _, i in ipairs({ 1, 2, 3, 4, 5, 6, 10, 12 }) do LASH[i] = true end
local EYE = {}
for i in pairs(LASH) do EYE[i] = true end
for _, i in ipairs({ 7, 8, 9, 11, 13, 15, 16, 17, 18, 19, 21, 23, 25, 32 }) do EYE[i] = true end

local LEFT_EYE = { x0 = 103, x1 = 114, y0 = 52, y1 = 59 }
local RIGHT_EYE = { x0 = 119, x1 = 129, y0 = 57, y1 = 64 }

-- Mouth templates: rows of palette indices, nil keeps the face pixel.
local SMALL = { { 22, 17, 22 }, { 17, 7, 17 } }
local WIDE = { { 22, 17, 22 }, { 11, 4, 11 }, { 17, 21, 17 }, { nil, 22, nil } }

-- clear: rectangles { x0, y0, x1, y1 } of the mood's mouth painted over with skin first.
-- small/wide: { template, x, y } or explicit edit lists { x, y, index }.
local MOODS = {
  happy = { eyes = false,
    -- The smile is already open: talking shades its inside, then opens it a row deeper.
    small = { { 113, 66, 17 }, { 114, 66, 7 } },
    wide = { { 113, 66, 7 }, { 114, 66, 4 }, { 115, 66, 17 }, { 113, 67, 17 }, { 114, 67, 21 }, { 115, 67, 17 },
             { 113, 68, 22 }, { 114, 68, 22 } } },
  excited = { eyes = true,
    -- The mouth is already wide open: talking closes it to a smaller one, then shows the
    -- inside of the open one (keepWide: no clearing for wide).
    keepWide = true,
    clear = { { 111, 64, 112, 64 }, { 111, 65, 116, 65 }, { 111, 66, 117, 66 }, { 111, 67, 116, 67 }, { 111, 68, 116, 68 }, { 113, 69, 114, 69 } },
    small = { tpl = { { 22, 22, 22, 21 }, { 21, 7, 7, 15 }, { nil, 22, 22, nil } }, x = 112, y = 65 },
    wide = { { 112, 66, 7 }, { 113, 66, 4 }, { 114, 66, 4 }, { 115, 66, 7 }, { 112, 67, 17 }, { 113, 67, 21 }, { 114, 67, 21 }, { 115, 67, 17 } } },
  sad = { eyes = true, clear = { { 114, 67, 115, 67 } }, small = { tpl = SMALL, x = 113, y = 67 }, wide = { tpl = WIDE, x = 113, y = 66 } },
  surprised = { eyes = true,
    small = { { 113, 67, 7 }, { 114, 67, 17 } },
    wide = { { 113, 67, 4 }, { 114, 67, 7 }, { 113, 68, 21 }, { 114, 68, 17 }, { 112, 68, 22 }, { 115, 68, 22 }, { 113, 69, 22 }, { 114, 69, 22 } } },
  thinking = { eyes = true, clear = { { 115, 67, 116, 67 } }, small = { tpl = SMALL, x = 114, y = 67 }, wide = { tpl = WIDE, x = 114, y = 66 } },
  embarrassed = { eyes = true, clear = { { 114, 66, 114, 66 }, { 114, 67, 115, 67 } }, small = { tpl = SMALL, x = 113, y = 66 }, wide = { tpl = WIDE, x = 113, y = 65 } },
  worried = { eyes = true, clear = { { 113, 66, 115, 66 } }, small = { tpl = SMALL, x = 113, y = 66 }, wide = { tpl = WIDE, x = 113, y = 65 } },
  neutral = { eyes = true }, -- only used with only=neutral to compare against the hand-drawn version
}

local function layer(path)
  local function find(list, name) for _, l in ipairs(list) do if l.name == name then return l end end end
  local group, name = path:match("^(%a+)/(%a+)$")
  if group then return find(find(spr.layers, group).layers, name) end
  return find(spr.layers, path)
end

-- Image:drawImage maps index 32 to 0 here, so cels are copied pixel by pixel.
local function paste(dst, cel)
  local img, pos = cel.image, cel.position
  for y = 0, img.height - 1 do for x = 0, img.width - 1 do
    local v = img:getPixel(x, y)
    if v ~= 0 then dst:drawPixel(pos.x + x, pos.y + y, v) end
  end end
end

-- The face as shown: the neutral body under the mood face (the face has a few holes).
local function shown(frame)
  local img = Image(spr.spec)
  paste(img, layer("@body"):cel(frame))
  paste(img, layer("face"):cel(frame))
  return img
end

local function eye(img, box, half, out)
  local cols = {}
  for x = box.x0, box.x1 do
    -- An eye column has a lash pixel; it spans from its first to its last eye-coloured pixel.
    local top, bottom, lash
    for y = box.y0, box.y1 do
      local v = img:getPixel(x, y)
      if EYE[v] then top = top or y; bottom = y end
      if LASH[v] then lash = true end
    end
    if lash then cols[#cols + 1] = { x = x, top = top, bottom = bottom } end
  end
  if #cols == 0 then error("no lash in eye box") end
  -- Columns between lash columns without a lash pixel take their neighbours' top.
  local byX = {}
  for _, c in ipairs(cols) do byX[c.x] = c end
  for x = cols[1].x, cols[#cols].x do
    if not byX[x] then
      local l, r = byX[x - 1], nil
      for k = x + 1, cols[#cols].x do if byX[k] then r = byX[k]; break end end
      local top = math.floor((l.top + r.top) / 2)
      local bottom = top
      for y = top, box.y1 do if EYE[img:getPixel(x, y)] then bottom = y end end
      byX[x] = { x = x, top = top, bottom = bottom }
    end
  end
  -- Iris or outline columns next to the lash ones belong to the eye too.
  local function column(x)
    local top, bottom
    for y = box.y0, box.y1 do if EYE[img:getPixel(x, y)] then top = top or y; bottom = y end end
    if top then return { x = x, top = top, bottom = bottom } end
  end
  local first, last = cols[1].x, cols[#cols].x
  while first > box.x0 and column(first - 1) do first = first - 1; byX[first] = column(first) end
  while last < box.x1 and column(last + 1) do last = last + 1; byX[last] = column(last) end
  local list = {}
  for x = first, last do list[#list + 1] = byX[x] end
  local xa, xb
  for _, c in ipairs(list) do if c.bottom - c.top >= 2 then xa = xa or c.x; xb = c.x end end
  -- Skin for a cleared pixel: the nearest plain (31) or blushed (28) skin in the same row
  -- outside the eye, so a blush band runs on through the closed eye.
  local function skinAt(y)
    local blush = false
    for d = 1, 4 do
      for _, x in ipairs({ first - d, last + d }) do
        local v = img:getPixel(x, y)
        if v == SKIN then return SKIN end
        if v == 28 then blush = true end
      end
    end
    return blush and 28 or SKIN
  end
  local function median(values)
    table.sort(values)
    return values[math.floor((#values + 1) / 2)]
  end
  local heights, bottoms = {}, {}
  for _, c in ipairs(list) do
    if c.x >= xa and c.x <= xb then heights[#heights + 1] = c.bottom - c.top; bottoms[#bottoms + 1] = c.bottom end
  end
  if half then
    -- The lowered lid is one straight line: a least-squares fit through the eye's top edge,
    -- moved down by half the eye height. Everything above it is cleared to skin, and lash
    -- rows left below it (a lash drawn two or more pixels thick) show what lay under them.
    local drop = math.max(1, median(heights) // 2)
    local n, sx, sy, sxx, sxy = 0, 0, 0, 0, 0
    for _, c in ipairs(list) do
      if c.x >= xa and c.x <= xb then n = n + 1; sx = sx + c.x; sy = sy + c.top; sxx = sxx + c.x * c.x; sxy = sxy + c.x * c.top end
    end
    local slope = (n * sxx - sx * sx) ~= 0 and (n * sxy - sx * sy) / (n * sxx - sx * sx) or 0
    local function lid(x) return math.floor((sy - slope * sx) / n + slope * x + drop + .5) end
    -- Like the closed eye, the lid stops one column short of the outer end, and the outer
    -- corner's lash below the lid goes too, so no stub is left there.
    local outer = xb - 1
    for _, c in ipairs(list) do
      if c.x >= xa and c.x <= outer then
        local y = lid(c.x)
        local runEnd = c.top
        while runEnd + 1 <= c.bottom and LASH[img:getPixel(c.x, runEnd + 1)] do runEnd = runEnd + 1 end
        if c.x >= outer - 1 then runEnd = c.bottom end
        for yy = math.min(c.top, y), y - 1 do out[#out + 1] = { c.x, yy, skinAt(yy) } end
        out[#out + 1] = { c.x, y, c.x == outer and 2 or 1 }
        for yy = y + 1, runEnd do
          local v = img:getPixel(c.x, yy)
          if LASH[v] then
            local under = img:getPixel(c.x, runEnd + 1)
            out[#out + 1] = { c.x, yy, (EYE[under] and not LASH[under]) and under or skinAt(yy) }
          end
        end
      else
        for yy = c.top, c.bottom do out[#out + 1] = { c.x, yy, skinAt(yy) } end
      end
    end
    if xa > list[1].x then out[#out + 1] = { xa - 1, lid(xa), 3 } end
  else
    -- An arc on the eye's usual lower edge (not its lowest pixel, which may be the lower lid),
    -- one column shorter at the outer end, ends a row higher.
    local low = median(bottoms)
    for _, c in ipairs(list) do
      local last = (c.x >= xa and c.x <= xb) and math.max(c.bottom, low) or c.bottom
            for y = c.top, last do out[#out + 1] = { c.x, y, skinAt(y) } end
    end
    local last = xb - 1
    for x = xa, last do
      local endCol = x == xa or x == last
      out[#out + 1] = { x, endCol and low - 1 or low, endCol and 2 or 1 }
    end
    if xa > list[1].x then out[#out + 1] = { xa - 1, low - 2, 3 } end
  end
end

local function mouth(spec, out)
  if spec.tpl then
    for r, row in ipairs(spec.tpl) do
      for c, v in pairs(row) do out[#out + 1] = { spec.x + c - 1, spec.y + r - 1, v } end
    end
  else
    for _, e in ipairs(spec) do out[#out + 1] = e end
  end
end

local function write(path, frame, base, edits)
  local full = base:clone()
  for _, e in ipairs(edits) do full:drawPixel(e[1], e[2], e[3]) end
  local diff = Image(spr.spec)
  for y = 0, spr.height - 1 do for x = 0, spr.width - 1 do
    local v = full:getPixel(x, y)
    if v ~= base:getPixel(x, y) then diff:drawPixel(x, y, v) end
  end end
  spr:newCel(layer(path), frame, diff, Point(0, 0))
end

for _, tag in ipairs(spr.tags) do
  local m = MOODS[tag.name]
  local wanted = P.only and P.only == tag.name or (not P.only and tag.name ~= "neutral")
  if m and wanted then
    local f = tag.fromFrame.frameNumber
    local base = shown(f)
    if m.eyes then
      for _, half in ipairs({ true, false }) do
        local edits = {}
        eye(base, LEFT_EYE, half, edits)
        eye(base, RIGHT_EYE, half, edits)
        write(half and "eyes/half" or "eyes/closed", f, base, edits)
      end
    end
    if m.small then
      for _, name in ipairs({ "small", "wide" }) do
        local edits = {}
        for _, r in ipairs((name == "wide" and m.keepWide) and {} or m.clear or {}) do
          for y = r[2], r[4] do for x = r[1], r[3] do edits[#edits + 1] = { x, y, SKIN } end end
        end
        mouth(m[name], edits)
        write("mouth/" .. name, f, base, edits)
      end
    end
  end
end
spr:saveAs(P.out or P.faces)
