"""A simulated fleet of bulk carriers on the routes this platform models.

There is no live AIS feed. AISStream is identified in DATA_SOURCES.md but not
wired, and the free tier is live-only with no historical backfill, so there is
no real position history to draw on either. The Fleet Map screen therefore runs
on this module: a deterministic simulation built from the *real* port
coordinates in ``port_constraints.yaml`` and the *real* vessel particulars in
``vessel_classes.yaml``, moving ships along hand-plotted sea routes.

Three properties are worth knowing before trusting anything this produces.

**It is fabricated.** Every vessel name, IMO and MMSI below is invented. No
figure returned here is an observation. The API stamps the payload
``is_synthetic=True`` and the UI badges it; see SYNTHETIC_DATA.md section 7.

**It is never persisted.** ``fleet_snapshot`` is a pure function of the clock.
Nothing is written to ``ais_positions`` or anywhere else, so these rows cannot
reach the panel, cannot become a feature, and cannot move the "% real" badge,
which counts ingested observations only.

**It is deterministic.** Position depends only on the timestamp passed in, so
two clients polling at the same moment see the same fleet, a reload does not
teleport anything, and there is no state to seed or migrate.

Routes are polylines through real water rather than great circles between
endpoints. A direct great circle from Newcastle to Paradip crosses the
Australian continent, which is immediately visible on a map and would undercut
the point of drawing one.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from ..domain.reference import load_ports_list, load_vessel_classes

EARTH_RADIUS_NM = 3440.065

# Every laden route in scope converges on the southern Bay of Bengal before
# fanning out to a discharge port, so the waypoint tables split there. This
# keeps the hand-plotted geography to 6 outbound legs plus 8 approaches rather
# than the 48 full routes the pairs would otherwise need.
GATEWAY = (6.0, 88.0)

Point = tuple[float, float]

# ------------------------------------------------------------- sea routes

# Load port -> gateway. Waypoints hug real shipping lanes: up the Australian
# east coast and through Torres Strait, out of the Java Sea via Karimata and
# Malacca, down the Mozambique Channel, and round the Cape of Good Hope from
# the Americas.
OUTBOUND: dict[str, list[Point]] = {
    "AUNTL": [
        (-32.925, 151.780), (-30.0, 153.6), (-25.5, 153.9), (-21.0, 153.6),
        (-16.0, 147.8), (-12.0, 144.6), (-10.4, 142.4), (-9.5, 138.0),
        (-9.5, 131.0), (-9.5, 123.0), (-9.2, 118.0), (-8.5, 110.0),
        (-6.5, 100.0), (-2.0, 96.5), (2.0, 94.0), (5.5, 94.5), (6.2, 91.0),
    ],
    "AUHPT": [
        (-21.283, 149.300), (-20.0, 149.8), (-18.0, 147.6), (-16.0, 146.4),
        (-13.0, 144.2), (-10.6, 142.4), (-9.5, 138.0), (-9.5, 131.0),
        (-9.5, 123.0), (-9.2, 118.0), (-8.5, 110.0), (-6.5, 100.0),
        (-2.0, 96.5), (2.0, 94.0), (5.5, 94.5), (6.2, 91.0),
    ],
    "IDTAB": [
        (-3.650, 114.450), (-4.2, 112.0), (-3.0, 108.5), (-1.5, 106.0),
        (0.5, 104.6), (1.2, 103.9), (2.5, 101.5), (4.5, 99.0), (5.8, 96.0),
        (6.4, 92.5), (6.2, 90.0),
    ],
    "MZBEW": [
        (-19.833, 34.850), (-19.9, 36.5), (-18.0, 39.5), (-14.0, 41.5),
        (-11.0, 42.5), (-7.0, 45.0), (-2.0, 50.0), (1.0, 58.0), (0.5, 68.0),
        (0.0, 74.0), (2.5, 81.0),
    ],
    "USORF": [
        (36.865, -76.330), (35.5, -74.0), (30.0, -65.0), (20.0, -50.0),
        (5.0, -32.0), (-10.0, -22.0), (-25.0, -8.0), (-34.5, 12.0),
        (-35.5, 20.0), (-33.0, 30.0), (-30.0, 38.0), (-26.5, 45.5),
        (-20.0, 52.0), (-10.0, 58.0), (-2.0, 68.0), (0.5, 74.0), (2.5, 81.0),
    ],
    "USMSR": [
        (29.950, -90.070), (28.5, -89.5), (26.0, -86.0), (24.5, -83.0),
        (24.0, -79.5), (25.0, -74.0), (26.0, -65.0), (20.0, -50.0),
        (5.0, -32.0), (-10.0, -22.0), (-25.0, -8.0), (-34.5, 12.0),
        (-35.5, 20.0), (-33.0, 30.0), (-30.0, 38.0), (-26.5, 45.5),
        (-20.0, 52.0), (-10.0, 58.0), (-2.0, 68.0), (0.5, 74.0), (2.5, 81.0),
    ],
}

# Gateway -> discharge or transhipment point.
APPROACH: dict[str, list[Point]] = {
    "INPRT": [(12.0, 87.0), (17.0, 86.5), (19.8, 86.9), (20.264, 86.675)],
    "INVTZ": [(11.0, 85.5), (15.0, 83.8), (17.3, 83.5), (17.686, 83.283)],
    "INGGV": [(11.0, 85.5), (15.0, 83.8), (17.2, 83.45), (17.630, 83.235)],
    "INDHR": [(12.0, 87.0), (17.0, 86.8), (20.3, 87.3), (20.783, 86.976)],
    "INGPR": [(12.0, 86.5), (17.0, 85.5), (19.0, 85.2), (19.263, 84.921)],
    "INHAL": [(13.0, 88.0), (18.0, 88.2), (20.6, 88.3), (21.5, 88.15), (22.033, 88.089)],
    "INSAG": [(13.0, 88.0), (18.0, 88.2), (20.6, 88.3), (21.650, 88.050)],
    "INSND": [(13.0, 88.0), (18.0, 88.2), (21.050, 88.300)],
}

# Lightering daughter runs. These never touch the gateway - routing a barge
# 1,200 nm out to sea and back to reach the Hooghly would be nonsense - so the
# composed route is overridden by an explicit short polyline.
DIRECT_ROUTES: dict[tuple[str, str], list[Point]] = {
    ("INSAG", "INHAL"): [(21.650, 88.050), (21.85, 88.07), (22.033, 88.089)],
    ("INSND", "INHAL"): [(21.050, 88.300), (21.5, 88.15), (22.033, 88.089)],
}


# ---------------------------------------------------------------- geodesy


def haversine_nm(a: Point, b: Point) -> float:
    """Great-circle distance in nautical miles."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(h)))


