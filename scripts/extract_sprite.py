"""Regenerate frontend/src/avatar/sprite.ts from the reference bitmap.

The reference is Nick's standing-Seymour intensity CSV (780x1305, one
black-intensity value 0..255 per pixel), kept gzipped next to this script.
Steps: recover the native pixel pitch (6.62px) from outline edges, flood
the OUTSIDE on a dilated ink mask (dilation seals anti-aliasing pinholes;
body fill and paper are the same intensity, so only topology separates
them), classify regions via connected components (eye whites, horns,
tuft, tongue), majority-vote each native cell into a class, and emit the
TypeScript sprite module plus palette-mapped preview PNGs.

Usage: python scripts/extract_sprite.py [reference.csv]
       (no argument: uses scripts/seymour_reference.csv.gz)

NOTE: the face anchors exported at the bottom (EYE_XS, MOUTH_BOX, ...)
are measured constants — re-check them against the class dump if the
reference art ever changes shape.
"""
import gzip, json, pathlib, struct, sys, zlib
from collections import deque

HERE = pathlib.Path(__file__).resolve().parent
CSV = sys.argv[1] if len(sys.argv) > 1 else str(HERE / "seymour_reference.csv.gz")
OUT = str(HERE)                              # previews land next to the script
TS = HERE.parent / "frontend" / "src" / "avatar" / "sprite.ts"

rows = []
with (gzip.open(CSV, "rt") if CSV.endswith(".gz") else open(CSV)) as f:
    for line in f:
        line = line.strip().rstrip(",")
        if line:
            rows.append([int(v) for v in line.split(",")])
H, W = len(rows), len(rows[0])
INK = 200

# ---- 1. native pitch: edges of the ink mask, fit pitch+offset ------------
def edges_along(get, n, m):
    """Positions where ink starts/stops scanning index 0..n-1 for each of m lines."""
    es = []
    for j in range(m):
        prev = False
        for i in range(n):
            v = get(i, j) >= INK
            if v != prev:
                es.append(i)
            prev = v
    return es

xe = edges_along(lambda x, y: rows[320 + (y * 7) % 680][x], W, 97)
ye = edges_along(lambda y, x: rows[y][110 + (x * 7) % 558], H, 79)

def fit(es):
    best = None
    for p100 in range(580, 740):
        p = p100 / 100
        # offset via circular mean of (e mod p)
        import math
        sx = sum(math.cos(2*math.pi*(e % p)/p) for e in es)
        sy = sum(math.sin(2*math.pi*(e % p)/p) for e in es)
        off = (math.atan2(sy, sx) / (2*math.pi)) * p % p
        err = sum(min((e - off) % p, p - (e - off) % p) for e in es) / len(es)
        if best is None or err < best[2]:
            best = (p, off, err)
    return best

px, ox, ex_ = fit(xe)
py, oy, ey_ = fit(ye)
print(f"pitch x={px} off={ox:.2f} err={ex_:.2f} | y={py} off={oy:.2f} err={ey_:.2f}",
      file=sys.stderr)

# ---- 2. outside flood on dilated ink --------------------------------------
R = 3                                   # dilation radius seals AA pinholes
ink = [[rows[y][x] >= INK for x in range(W)] for y in range(H)]
dil = [[False]*W for _ in range(H)]
for y in range(H):
    for x in range(W):
        if ink[y][x]:
            for dy in range(-R, R+1):
                for dx in range(-R, R+1):
                    ny, nx = y+dy, x+dx
                    if 0 <= ny < H and 0 <= nx < W:
                        dil[ny][nx] = True
