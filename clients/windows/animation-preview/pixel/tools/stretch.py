"""Convert the stretch reference sheets to sprite sources for clips.lua (owner request 2026-11-01).

Two generated sheets, kept outside the repository (build/ is ignored):
  himari-stretch-seven-keyposes.png   seven upper-body key poses, arms from the cheek to clasped overhead
  himari-leg-stretch-eight-keyposes.png  eight leg poses, from crossed to stretched out
Each key pose is cut from the sheet, its background flood-filled away, fitted to the rest pose
(the head for the upper body, the wheelchair for the legs), sampled to the 205x206 grid and mapped
to himari-32.gpl, like pose.lua. Writes to clean/poses/stretch/:
  upper-<n>[-lift<k>].png  what the upper-body layer shows (halo swapped for the rest pose's)
  legs-<n>.png             the legs zone (x >= 98, y >= 102) that replaces the base layer's
  stretch.json             fits, face offsets and the frame list clips.lua builds the clip from
Needs Python 3 with numpy and Pillow, and aseprite on PATH (to read body.aseprite's rest layers).
The fits take minutes; they are kept in stretch.json and reused unless --refit is given.
Run from clients/windows/animation-preview/pixel:
  python -I tools/stretch.py --sheets ../../../../build/animation-reference
then the clips.lua and export.lua commands in clean/README.md.
"""
import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile
import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
CLEAN = os.path.join(HERE, "..", "clean")
OUT = os.path.join(CLEAN, "poses", "stretch")
STRETCH_SHEET, LEGS_SHEET = "himari-stretch-seven-keyposes.png", "himari-leg-stretch-eight-keyposes.png"
# Sheet boxes (x0, y0, x1, y1). The top row's figures touch; the cuts are the least-filled columns.
UPPER_CELLS = {2: (343, 40, 660, 590), 3: (660, 40, 976, 590), 4: (976, 40, 1312, 590),
               5: (30, 590, 470, 1170), 6: (470, 590, 878, 1170), 7: (878, 590, 1312, 1170)}
