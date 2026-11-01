-- Clear the fingers under the chin from every mood face in faces.aseprite (owner request
-- 2026-11-01). The rest pose's left hand rests under the chin, so the faces cut from it also
-- held the fingers right of the jaw line; in a frame whose hand is elsewhere (stretch) those
-- fingers floated on the chin. The body frames already hold the same hand there (42 of 43
-- pixels match the neutral face, the 43rd is transparent in it), so the rest pose is unchanged
-- for neutral and differs by a few pixels for the other moods. Safe to run again.
--   aseprite -b --script-param faces=clean/faces.aseprite --script-param log=face-hand.txt --script tools/face-hand.lua
local P = app.params
-- Rest-pose rows and columns of the hand: below the finger top edge (row 68) and right of the
-- jaw line ((123, 68), (121, 69), (118, 70), (117, 71)), up to the old hand box (x >= 129).
local HAND = { [68] = { 124, 128 }, [69] = { 122, 128 }, [70] = { 120, 128 }, [71] = { 118, 128 }, [72] = { 118, 128 } }

local spr = app.open(P.faces)
local face
for _, l in ipairs(spr.layers) do if l.name == "face" then face = l end end
if not face then error("face-hand: faces.aseprite has no face layer", 0) end
local lines = {}
for _, tag in ipairs(spr.tags) do
  local cel = face:cel(tag.fromFrame)
  local cleared = 0
  if cel then
    local img, pos = cel.image:clone(), cel.position
    for y, span in pairs(HAND) do
      for x = span[1], span[2] do
        local ix, iy = x - pos.x, y - pos.y
        if ix >= 0 and iy >= 0 and ix < img.width and iy < img.height and img:getPixel(ix, iy) ~= 0 then
          img:drawPixel(ix, iy, 0); cleared = cleared + 1
        end
      end
    end
    cel.image = img
  end
  lines[#lines + 1] = string.format("%s: %d pixels cleared", tag.name, cleared)
end
spr:saveAs(P.faces)
if P.log then local f = io.open(P.log, "w"); f:write(table.concat(lines, "\n"), "\n"); f:close() end
