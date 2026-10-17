"""Build the Fleet Map basemap: frontend/public/world.geo.json.

Run this only when the basemap needs regenerating; the output is committed, so
a normal checkout does not need network access.

    ./.venv/Scripts/python.exe scripts/build_basemap.py

Why this is not just a downloaded file
--------------------------------------
ECharts needs GeoJSON, but the good public world outlines ship as TopoJSON, so
this converts. More importantly it solves a size problem. Natural Earth 10m
(~1 km coastline detail) is 8.6 MB as GeoJSON, and almost all of that is
coastline the Fleet Map never magnifies - Norwegian fjords, the Canadian
arctic, Patagonia. The map only ever zooms into the 14 ports in
`port_constraints.yaml`.

So this keeps every 10m vertex within NEAR_DEG of a port, and elsewhere keeps
a vertex only once the outline has moved MIN_STEP degrees since the last one
kept: full detail where someone will actually look, a coarse but correct
outline everywhere else, and ~1.9 MB instead of 8.6 MB. Because it is one
dataset sampled at two rates there is no seam between the detailed and coarse
regions.

Coarsening the rest is safe: at the zoom levels where distant coastline is
visible at all, the dropped vertices are sub-pixel.

The thinning is by distance, not by every Nth vertex. Index striding looks
equivalent and is not: where a ring runs along the antimeridian or a pole in
long collinear runs, dropping fixed indices leaves edges that cut clean across
the map. Bounding each step keeps the error local by construction.
"""
from __future__ import annotations

import gzip
import json
import urllib.request
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PORTS_FILE = REPO_ROOT / "backend" / "data" / "reference" / "port_constraints.yaml"
OUTPUT = REPO_ROOT / "frontend" / "public" / "world.geo.json"

SOURCE = "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-10m.json"
"""Natural Earth 1:10m country polygons, public domain, as TopoJSON."""

NEAR_DEG = 4.0    # full detail within this many degrees of a port
MIN_STEP = 0.06   # elsewhere, drop vertices closer than this to the last kept
DP = 3            # ~110 m coordinate precision; finer than any zoom we allow


def decode_arcs(topo: dict) -> list[list[list[float]]]:
    """Undo TopoJSON delta encoding and quantisation into absolute points."""
    transform = topo.get("transform")
    out = []
    for arc in topo["arcs"]:
        x = y = 0
        points = []
        for dx, dy in arc:
            x += dx
            y += dy
            if transform:
                points.append([
                    x * transform["scale"][0] + transform["translate"][0],
                    y * transform["scale"][1] + transform["translate"][1],
                ])
            else:
                points.append([x, y])
        out.append(points)
    return out


def stitch(indices: list[int], arcs: list) -> list[list[float]]:
    """Join arc references into one ring. A negative index means reversed."""
    ring: list[list[float]] = []
    for i in indices:
        segment = arcs[~i][::-1] if i < 0 else arcs[i]
        if ring:
            segment = segment[1:]  # the shared endpoint is already there
        ring.extend(segment)

    deduped: list[list[float]] = []
    for point in ring:
        rounded = [round(point[0], DP), round(point[1], DP)]
        if not deduped or rounded != deduped[-1]:
            deduped.append(rounded)
    return deduped if len(deduped) >= 4 else ring



def _lerp_y(a: list[float], b: list[float], x: float) -> float:
    if b[0] == a[0]:
        return a[1]
    return a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0])


def _clip_side(ring: list[list[float]], bound: float, keep_above: bool) -> list[list[float]]:
    """Sutherland-Hodgman clip of a ring against one vertical line."""
    inside = (lambda p: p[0] >= bound) if keep_above else (lambda p: p[0] <= bound)
    out: list[list[float]] = []
    for i in range(len(ring)):
        current, previous = ring[i], ring[i - 1]
        c_in, p_in = inside(current), inside(previous)
        if c_in:
            if not p_in:
                out.append([bound, _lerp_y(previous, current, bound)])
            out.append(current)
        elif p_in:
            out.append([bound, _lerp_y(previous, current, bound)])
    return out


def _clip_lon(ring: list[list[float]]) -> list[list[float]] | None:
    clipped = _clip_side(ring, -180.0, True)
    if len(clipped) < 3:
        return None
    clipped = _clip_side(clipped, 180.0, False)
    return clipped if len(clipped) >= 4 else None