LEG_CELLS = {n: ((n - 1) % 4 * 384, (n - 1) // 4 * 512, (n - 1) % 4 * 384 + 384, (n - 1) // 4 * 512 + 512) for n in range(1, 9)}
FX, FY = 101, 47                 # face origin in the rest pose
HEAD = (78, 18, 150, 82)         # hair and face of the rest pose: what the upper body is fitted on
LEGS = (98, 102)                 # legs zone: x >= 98 and y >= 102 come from the leg pose
TOPCUT, LOWCUT = 96, 118         # the key pose wins above TOPCUT, and down to LOWCUT where nothing is below
WAIST, HANDS = 92, 18            # lift: rows above WAIST rise, hands above HANDS by at most 1 px
TOP = 24                         # work canvas rows above the sprite, to catch anything that overflows
OWN_FACE = {6, 7}                # closed-eye key poses keep their drawn face
HALO_COLOURS = [21, 22, 23, 25, 28, 31, 32]   # what the key poses' halo maps to
HEAD_HAIR = [6, 10, 14, 20, 24]                 # hair mass, to find the crown under the ahoge
HAND_OUTLINE = [4, 7, 9, 11, 13, 15, 16, 17]   # red-brown skin outlines

# The clip, as (upper pose, leg pose, ms, lift). Upper 1 / legs 0 are the rest pose's own.
# Folding back read well, so unfolding is its mirror (owner review 2026-11-01); at the top the
# upper body rises 1 px twice (2-3 px was too much).
FOLD = [(7, 8), (6, 8), (5, 7), (4, 6), (3, 4), (2, 2), (1, 3), (1, 0)]
FOLD_MS = [200, 170, 150, 150, 170, 160, 900]
UNFOLD_MS = [600, 160, 170, 160, 160, 190, 220]
HOLD = [(0, 160), (1, 420), (0, 140), (1, 700), (0, 180)]
SEQ = [(s, l, m, 0) for (s, l), m in zip(FOLD[::-1][:-1], UNFOLD_MS)] \
    + [(7, 8, m, k) for k, m in HOLD] \
    + [(s, l, m, 0) for (s, l), m in zip(FOLD[1:], FOLD_MS)]


def palette():
    pal = []
    for line in open(os.path.join(CLEAN, "himari-32.gpl"), encoding="utf-8"):
        parts = line.split()
        if len(parts) >= 4 and parts[0].isdigit():
            pal.append(tuple(int(p) for p in parts[:3]))
    return np.array(pal[:33], dtype=float)


PAL = palette()
FLAT = [0, 0, 0] + [int(v) for c in PAL[1:] for v in c]
FLAT += [0] * (768 - len(FLAT))


def lab(rgb):
    c = rgb / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    x = (c[..., 0] * .4124 + c[..., 1] * .3576 + c[..., 2] * .1805) / .95047
    y = c[..., 0] * .2126 + c[..., 1] * .7152 + c[..., 2] * .0722
    z = (c[..., 0] * .0193 + c[..., 1] * .1192 + c[..., 2] * .9505) / 1.08883
    f = lambda t: np.where(t > 0.008856, np.cbrt(t), 7.787 * t + 16 / 116)
    fx, fy, fz = f(x), f(y), f(z)
    return np.stack([116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)], -1)


PLAB = lab(PAL[1:])


def cut(sheet, box):
    """A key pose with its background (flood-filled from the box edges) as index 0."""
    im = Image.open(sheet).convert("RGB").crop(box)
    marker, w, h = (255, 0, 255), im.size[0], im.size[1]
    bg = im.getpixel((2, 2))
    for seed in [(x, 0) for x in range(0, w, 5)] + [(x, h - 1) for x in range(0, w, 5)] + \
                [(0, y) for y in range(0, h, 5)] + [(w - 1, y) for y in range(0, h, 5)]:
        p = im.getpixel(seed)
        if p != marker and sum(abs(a - b) for a, b in zip(p, bg)) < 40:
            ImageDraw.floodfill(im, seed, marker, thresh=34)
    rgb = np.asarray(im).astype(float)
    alpha = ~np.all(rgb == marker, -1)
    d = ((lab(rgb)[..., None, :] - PLAB[None, None]) ** 2).sum(-1)
    return np.where(alpha, d.argmin(-1) + 1, 0)


def sample(idx, s, ox, oy, W, H, mode=False):
    """Grid pixel (x, y) <- source around (ox + (x+.5)s, oy + (y+.5)s): the cell centre for
    fitting, the most common colour of the cell's middle half for the result (cf. pose.lua)."""
    ys, xs = oy + (np.arange(H) + .5) * s, ox + (np.arange(W) + .5) * s
    out = np.zeros((H, W), int)
    if not mode:
        yi, xi = np.round(ys).astype(int), np.round(xs).astype(int)
        oky, okx = (yi >= 0) & (yi < idx.shape[0]), (xi >= 0) & (xi < idx.shape[1])
        sub = idx[np.clip(yi, 0, idx.shape[0] - 1)][:, np.clip(xi, 0, idx.shape[1] - 1)]
        out[np.ix_(oky, okx)] = sub[np.ix_(oky, okx)]
        return out
    r = max(1, int(round(s * .25)))
    for j, cy in enumerate(ys):
        for i, cx in enumerate(xs):
            y0, y1, x0, x1 = int(round(cy - r)), int(round(cy + r)), int(round(cx - r)), int(round(cx + r))
            if y1 <= 0 or x1 <= 0 or y0 >= idx.shape[0] or x0 >= idx.shape[1]:
                continue
            v = idx[max(0, y0):y1 + 1, max(0, x0):x1 + 1].ravel()
            if (v > 0).sum() * 2 > v.size:
                out[j, i] = np.bincount(v[v > 0]).argmax()
    return out


def fit(idx, ref, mask, box, scales, span):
    """Scale and offset (source position of the grid's (0, 0)) that match ref inside box best:
    a coarse search over the crop, then a fine one around the best hit."""
    x0, y0, x1, y1 = box
    ref, mask = ref[y0:y1, x0:x1], mask[y0:y1, x0:x1]
    def score(s, bx, by):
        return int(((sample(idx, s, bx, by, x1 - x0, y1 - y0) == ref) & mask).sum())
    best = (-1,)
    for s in scales:
        for by in np.arange(span[1][0], span[1][1], 4):
            for bx in np.arange(span[0][0], span[0][1], 4):
                sc = score(s, bx, by)
                if sc > best[0]:
                    best = (sc, s, bx, by)
    _, s0, bx0, by0 = best
    for s in np.arange(s0 - .04, s0 + .041, .01):
        for by in np.arange(by0 - 4, by0 + 4.01, .5):
            for bx in np.arange(bx0 - 4, bx0 + 4.01, .5):
                sc = score(s, bx, by)
                if sc > best[0]:
                    best = (sc, s, bx, by)
    sc, s, bx, by = best
    return [sc, round(float(s), 3), float(bx - x0 * s), float(by - y0 * s)]


def components(mask):
    seen, out = np.zeros_like(mask, bool), []
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        stack, comp = [(y0, x0)], []
        seen[y0, x0] = True
        while stack:
            y, x = stack.pop()
            comp.append((y, x))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    yy, xx = y + dy, x + dx
                    if 0 <= yy < mask.shape[0] and 0 <= xx < mask.shape[1] and mask[yy, xx] and not seen[yy, xx]:
                        seen[yy, xx] = True
                        stack.append((yy, xx))
        out.append(comp)
    return out


def rest_layers(aseprite):
    """Idle frame 1 of base, hair tail, halo and body, as index arrays with TOP empty rows."""
    out = {}
    aseprite = shutil.which(aseprite) or aseprite  # finds aseprite.cmd on Windows too
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("base", "hair tail", "halo", "body"):
            path = os.path.join(tmp, name + ".png")
            subprocess.run([aseprite, "-b", "--layer", name, "--frame-range", "0,0",
                            os.path.join(CLEAN, "body.aseprite"), "--save-as", path], check=True)
            a = np.asarray(Image.open(path)).astype(int)
            out[name] = np.vstack([np.zeros((TOP, a.shape[1]), int), a])
    return out


def save(a, name):
    if (a[:TOP] > 0).any():
        sys.exit(f"stretch: {name} reaches {int((a[:TOP] > 0).any(1).sum())} rows above the canvas")
    im = Image.fromarray(a[TOP:].astype(np.uint8), "P")
    im.putpalette(FLAT)
    im.save(os.path.join(OUT, name + ".png"), transparency=0)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sheets", required=True, help="folder with the two reference sheets")
    ap.add_argument("--aseprite", default="aseprite")
    ap.add_argument("--refit", action="store_true", help="search the fits again instead of reusing stretch.json")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    meta_path = os.path.join(OUT, "stretch.json")
    old = json.load(open(meta_path)) if os.path.exists(meta_path) and not a.refit else {}
    fits = {"upper": old.get("fits", {}).get("upper", {}), "legs": old.get("fits", {}).get("legs", {})}
    # The sheets are not in the repository; their hashes say whether these are the ones the
    # kept fits were found on.
    sources = {}
    for name in (STRETCH_SHEET, LEGS_SHEET):
        path = os.path.join(a.sheets, name)
        if not os.path.exists(path):
            sys.exit(f"stretch: {path} is missing (see clean/README.md, 기지개)")
        with open(path, "rb") as fh:
            sources[name] = hashlib.sha256(fh.read()).hexdigest()
        known = old.get("sources", {}).get(name)
        if known and known != sources[name]:
            sys.exit(f"stretch: {name} is not the sheet the kept fits were found on; run with --refit")

    L = rest_layers(a.aseprite)
    base, tail, halo, body = L["base"], L["hair tail"], L["halo"], L["body"]
    H, W = base.shape
    rows, cols = np.arange(H)[:, None], np.arange(W)[None, :]
    # The rest pose as the app shows it (the UI test's reference render), eyes and mouth included.
    rest = np.asarray(Image.open(os.path.join(HERE, "..", "..", "..", "Himari.Companion.UiTests", "Fixtures",
                                              "character", "idle-1-neutral.png"))).astype(int)
    rest = np.vstack([np.zeros((TOP, W), int), rest])
    halo_idx = sorted(set(halo[halo > 0].tolist()))
    hair_idx = sorted(set(body[TOP + 12:TOP + 30, 95:135][body[TOP + 12:TOP + 30, 95:135] > 0].tolist()) - set(halo_idx))
    used = sorted({s for s, *_ in SEQ if s > 1}), sorted({l for _, l, *_ in SEQ if l > 0})

    # Legs, fitted on the wheelchair outside the legs zone.
    chair = base.copy()
    chair[TOP + 106:TOP + 196, 100:177] = 0   # legs.lua's legs box
    chair[:TOP + 90] = 0
    legs = {}
    for n in used[1]:
        idx = cut(os.path.join(a.sheets, LEGS_SHEET), LEG_CELLS[n])
        if str(n) not in fits["legs"]:
            f = fit(idx, chair[TOP:], chair[TOP:] > 0, (0, 0, W, H - TOP), np.arange(2.40, 3.12, .04), ((-120, 60), (-120, 40)))
            fits["legs"][str(n)] = f
            print(f"legs {n}: chair match {f[0]} scale {f[1]}", flush=True)
        _, s, ox, oy = fits["legs"][str(n)]
        g = sample(idx, s, ox, oy - TOP * s, W, H, mode=True)
        zone = (rows >= TOP + LEGS[1]) & (cols >= LEGS[0])
        legs[n] = np.where(zone, g, 0)
        save(legs[n], f"legs-{n}")

    # Upper body, fitted on the head; the key poses' own halo gives way to the rest pose's.
    faces = {}
    for n in used[0]:
        idx = cut(os.path.join(a.sheets, STRETCH_SHEET), UPPER_CELLS[n])
        if str(n) not in fits["upper"]:
            sh, sw = idx.shape
            f = fit(idx, rest[TOP:], rest[TOP:] > 0, HEAD, np.arange(2.40, 3.12, .04),
                    ((0, sw - (HEAD[2] - HEAD[0]) * 1.2), (0, sh * .6)))
            fits["upper"][str(n)] = f
            print(f"upper {n}: head match {f[0]} scale {f[1]}", flush=True)
        _, s, ox, oy = fits["upper"][str(n)]
        g = sample(idx, s, ox, oy - TOP * s, W, H, mode=True)
        for comp in components(g > 0):  # pieces of the neighbouring key pose at the edges
            cys, cxs = zip(*comp)
            if len(comp) < 300 and (min(cxs) >= 160 or max(cxs) <= 36) and max(cys) < 130 + TOP:
                for y, x in comp:
                    g[y, x] = 0
        crown = int(np.argmax(np.isin(g[:, 95:136], HEAD_HAIR).sum(1) >= 6))
        near_hand = np.zeros_like(g, bool)
        hy, hx = np.nonzero(np.isin(g, HAND_OUTLINE))
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                near_hand[np.clip(hy + dy, 0, H - 1), np.clip(hx + dx, 0, W - 1)] = True
        band = (cols >= 60) & (cols <= 170) & ~near_hand
        above = (rows < crown) & band & np.isin(g, HALO_COLOURS)
        beside = (rows >= crown) & (rows < crown + 16) & ((cols < 92) | (cols > 140)) & band & np.isin(g, [21, 23, 25, 31])
        g = np.where(above | beside, 0, g)
        g = np.where((halo > 0) & ((g == 0) | np.isin(g, hair_idx)), halo, g)
        if n not in OWN_FACE:
            faces[n] = face_offset(g)
        pair = {l for s2, l, *_ in SEQ if s2 == n}
        assert len(pair) == 1, f"upper {n} pairs with several leg poses {pair}"
        lp = pair.pop()
        under_base = np.where((rows >= TOP + LEGS[1]) & (cols >= LEGS[0]), legs[lp], base) if lp else base
        under = np.where(tail > 0, tail, under_base)
        use = (g > 0) & ((rows < TOPCUT + TOP) | ((rows < LOWCUT + TOP) & (under == 0)))
        layer = np.where(use, g, 0)
        save(layer, f"upper-{n}")
        for k in sorted({k for s2, _, _, k in SEQ if s2 == n and k}):
            save(lift(layer, k), f"upper-{n}-lift{k}")

    frames = []
    for s, l, ms, k in SEQ:
        f = {"ms": ms}
        if s > 1:
            f["upper"] = f"upper-{s}" + (f"-lift{k}" if k else "")
            f["face"] = "own" if s in OWN_FACE else faces[s]
        if l:
            f["legs"] = f"legs-{l}"
        frames.append(f)
    meta = {"note": "written by tools/stretch.py; clips.lua builds the stretch clip from frames",
            "sources": sources, "legsZone": list(LEGS), "fits": fits, "faces": {str(k): v for k, v in faces.items()}, "frames": frames}
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)
    print(f"stretch: {len(frames)} frames, {sum(f['ms'] for f in frames)} ms, faces {faces}")