def initial_bearing_deg(a: Point, b: Point) -> float:
    """Initial great-circle bearing from a to b, in degrees true."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def interpolate(a: Point, b: Point, fraction: float) -> Point:
    """Spherical interpolation, so a long leg bows like a real track."""
    if fraction <= 0.0:
        return a
    if fraction >= 1.0:
        return b

    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    delta = haversine_nm(a, b) / EARTH_RADIUS_NM
    if delta < 1e-9:
        return a

    p = math.sin((1 - fraction) * delta) / math.sin(delta)
    q = math.sin(fraction * delta) / math.sin(delta)
    x = p * math.cos(lat1) * math.cos(lon1) + q * math.cos(lat2) * math.cos(lon2)
    y = p * math.cos(lat1) * math.sin(lon1) + q * math.cos(lat2) * math.sin(lon2)
    z = p * math.sin(lat1) + q * math.sin(lat2)
    return (
        math.degrees(math.atan2(z, math.sqrt(x * x + y * y))),
        math.degrees(math.atan2(y, x)),
    )


# ------------------------------------------------------------ route model


@lru_cache(maxsize=64)
def _port_coords() -> dict[str, Point]:
    """Port code -> (lat, lon), read from the real reference YAML."""
    return {p["code"]: (float(p["latitude"]), float(p["longitude"])) for p in load_ports_list()}


@lru_cache(maxsize=64)
def _port_names() -> dict[str, str]:
    return {p["code"]: p["name"] for p in load_ports_list()}


@lru_cache(maxsize=128)
def _route(load_port: str, discharge_port: str) -> tuple[Point, ...]:
    """The sea route between two ports as a waypoint polyline."""
    direct = DIRECT_ROUTES.get((load_port, discharge_port))
    if direct is not None:
        return tuple(direct)
    return tuple(OUTBOUND[load_port] + [GATEWAY] + APPROACH[discharge_port])


@lru_cache(maxsize=128)
def _cumulative_nm(route: tuple[Point, ...]) -> tuple[float, ...]:
    """Distance along the polyline at each waypoint, starting at zero."""
    running = [0.0]
    for previous, current in zip(route, route[1:]):
        running.append(running[-1] + haversine_nm(previous, current))
    return tuple(running)


def _point_at(route: tuple[Point, ...], distance_nm: float) -> tuple[Point, float]:
    """Position and course at a distance along the polyline."""
    marks = _cumulative_nm(route)
    total = marks[-1]
    distance_nm = max(0.0, min(total, distance_nm))

    for index in range(len(route) - 1):
        if distance_nm <= marks[index + 1] or index == len(route) - 2:
            leg = marks[index + 1] - marks[index]
            fraction = 0.0 if leg <= 0 else (distance_nm - marks[index]) / leg
            here = interpolate(route[index], route[index + 1], fraction)
            return here, initial_bearing_deg(here, route[index + 1])

    return route[-1], 0.0


# ------------------------------------------------------------- the roster

# (imo, mmsi, name, vessel_class, load_port, discharge_port, cargo, tonnes,
#  offset_days). The offset staggers each vessel within its own voyage cycle so
# that at any moment the fleet is spread across passage, anchorage and berth
# rather than all arriving together.
VOYAGES: tuple[tuple, ...] = (
    ("9412345", "563184000", "Kalinga Spirit", "Capesize", "IDTAB", "INPRT", "thermal_coal_low_cv", 165000, 0),
    ("9388217", "353902000", "Bay Trader", "Panamax", "AUNTL", "INPRT", "thermal_coal_high_cv", 72000, 9),
    ("9501882", "477553000", "Utkal Pioneer", "Kamsarmax", "IDTAB", "INDHR", "thermal_coal_low_cv", 79000, 4),
    ("9276104", "419001250", "Coromandel Star", "Supramax", "IDTAB", "INHAL", "thermal_coal_low_cv", 44000, 15),
    ("9633471", "538007412", "Newcastle Venture", "Newcastlemax", "AUNTL", "INDHR", "thermal_coal_high_cv", 195000, 22),
    ("9455109", "563220000", "Dalrymple Trader", "Capesize", "AUHPT", "INVTZ", "coking_coal", 170000, 31),
    ("9702338", "477118600", "Godavari Maru", "Ultramax", "IDTAB", "INGPR", "thermal_coal_mid_cv", 61000, 7),
    ("9310556", "353771000", "Beira Endeavour", "Panamax", "MZBEW", "INVTZ", "coking_coal", 75000, 18),
    ("9588240", "538004901", "Chesapeake Bulker", "Capesize", "USORF", "INGGV", "coking_coal", 172000, 40),
    ("9421673", "353615000", "Mississippi Glory", "Kamsarmax", "USMSR", "INPRT", "thermal_coal_mid_cv", 81000, 55),
    ("9667012", "419000870", "Sagar Lifter", "Supramax", "INSAG", "INHAL", "thermal_coal_low_cv", 38000, 1),
    ("9203845", "419000915", "Hooghly Daughter", "Handysize", "INSND", "INHAL", "thermal_coal_low_cv", 28000, 3),
    ("9744029", "563445000", "Paradip Express", "PostPanamax", "AUHPT", "INPRT", "coking_coal", 91000, 12),
    ("9350118", "477880300", "Taboneo Carrier", "Panamax", "IDTAB", "INVTZ", "thermal_coal_low_cv", 74000, 20),
    ("9612907", "538009133", "Vizag Mariner", "Ultramax", "AUNTL", "INGGV", "thermal_coal_high_cv", 62000, 27),
    ("9478365", "353210000", "Odisha Anchor", "Capesize", "AUNTL", "INSND", "thermal_coal_high_cv", 168000, 34),
    ("9522764", "419001640", "Gopalpur Trader", "Supramax", "IDTAB", "INGPR", "thermal_coal_mid_cv", 52000, 11),
    ("9691483", "538011256", "Dhamra Voyager", "Capesize", "MZBEW", "INDHR", "coking_coal", 175000, 46),
    ("9284470", "419002205", "Bengal Runner", "Handysize", "IDTAB", "INHAL", "thermal_coal_low_cv", 31000, 6),
    ("9736518", "563990000", "Sandheads Mother", "Newcastlemax", "AUHPT", "INSAG", "coking_coal", 205000, 25),
)

# Anchored so the cycle position is reproducible across restarts and machines.
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _jitter(imo: str, salt: int, spread: int) -> int:
    """Small deterministic per-vessel variation, seeded from the IMO."""
    return (int(imo) * 7919 + salt * 104729) % spread


# ------------------------------------------------------------- the fleet


def _vessel_state(voyage: tuple, as_of: datetime) -> dict:
    """Where one vessel is, and what it is doing, at a given instant."""
    imo, mmsi, name, klass, load_port, discharge_port, cargo, tonnes, offset = voyage

    particulars = load_vessel_classes()[klass]
    names = _port_names()
    route = _route(load_port, discharge_port)
    total_nm = _cumulative_nm(route)[-1]

    # Service speed varies by a few percent per vessel so the fleet does not
    # move in lockstep, but deterministically: the same ship is always the
    # same speed.
    speed_kn = getattr(particulars, "speed_kn", 14.0) * (0.93 + _jitter(imo, 1, 15) / 100.0)

    passage_h = total_nm / speed_kn
    waiting_h = (2 + _jitter(imo, 2, 8)) * 24.0
    berth_h = (2 + _jitter(imo, 3, 5)) * 24.0
    ballast_h = passage_h * 0.92  # faster in ballast, same water
    cycle_h = passage_h + waiting_h + berth_h + ballast_h

    elapsed_h = (as_of - EPOCH).total_seconds() / 3600.0 + offset * 24.0
    phase_h = elapsed_h % cycle_h

    origin, destination = names.get(load_port, load_port), names.get(discharge_port, discharge_port)

    if phase_h < passage_h:
        # Laden passage.
        travelled = total_nm * (phase_h / passage_h)
        position, course = _point_at(route, travelled)
        remaining = total_nm - travelled
        status = "under way"
        current_speed = speed_kn
        eta = as_of + timedelta(hours=remaining / speed_kn)
        laden = True

    elif phase_h < passage_h + waiting_h:
        # At the anchorage, holding for a berth. Anchored vessels swing, so the
        # position drifts on a slow circle - enough that the map reads as live
        # rather than frozen.
        waited_h = phase_h - passage_h
        anchor_off = 6.0 + _jitter(imo, 4, 12)
        base, _ = _point_at(route, max(0.0, total_nm - anchor_off))
        swing = (waited_h / 6.0) * 2 * math.pi + _jitter(imo, 5, 360) * math.pi / 180.0
        position = (base[0] + 0.02 * math.sin(swing), base[1] + 0.02 * math.cos(swing))
        course = (math.degrees(swing) + 90.0) % 360.0
        remaining = anchor_off
        status = "waiting at anchorage"
        current_speed = 0.3
        eta = as_of + timedelta(hours=waiting_h - waited_h)
        laden = True

    elif phase_h < passage_h + waiting_h + berth_h:
        # Alongside, discharging. Fanned a couple of kilometres around the port
        # point: a port is several berths spread along a waterfront, and
        # stacking every vessel on one coordinate makes them unpickable on the
        # map as well as wrong.
        worked_h = phase_h - passage_h - waiting_h
        fan = _jitter(imo, 6, 360) * math.pi / 180.0
        position = (route[-1][0] + 0.015 * math.sin(fan), route[-1][1] + 0.015 * math.cos(fan))
        course = initial_bearing_deg(route[-2], route[-1])
        remaining = 0.0
        status = "at berth"
        current_speed = 0.0
        eta = as_of + timedelta(hours=berth_h - worked_h)
        laden = True

    else:
        # Ballasting back to load. Same polyline, walked in reverse.
        run_h = phase_h - passage_h - waiting_h - berth_h
        travelled = total_nm * (run_h / ballast_h)
        position, _ = _point_at(route, max(0.0, total_nm - travelled))
        ahead, _ = _point_at(route, max(0.0, total_nm - travelled - 30.0))
        course = initial_bearing_deg(position, ahead)
        remaining = total_nm - travelled
        status = "ballast to load port"
        current_speed = speed_kn * 1.08
        eta = as_of + timedelta(hours=remaining / (speed_kn * 1.08))
        laden = False

    progress = 0.0 if total_nm <= 0 else max(0.0, min(100.0, 100.0 * (1 - remaining / total_nm)))

    return {
        "imo": imo,
        "mmsi": mmsi,
        "name": f"MV {name}",
        "vessel_class": klass,
        "dwt": particulars.dwt,
        "latitude": round(position[0], 4),
        "longitude": round(((position[1] + 180.0) % 360.0) - 180.0, 4),
        "course_deg": round(course, 1),
        "speed_kn": round(current_speed, 1),
        "status": status,
        "laden": laden,
        "source_port": load_port,
        "source_port_name": origin,
        "destination_port": discharge_port,
        "destination_port_name": destination,
        "cargo": cargo,
        "cargo_tonnes": tonnes if laden else 0,
        "eta": eta.replace(microsecond=0).isoformat(),
        "distance_remaining_nm": round(remaining, 1),
        "route_distance_nm": round(total_nm, 1),
        "progress_pct": round(progress, 1),
        "route": [[round(lon, 3), round(lat, 3)] for lat, lon in route],
    }


def fleet_snapshot(as_of: datetime | None = None) -> list[dict]:
    """Every simulated vessel's position and voyage state at one instant.

    Pure: given the same ``as_of`` this returns the same fleet, on any machine
    and after any restart. Coordinates come out as decimal degrees, course as
    degrees true, speed in knots.
    """
    moment = as_of or datetime.now(timezone.utc)
    return [_vessel_state(voyage, moment) for voyage in VOYAGES]