def split_antimeridian(ring: list[list[float]]) -> list[list[list[float]]]:
    """Split a ring that wraps past +/-180 into pieces inside the map.

    Natural Earth stores Russia, Alaska, Fiji, Kiribati and Antarctica as rings
    whose longitudes jump from +180 to -180. Drawn on a flat map that jump
    becomes a horizontal streak clean across the world. Unwrapping the ring
    into continuous longitudes and then clipping copies of it at each meridian
    turns each wrap into a clean edge at the map border instead.
    """
    unwrapped = [list(ring[0])]
    offset = 0.0
    crossed = False
    for previous, current in zip(ring, ring[1:]):
        delta = current[0] - previous[0]
        if delta > 180:
            offset -= 360
            crossed = True
        elif delta < -180:
            offset += 360
            crossed = True
        unwrapped.append([current[0] + offset, current[1]])

    longitudes = [p[0] for p in unwrapped]
    if not crossed and -180 <= min(longitudes) and max(longitudes) <= 180:
        return [ring]

    pieces = []
    for shift in (-360.0, 0.0, 360.0):
        shifted = [[p[0] + shift, p[1]] for p in unwrapped]
        if max(p[0] for p in shifted) < -180 or min(p[0] for p in shifted) > 180:
            continue
        piece = _clip_lon(shifted)
        if piece:
            pieces.append(piece)
    return pieces or [ring]


def _lon_span(ring: list[list[float]]) -> tuple[float, float]:
    xs = [p[0] for p in ring]
    return min(xs), max(xs)


def rebuild_polygon(rings: list[list[list[float]]]) -> list[list[list[list[float]]]]:
    """Split one polygon's outer ring, keeping each hole with its piece."""
    outer, holes = rings[0], rings[1:]
    pieces = split_antimeridian(outer)
    if len(pieces) == 1:
        return [[pieces[0], *holes]]

    polygons = []
    for piece in pieces:
        lo, hi = _lon_span(piece)
        mine = [h for h in holes if lo <= sum(x for x, _ in h) / len(h) <= hi]
        polygons.append([piece, *mine])
    return polygons


def main() -> None:
    anchors = [
        (float(p["longitude"]), float(p["latitude"]))
        for p in yaml.safe_load(PORTS_FILE.read_text(encoding="utf-8"))["ports"]
    ]
    print(f"{len(anchors)} ports will keep full coastline detail")

    def near_a_port(point: list[float]) -> bool:
        return any(
            abs(point[0] - lon) < NEAR_DEG and abs(point[1] - lat) < NEAR_DEG
            for lon, lat in anchors
        )

    def reduce_ring(ring: list[list[float]]) -> list[list[float]]:
        if len(ring) <= 8:
            return ring
        kept = [ring[0]]
        for point in ring[1:-1]:
            if near_a_port(point):
                kept.append(point)
                continue
            last = kept[-1]
            if abs(point[0] - last[0]) >= MIN_STEP or abs(point[1] - last[1]) >= MIN_STEP:
                kept.append(point)
        kept.append(ring[-1])
        return kept if len(kept) >= 4 else ring

    print(f"fetching {SOURCE}")
    with urllib.request.urlopen(SOURCE, timeout=120) as response:
        topo = json.load(response)

    arcs = decode_arcs(topo)
    collection = topo["objects"].get("countries") or next(iter(topo["objects"].values()))

    features = []
    for geometry in collection["geometries"]:
        kind = geometry.get("type")
        if kind == "Polygon":
            raw = [[reduce_ring(stitch(r, arcs)) for r in geometry["arcs"]]]
        elif kind == "MultiPolygon":
            raw = [
                [reduce_ring(stitch(r, arcs)) for r in polygon] for polygon in geometry["arcs"]
            ]
        else:
            continue

        polygons: list = []
        for rings in raw:
            polygons.extend(rebuild_polygon(rings))
        if not polygons:
            continue

        features.append({
            "type": "Feature",
            "properties": {},  # country names are unused; the map labels ports
            "geometry": {"type": "MultiPolygon", "coordinates": polygons},
        })

    blob = json.dumps(
        {"type": "FeatureCollection", "features": features}, separators=(",", ":")
    ).encode()
    OUTPUT.write_bytes(blob)

    print(
        f"wrote {OUTPUT.relative_to(REPO_ROOT)}: {len(features)} features, "
        f"{len(blob) / 1048576:.2f} MB raw, {len(gzip.compress(blob)) / 1048576:.2f} MB gzipped"
    )


if __name__ == "__main__":
    main()