def lift(g, k):
    """Rows above WAIST rise by k, the clasped hands (above HANDS) by at most 1; the row just
    above WAIST repeats, so the torso lengthens."""
    w, h = WAIST + TOP, HANDS + TOP
    y = np.arange(w)
    out = g.copy()
    out[:w] = g[np.where(y < h, y + min(k, 1), np.minimum(y + k, w - 1))]
    return out


def face_offset(g):
    """Where the neutral mood face lines up with the key pose's dark lines (eyes, brows)."""
    sheet = os.path.join(HERE, "..", "..", "..", "Himari.Companion", "Assets")
    manifest = json.load(open(os.path.join(sheet, "character.json")))
    x, y, w, h = manifest["faces"]["neutral"]["src"]
    neutral = np.asarray(Image.open(os.path.join(sheet, "character.png"))).astype(int)[y:y + h, x:x + w]
    dark = [i for i in range(1, 33) if PAL[i].sum() < 330]
    nd = np.isin(neutral, dark)
    best = (-np.inf,)
    for dy in range(-5, 6):
        for dx in range(-5, 6):
            r = np.isin(g[FY + dy + TOP:FY + dy + TOP + h, FX + dx:FX + dx + w], dark)
            sc = (r & nd).sum() - .5 * (r ^ nd).sum()
            if sc > best[0]:
                best = (sc, dx, dy)
    return [best[1], best[2]]


if __name__ == "__main__":
    main()
