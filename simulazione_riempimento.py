# Geometric simulation of the fill computation (standard library only, ~40 s).
# Pinhole camera perpendicular to the floor, 150 mm above the rim, 70 degree horizontal field;
# bin inside 1000 x 800 x 1100 mm. PERFECT depth (no model error): the numbers isolate the
# error due to the aggregation method and to the geometry.
#   repo mediana  = current method of depth_fill.analyze() with a bin_mask that includes the walls
#   solo-fondo    = same median with a mask that contains only the floor
#   mappa [a - b] = height map of the floor: hidden cells empty (a) or full up to the line of sight (b)
#   con memoria   = height map where hidden cells keep the height seen in the previous photo
# Usage: python simulazione_riempimento.py
import math, statistics

W, H = 200, 150
HFOV = math.radians(70)
f = (W / 2) / math.tan(HFOV / 2)
cx, cy = W / 2, H / 2
RIM, FLOOR = 150.0, 1250.0
BX, BY = 500.0, 400.0
DEPTH = FLOOR - RIM
CELL = 20.0
NX, NY = int(2 * BX / CELL), int(2 * BY / CELL)

def hit(dx, dy, boxes):
    z_exit = FLOOR
    if dx: z_exit = min(z_exit, BX / abs(dx))
    if dy: z_exit = min(z_exit, BY / abs(dy))
    if z_exit < RIM:
        return None, None
    kind = "floor" if z_exit >= FLOOR else "wall"
    z = z_exit
    for x0, x1, y0, y1, top in boxes:
        c = []
        if x0 <= dx * top <= x1 and y0 <= dy * top <= y1: c.append(top)
        for xs in (x0, x1):
            if dx:
                t = xs / dx
                if top <= t <= FLOOR and y0 <= dy * t <= y1: c.append(t)
        for ys in (y0, y1):
            if dy:
                t = ys / dy
                if top <= t <= FLOOR and x0 <= dx * t <= x1: c.append(t)
        if c: z = min(z, min(c))
    return kind, z

def cell_of(x, y):
    i, j = int((x + BX) // CELL), int((y + BY) // CELL)
    return (min(max(i, 0), NX - 1), min(max(j, 0), NY - 1))

def height_map(boxes):
    """Height seen per cell (None = hidden) and upper bound from the line of sight."""
    seen = {}
    upper = {(i, j): DEPTH for i in range(NX) for j in range(NY)}
    for v in range(H):
        for u in range(W):
            dx, dy = (u + 0.5 - cx) / f, (v + 0.5 - cy) / f
            kind, _ = hit(dx, dy, [])
            if kind is None:
                continue
            _, z = hit(dx, dy, boxes)
            x, y = dx * z, dy * z
            if abs(x) < BX - 1 and abs(y) < BY - 1:       # point on the contents/floor, not on a wall
                c = cell_of(x, y)
                seen[c] = max(seen.get(c, 0.0), FLOOR - z)
            zs = RIM                                     # free space along the ray
            while zs < z - 5:
                xs, ys = dx * zs, dy * zs
                if abs(xs) < BX and abs(ys) < BY:
                    c = cell_of(xs, ys)
                    upper[c] = min(upper[c], FLOOR - zs)
                zs += 10
    return seen, upper

def run(name, boxes, true_pct, previous=None):
    fill_all, fill_floor = [], []
    for v in range(H):
        for u in range(W):
            dx, dy = (u + 0.5 - cx) / f, (v + 0.5 - cy) / f
            kind, ze = hit(dx, dy, [])
            if kind is None: continue
            _, z = hit(dx, dy, boxes)
            h = min(max(ze - z, 0.0), DEPTH)
            fill_all.append(h)
            if kind == "floor": fill_floor.append(h)
    seen, upper = height_map(boxes)
    cells = [(i, j) for i in range(NX) for j in range(NY)]
    n = len(cells)
    lo = sum(seen.get(c, 0.0) for c in cells) / n / DEPTH * 100
    hi = sum(seen[c] if c in seen else upper[c] for c in cells) / n / DEPTH * 100
    hidden = 100 * (n - len(seen)) / n
    line = (f"{name:38s} vero {true_pct:5.1f} | repo mediana {100*statistics.median(fill_all)/DEPTH:5.1f}"
            f"  solo-fondo {100*statistics.median(fill_floor)/DEPTH:5.1f} | mappa [{lo:5.1f} - {hi:5.1f}]"
            f" nascoste {hidden:4.1f}%")
    if previous is not None:
        prev_seen, _ = height_map(previous)
        mem = sum(seen[c] if c in seen else prev_seen.get(c, 0.0) for c in cells) / n / DEPTH * 100
        line += f" | con memoria {mem:5.1f}"
    print(line)

A = 4 * BX * BY
def box(x0, x1, y0, y1, h): return (x0, x1, y0, y1, FLOOR - h)
def pct(v): return 100 * v / (A * DEPTH)

bag_c = box(-150, 150, -200, 200, 800)
layer20 = box(-BX, BX, -BY, BY, 200)
run("sacco alto al centro", [bag_c], pct(300 * 400 * 800), previous=[])
run("stesso sacco contro la parete", [box(200, 500, -200, 200, 800)], pct(300 * 400 * 800), previous=[])
run("strato uniforme 40 cm", [box(-BX, BX, -BY, BY, 400)], pct(A * 400))
run("strato 20 cm + sacco al centro", [layer20, bag_c], pct((A - 120000) * 200 + 120000 * 800), previous=[layer20])
run("strato uniforme 90 cm (quasi pieno)", [box(-BX, BX, -BY, BY, 900)], pct(A * 900))
