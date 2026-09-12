"""Extract both reference scenes into class grids (pixel-level regions).

Per pixel: nearest tone anchor. Barrier = the two darkest anchors (D ink
+ D2 dark strokes) — D2 seals soft outlines that plain ink misses.
Outside = flood from border over DILATED barrier. Enclosed components
get semantics from per-scene furniture ZONES (with a tone rule where a
zone overlaps the monster, e.g. his feet in front of the keyboard);
whatever no zone claims inside the monster area is monster. Ink splits
into monster 'k' vs furniture 'i' by neighborhood. 7px cell vote emits
the char grid + measured eye anchors.
"""
from PIL import Image
from collections import Counter, deque
import json, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
TS = HERE.parent / "frontend" / "src" / "avatar" / "scenes.ts"
CELL = 7
RESULTS = {}                      # scene name -> exported dict

def extract(name, crop, anchors, cfg, out):
    im = Image.open(HERE / f"scene_reference_{name}.webp").convert("RGB")
    src = im.load()
    x0, y0, x1, y1 = crop
    W, H = x1 - x0, y1 - y0
    keys = list(anchors)
    avals = [anchors[k] for k in keys]
    tone = [bytearray(W) for _ in range(H)]
    for y in range(H):
        ty = tone[y]
        for x in range(W):
            c = src[x0 + x, y0 + y]
            bi, bd = 0, 1 << 30
            for a, av in enumerate(avals):
                d = (c[0]-av[0])**2 + (c[1]-av[1])**2 + (c[2]-av[2])**2
                if d < bd: bd, bi = d, a
            ty[x] = bi
    BAR = {keys.index(k) for k in ("D", "D2") if k in keys}
    bar = [[tone[y][x] in BAR for x in range(W)] for y in range(H)]
    R = 3
    dil = [[False]*W for _ in range(H)]
    for y in range(H):
        for x in range(W):
            if bar[y][x]:
                for dy in range(-R, R+1):
                    yy = y + dy
                    if 0 <= yy < H:
                        row = dil[yy]
                        for dx in range(-R, R+1):
                            xx = x + dx
                            if 0 <= xx < W: row[xx] = True
    outside = [[False]*W for _ in range(H)]
    q = deque()
    for x in range(W):
        for y in (0, H-1):
            if not dil[y][x]: outside[y][x] = True; q.append((x, y))
    for y in range(H):
        for x in (0, W-1):
            if not dil[y][x] and not outside[y][x]:
                outside[y][x] = True; q.append((x, y))
    while q:
        x, y = q.popleft()
        for nx, ny in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
            if 0 <= nx < W and 0 <= ny < H and not outside[ny][nx] and not dil[ny][nx]:
                outside[ny][nx] = True; q.append((nx, ny))
    for _ in range(R):
        grow = []
        for y in range(H):
            for x in range(W):
                if not outside[y][x] and not bar[y][x]:
                    for nx, ny in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                        if 0 <= nx < W and 0 <= ny < H and outside[ny][nx]:
                            grow.append((x, y)); break
        for x, y in grow: outside[y][x] = True
    comp = [[0]*W for _ in range(H)]
    comps = {}
    cid = 0
    for y in range(H):
        for x in range(W):
            if not bar[y][x] and not outside[y][x] and comp[y][x] == 0:
                cid += 1
                qq = deque([(x, y)]); comp[y][x] = cid
                n = 0; minx = maxx = x; miny = maxy = y
                tc = Counter()
                while qq:
                    cx, cy = qq.popleft(); n += 1
                    tc[keys[tone[cy][cx]]] += 1
                    if cx < minx: minx = cx
                    if cx > maxx: maxx = cx
                    if cy < miny: miny = cy
                    if cy > maxy: maxy = cy
                    for nx, ny in ((cx-1,cy),(cx+1,cy),(cx,cy-1),(cx,cy+1)):
                        if 0 <= nx < W and 0 <= ny < H and comp[ny][nx] == 0 \
                           and not bar[ny][nx] and not outside[ny][nx]:
                            comp[ny][nx] = cid; qq.append((nx, ny))
                comps[cid] = {"n": n, "bb": (minx, maxx, miny, maxy), "tones": tc}
    # ---- semantics: zones first, then bands, then default ----------------
    sem = {}
    for c, d in comps.items():
        n = d["n"]; a, b, cc, dd = d["bb"]
        cxm, cym = (a + b) / 2, (cc + dd) / 2
        light = d["tones"].get("S", 0) + d["tones"].get("W", 0)
        kind = None
        for (zx0, zx1, zy0, zy1) in cfg.get("horn_zones", []):
            if zx0 <= cxm <= zx1 and zy0 <= cym <= zy1:
                kind = "horn"
                break
        if kind is None and cc > cfg.get("monster_floor", 10**9):
            kind = "furn"                # starts below his feet: desk furniture
        if kind is None:
          for (zx0, zx1, zy0, zy1, zkind, zrule) in cfg["zones"]:
            if zx0 <= cxm <= zx1 and zy0 <= cym <= zy1:
                if zrule == "light-is-monster" and light > n * 0.5:
                    kind = "monster"
                elif zrule == "big-is-monster" and n > 700:
                    kind = "monster"
                else:
                    kind = zkind
                break
        if kind is None:
            e = cfg["eyes"]
            if e[0] <= cym <= e[1] and cfg["mx"][0] <= cxm <= cfg["mx"][1] \
               and (200 < n < 8000 or n <= 40):
                kind = "eye"          # whites / catch-lights, never the body
            elif cym < cfg["crown"] and n > 150 \
                 and cfg["mx"][0] <= cxm <= cfg["mx"][1]:
                kind = "horn"
            elif cfg["mx"][0] <= cxm <= cfg["mx"][1] and cym < cfg["deskline"] + 60:
                kind = "monster"
            else:
                kind = "furn"
        sem[c] = kind
    mb = cfg.get("mouth")
    if mb:
        for c, d in comps.items():
            a, b, cc, dd = d["bb"]
            if sem[c] != "eye" and a >= mb[0] and b <= mb[1] and cc >= mb[2] and dd <= mb[3]:
                sem[c] = "tongue"
    # report
    print(f"{name}:", file=sys.stderr)
    e = cfg["eyes"]
    for c, d in comps.items():
        a, b, cc, dd = d["bb"]
        if e[0] <= (cc + dd) / 2 <= e[1] and d["n"] > 100:
            print(f"   eyeband comp {c}: n={d['n']} bb={d['bb']} -> {sem[c]}",
                  file=sys.stderr)
    for c, d in sorted(comps.items(), key=lambda kv: -kv[1]["n"])[:18]:
        print(f"   {c}: n={d['n']} bb={d['bb']} {sem[c]}", file=sys.stderr)
    tmap = cfg["tones"]
    ch = [["."]*W for _ in range(H)]
    for y in range(H):
        for x in range(W):
            if bar[y][x]: ch[y][x] = "K"; continue
            if outside[y][x]:
                t = keys[tone[y][x]]
                gz = cfg.get("glyph_zone")
                if gz and t not in ("S", "S2", "W") \
                   and gz[0] <= x <= gz[1] and gz[2] <= y <= gz[3]:
                    ch[y][x] = "g"                     # the steam strokes
                elif y >= cfg["deskline"] and cfg.get("front_fill"):
                    ch[y][x] = "f"
                continue
            kind = sem.get(comp[y][x], "furn") if comp[y][x] else "furn"
            t = keys[tone[y][x]]
            ch[y][x] = tmap[kind].get(t, tmap[kind]["*"])
    if cfg.get("front_fill"):
        for y in range(cfg["deskline"], H):
            for x in range(W):
                if ch[y][x] == ".": ch[y][x] = "f"
    MONSTER_CH = set("bswodt")
    for y in range(H):
        for x in range(W):
            if ch[y][x] not in ("K", "g"): continue
            mono = furn = 0
            for r in (2, 5):
                for nx, ny in ((x-r,y),(x+r,y),(x,y-r),(x,y+r),
                               (x-r,y-r),(x+r,y-r),(x-r,y+r),(x+r,y+r)):
                    if 0 <= nx < W and 0 <= ny < H:
                        cc = ch[ny][nx]
                        if cc in MONSTER_CH: mono += 1
                        elif cc in ("f", "m"): furn += 1
            ch[y][x] = "k" if mono > furn else "i"
    GW, GH = W // CELL, H // CELL
    grid = []
    for j in range(GH):
        row = []
        for i in range(GW):
            cnt = Counter()
            for dy in range(1, CELL-1):
                for dx in range(1, CELL-1):
                    cnt[ch[j*CELL+dy][i*CELL+dx]] += 1
            total = sum(cnt.values())
            dark = cnt.get("k", 0) + cnt.get("i", 0)
            if dark >= total * 0.34:
                row.append("k" if cnt.get("k", 0) >= cnt.get("i", 0) else "i")
            else:
                row.append(cnt.most_common(1)[0][0])
        grid.append("".join(row))
    # eye anchors: from eye-comp bboxes (px) when found, else from config
    grid = [list(r) for r in grid]
    eyecomps = sorted((c for c in comps if sem[c] == "eye" and comps[c]["n"] > 500),
                      key=lambda c: comps[c]["bb"][0])
    if eyecomps:
        eyes = []
        for c in eyecomps:
            a, b, cc, dd = comps[c]["bb"]
            eyes.append((round((a + b) / 2 / CELL), round((cc + dd) / 2 / CELL)))
    else:
        eyes = cfg["eyes_fix"]
        # the whites share the body tone in this art — paint them in so the
        # eyes still read as eyes when the body is tinted
        rx, ry = cfg["eyes_fix_r"]
        for ex, ey in eyes:
            for j in range(ey - ry, ey + ry + 1):
                for i in range(ex - rx, ex + rx + 1):
                    if 0 <= i < GW and 0 <= j < GH \
                       and ((i-ex)/rx)**2 + ((j-ey)/ry)**2 <= 1 \
                       and grid[j][i] in ("b", "s"):
                        grid[j][i] = "w"
    for (fx0, fx1, fy0, fy1) in cfg.get("furn_fix", []):
        for j in range(fy0, fy1 + 1):
            for i in range(fx0, fx1 + 1):
                if 0 <= i < GW and 0 <= j < GH:
                    c = grid[j][i]
                    if c == "b": grid[j][i] = "f"
                    elif c in ("s", "w"): grid[j][i] = "m"
    for (hx0, hx1, hy0, hy1) in cfg.get("horn_fix", []):
        for j in range(hy0, hy1 + 1):
            for i in range(hx0, hx1 + 1):
                if 0 <= i < GW and 0 <= j < GH:
                    c = grid[j][i]
                    if c == "b": grid[j][i] = "o"
                    elif c == "s": grid[j][i] = "d"
    tl, tt, tr, tb = cfg.get("trim", (0, 0, 0, 0))
    grid = ["".join(r[tl:GW - tr]) for r in grid[tt:GH - tb]]
    GW -= tl + tr; GH -= tt + tb
    eyes = [(x - tl, y - tt) for x, y in eyes]
    mc = [v // CELL for v in cfg["mouth"]]
    anchors_out = {
        "eyes": eyes,
        "mouth": [mc[0] - tl, mc[1] - tl, mc[2] - tt, mc[3] - tt],
        "deskline": cfg["deskline"] // CELL - tt,
    }
    print(f"   grid {GW}x{GH}, eyes={eyes}", file=sys.stderr)
    RESULTS[out] = {"GW": GW, "GH": GH, "rows": grid, **anchors_out}

TONES_DESK = {
    "monster": {"B": "b", "s": "s", "c": "s", "W": "w", "S": "b", "S2": "b",
                "F": "b", "M": "s", "*": "b"},
    "eye":     {"W": "w", "S": "w", "S2": "w", "B": "w", "s": "s", "*": "w"},
    "horn":    {"S": "o", "S2": "d", "F": "o", "M": "d", "B": "o", "s": "d",
                "W": "o", "*": "o"},
    "tongue":  {"W": "w", "*": "t"},
    "furn":    {"S": "f", "S2": "f", "F": "f", "M": "m", "B": "m", "s": "m",
                "c": "m", "W": "w", "*": "f"},
}
extract("desk", (172, 158, 1231, 950), {
    "S":  (184, 197, 154), "S2": (170, 185, 138),
    "F":  (165, 175, 141), "M":  (128, 143, 110),
    "B":  (150, 181, 177), "s":  (118, 152, 153),
    "W":  (199, 211, 182), "D":  (28, 52, 46), "D2": (58, 92, 90),
}, {
    "eyes": (280, 440), "crown": 220, "deskline": 640, "mx": (390, 980),
    "mouth": (490, 720, 430, 530), "front_fill": True, "trim": (0, 1, 0, 0),
    "furn_fix": [(56, 82, 63, 68)],
    "zones": [
        (0, 385, 0, 640, "furn", None),            # CRT + left wall objects
        (280, 640, 380, 640, "furn", "big-is-monster"),  # keyboard (paws are big)
        (905, 1060, 240, 640, "furn", None),       # chair sliver
    ],
    "tones": TONES_DESK,
}, "desk")

TONES_TEA = {
    "monster": {"S": "b", "W": "w", "s": "s", "M": "s", "c": "s", "*": "b"},
    "eye":     {"W": "w", "S": "w", "s": "s", "*": "w"},
    "horn":    {"S": "o", "s": "d", "M": "d", "W": "o", "*": "o"},
    "tongue":  {"*": "t"},
    "furn":    {"W": "w", "S": "f", "s": "f", "M": "m", "c": "m", "*": "f"},
}
extract("tea", (60, 104, 1032, 1392), {
    "W": (214, 209, 148), "S": (190, 189, 125), "s": (163, 161, 102),
    "M": (117, 123, 77), "c": (84, 95, 57), "D": (30, 40, 12), "D2": (62, 72, 40),
}, {
    "eyes": (460, 610), "crown": 400, "deskline": 1170, "mx": (300, 920),
    "mouth": (555, 800, 620, 730), "glyphs": True,
    "eyes_fix": [(80, 76), (91, 76), (102, 77)], "eyes_fix_r": (4, 5),
    "trim": (4, 2, 2, 2), "monster_floor": 1030,
    "horn_fix": [(106, 126, 33, 60)],
    "glyph_zone": (355, 485, 240, 490),
    "horn_zones": [(480, 625, 330, 480), (735, 885, 330, 480)],
    "zones": [
        (40, 330, 600, 1030, "furn", None),        # computer
        (320, 655, 870, 1050, "furn", "light-is-monster"),  # keyboard vs feet
        (395, 545, 540, 675, "furn", None),        # teacup (held up)
        (690, 930, 980, 1160, "furn", None),       # teapot + saucer
        (830, 1000, 430, 1000, "furn", None),      # chair
    ],
    "tones": TONES_TEA,
}, "tea")
def block(d, const, doc):
    rows = ",\n".join(f'    "{r}"' for r in d["rows"])
    return (f"/** {doc} */\n"
            f"export const {const}: Scene = {{\n"
            f"  w: {d['GW']}, h: {d['GH']},\n"
            f"  eyes: {json.dumps(d['eyes'])},\n"
            f"  mouth: {json.dumps(d['mouth'])},\n"
            f"  deskline: {d['deskline']},\n"
            f"  rows: [\n{rows},\n  ],\n}};")

TS.write_text(f'''/** scenes.ts — the two full scenes Seymour lives in, cell for cell.
 *
 * Both were EXTRACTED from Nick's reference art (the framed "monster at
 * the computer" and "tea break, feet on the desk" images): each source's
 * screen area was located inside its bezel, every pixel classified by
 * tone + region (flood fill on a dilated ink mask, enclosed components,
 * furniture zones), and the result voted into 7px cells — so the scenes
 * ARE the references. Regenerate with scripts/extract_scenes.py if the
 * reference art ever changes.
 *
 * One character per cell, mapped onto the live palette at draw time:
 *   '.' wall (transparent — the canvas paper + dot texture shows)
 *   'k' monster ink   'i' furniture ink   'g' floating strokes (steam)
 *   'b' body          's' body shade      'w' white (eyes/teeth/cup)
 *   'o' horn          'd' horn ridge      't' tongue
 *   'f' furniture fill  'm' furniture mid
 */

/** One extracted scene: the grid plus its measured face anchors. */
export interface Scene {{
  w: number;                     // grid width in cells
  h: number;                     // grid height in cells
  eyes: [number, number][];      // the three eye centers, in cells
  mouth: [number, number, number, number];  // erasable mouth box [x0,x1,y0,y1]
  deskline: number;              // the desk edge row (composition anchor)
  rows: string[];                // h strings of w cells
}}

{block(RESULTS["desk"], "DESK_SCENE",
       "Seymour typing at the CRT — every state where he is at work\n"
       " *  (working / waiting / blocked / sleeping wear their poses on it).")}

{block(RESULTS["tea"], "TEA_SCENE",
       "Feet on the desk, cup in paw — the loaded-but-idle scene (and the\n"
       " *  after-work tea break; both are the same well-earned recline).")}
''')
print(f"wrote {TS}", file=sys.stderr)