outside = [[False]*W for _ in range(H)]
q = deque()
for seed in [(2, H//2), (W-3, H//2), (W//2, 250), (W//2, H-50)]:
    x, y = seed
    if not dil[y][x]:
        outside[y][x] = True; q.append((x, y))
while q:
    x, y = q.popleft()
    for nx, ny in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
        if 0 <= nx < W and 0 <= ny < H and not outside[ny][nx] and not dil[ny][nx]:
            outside[ny][nx] = True; q.append((nx, ny))
# undilate: pixels near outside that aren't ink are outside too
for _ in range(R):
    grow = []
    for y in range(H):
        for x in range(W):
            if not outside[y][x] and not ink[y][x]:
                if any(0 <= ny < H and 0 <= nx < W and outside[ny][nx]
                       for nx, ny in ((x-1,y),(x+1,y),(x,y-1),(x,y+1))):
                    grow.append((x, y))
    for x, y in grow:
        outside[y][x] = True

# ---- 3. inside-light components (body / eye whites / horns / tuft) -------
LIGHT = 36
cls = [["."]*W for _ in range(H)]
for y in range(H):
    for x in range(W):
        if outside[y][x]:
            continue
        v = rows[y][x]
        cls[y][x] = "k" if v >= INK else ("m" if v >= 100 else
                    "s" if v >= LIGHT else "L")   # L = inside light, tbd
comp = [[0]*W for _ in range(H)]
comps = {}
cid = 0
for y in range(H):
    for x in range(W):
        if cls[y][x] == "L" and comp[y][x] == 0:
            cid += 1
            qq = deque([(x, y)]); comp[y][x] = cid
            size = 0; minx = maxx = x; miny = maxy = y
            while qq:
                cx, cy = qq.popleft(); size += 1
                minx = min(minx, cx); maxx = max(maxx, cx)
                miny = min(miny, cy); maxy = max(maxy, cy)
                for nx, ny in ((cx-1,cy),(cx+1,cy),(cx,cy-1),(cx,cy+1)):
                    if 0 <= nx < W and 0 <= ny < H and cls[ny][nx] == "L" and comp[ny][nx] == 0:
                        comp[ny][nx] = cid; qq.append((nx, ny))
            comps[cid] = dict(size=size, bbox=(minx, maxx, miny, maxy))
big = sorted(comps, key=lambda c: -comps[c]["size"])
print("light comps:", [(c, comps[c]["size"], comps[c]["bbox"]) for c in big[:12]],
      file=sys.stderr)
body_id = big[0]
# eye whites: sizeable comps fully inside the eye band (y 500..625)
eyes = [c for c in big[1:] if comps[c]["size"] > 800
        and comps[c]["bbox"][2] > 480 and comps[c]["bbox"][3] < 640]
# horns: comps above the head line, well left/right of center
horns = [c for c in big[1:] if comps[c]["size"] > 300
         and comps[c]["bbox"][3] < 480
         and (comps[c]["bbox"][1] < 300 or comps[c]["bbox"][0] > 470)]
# everything else small: catch-lights inside pupils ('w'), tuft ('L'->body)
print("eyes:", eyes, "horns:", horns, file=sys.stderr)

for y in range(H):
    for x in range(W):
        if cls[y][x] == "L":
            c = comp[y][x]
            cls[y][x] = ("w" if c in eyes else "o" if c in horns else
                         "w" if comps[c]["size"] <= 60
                             and 480 < y < 640 else "b")

# mouth: the ink blob containing the mouth sample — find its bbox to mark
# the tongue ('m' inside the mouth bbox -> 't'; elsewhere 'm' -> shade or ridge)
mq = deque([(380, 665)])
mouth_seen = {(380, 665)}
mminx = mmaxx = 380; mminy = mmaxy = 665
while mq:
    x, y = mq.popleft()
    mminx = min(mminx, x); mmaxx = max(mmaxx, x)
    mminy = min(mminy, y); mmaxy = max(mmaxy, y)
    for nx, ny in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
        if (nx, ny) not in mouth_seen and 0 <= nx < W and 0 <= ny < H \
           and rows[ny][nx] >= 130:      # mouth interior incl. tongue is >=130
            mouth_seen.add((nx, ny)); mq.append((nx, ny))
print("mouth bbox:", (mminx, mmaxx, mminy, mmaxy), file=sys.stderr)
horn_boxes = [comps[c]["bbox"] for c in horns]
# Tongue: ANY non-ink pixel inside the mouth bbox that has ink both above
# and below it (vertically enclosed by the lips) — the tongue's light
# middle and darker rim alike. Corner skin above/below the lip curves
# fails the enclosure test and stays skin.
for y in range(mminy, mmaxy + 1):
    for x in range(mminx, mmaxx + 1):
        if cls[y][x] in ("m", "s", "b"):
            above = any(rows[yy][x] >= INK for yy in range(mminy, y))
            below = any(rows[yy][x] >= INK for yy in range(y + 1, mmaxy + 1))
            if above and below:
                cls[y][x] = "t"
for y in range(H):
    for x in range(W):
        if cls[y][x] == "m":
            if any(bx0-14 <= x <= bx1+14 and by0-14 <= y <= by1+14
                   for bx0, bx1, by0, by1 in horn_boxes):
                cls[y][x] = "d"          # horn ridge stripes
            else:
                cls[y][x] = "s"
# Horn bases: the artist drew no line where horn meets head (both fills
# are white), so the segment below the last ridge band lands in the body
# component. Claim body pixels just below/inside each horn bbox back for
# the horn, stopping at the head outline's ink.
for bx0, bx1, by0, by1 in horn_boxes:
    for y in range(by0, min(H, by1 + 26)):
        for x in range(max(0, bx0 - 6), min(W, bx1 + 7)):
            if cls[y][x] == "b":
                cls[y][x] = "o"

# ---- 4. sample native cells ----------------------------------------------
import math
gx0 = math.floor((110 - ox) / px)       # first cell column touching the bbox
gx1 = math.ceil((668 - ox) / px)
gy0 = math.floor((320 - oy) / py)
gy1 = math.ceil((1000 - oy) / py)
SW, SH = gx1 - gx0 + 1, gy1 - gy0 + 1
print("sprite grid:", SW, "x", SH, file=sys.stderr)

def cell_class(i, j):
    """Majority class over the cell's pixels (center 60% to dodge AA edges)."""
    x0 = ox + (gx0 + i) * px; y0 = oy + (gy0 + j) * py
    counts = {}
    a0, a1 = int(x0 + px*0.2), int(x0 + px*0.8) + 1
    b0, b1 = int(y0 + py*0.2), int(y0 + py*0.8) + 1
    for yy in range(max(0, b0), min(H, b1)):
        for xx in range(max(0, a0), min(W, a1)):
            counts[cls[yy][xx]] = counts.get(cls[yy][xx], 0) + 1
    if not counts:
        return "."
    # ink wins ties/pluralities to keep outlines closed
    total = sum(counts.values())
    if counts.get("k", 0) >= total * 0.34:
        return "k"
    return max(counts, key=lambda k: counts[k])

grid = [[cell_class(i, j) for i in range(SW)] for j in range(SH)]

# ---- 5. previews ----------------------------------------------------------
def png(path, pix, scale):
    h, w = len(pix), len(pix[0])
    raw = b""
    for row in pix:
        line = b""
        for p in row:
            line += bytes(p) * scale
        raw += (b"\x00" + line) * scale
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    open(path, "wb").write(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w*scale, h*scale, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

LIGHTPAL = {".": (240, 235, 224), "k": (38, 36, 37), "b": (168, 196, 222),
            "s": (118, 148, 178), "w": (247, 247, 250), "o": (229, 196, 148),
            "d": (118, 148, 178), "t": (178, 74, 74)}
GB = {".": (155, 188, 15), "k": (15, 56, 15), "b": (139, 172, 15),
      "s": (48, 98, 48), "w": (155, 188, 15), "o": (139, 172, 15),
      "d": (48, 98, 48), "t": (48, 98, 48)}
png(f"{OUT}/preview_theme.png", [[LIGHTPAL[c] for c in r] for r in grid], 6)
png(f"{OUT}/preview_gb.png", [[GB[c] for c in r] for r in grid], 6)

# Stray light cells inside the three eye whites are catch-lights the
# component pass missed (their tiny components fell to "body"): eye
# interiors hold only ink and white, so any body cell there is white.
EYE_COLS = [(21, 31), (34, 45), (47, 58)]      # abs col ranges of the whites
for j in range(30, 45):
    for x0c, x1c in EYE_COLS:
        for i in range(x0c, x1c + 1):
            if grid[j][i] == "b":
                grid[j][i] = "w"

out_rows = ["".join(r) for r in grid]
assert {c for r in out_rows for c in r} <= set(".kbswodt")
lines = ",\n".join(f'  "{r}"' for r in out_rows)
TS.write_text(f'''/** sprite.ts — Seymour himself, cell for cell.
 *
 * This sprite was EXTRACTED from Nick's reference bitmap (the standing-
 * monster intensity CSV, 780×1305 grayscale): the source's native pixel
 * pitch (6.62px) was recovered from its outline edges, every native cell
 * was classified by region (flood fill + connected components), and the
 * result is this {SW}×{SH} grid — so the avatar now IS the reference,
 * not an approximation of it. Regenerate with scripts/extract_sprite.py
 * if the reference art ever changes.
 *
 * One character per cell, mapped onto the live palette at draw time
 * (scene.ts), which is what keeps the sprite tintable by theme, hue and
 * the Game Boy screen option:
 *   '.' transparent   'k' line (ink)   'b' body (skin)   's' bodyDim
 *   'w' eye white     'o' horn         'd' horn ridge    't' tongue
 */

/** The grid: {SW} columns × {SH} rows (row 0 = horn tips, last = toes). */
export const SPRITE_W = {SW};
export const SPRITE_H = {SH};
export const SEYMOUR: string[] = [
{lines},
];

/** Face anchors, in CELL coordinates — measured from the same extraction,
 *  used by scene.ts to overdraw expressions (blinks, glances, mouths)
 *  without disturbing the rest of the sprite. */
export const HEAD_TOP = 11;            // head-outline crown row (blit anchor)
export const EYE_Y = 37;               // the shared eye-center row
export const EYE_XS = [26, 39, 52];    // left / middle / right eye centers
export const EYE_RX = 6;               // eye outline half-width (they touch)
export const EYE_RY = 8;               // …and half-height (slightly tall)
export const PUPIL_R = 3;              // the resting pupil radius
/** The face bands an overdraw may erase: [x0, y0, x1, y1] inclusive. */
export const EYE_BAND: [number, number, number, number] = [19, 29, 60, 46];
export const MOUTH_BOX: [number, number, number, number] = [27, 50, 51, 59];
''')
print(f"wrote {TS}", file=sys.stderr)
