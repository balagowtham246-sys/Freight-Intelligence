"""Tests for the simulated fleet behind the Fleet Map screen.

Two things are being guarded here. The first is that the simulation is
coherent: ships point where they are going, stay on their route, and only ever
run between ports that actually exist in the reference data. The second, and
the more important one, is containment - this module is fabricated data, and
the tests assert that it stays display-only and cannot leak into anything the
platform reports as real.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.reference import load_ports_list, load_vessel_classes
from app.sim.fleet import (
    APPROACH,
    DIRECT_ROUTES,
    OUTBOUND,
    VOYAGES,
    fleet_snapshot,
    haversine_nm,
    initial_bearing_deg,
)

FIXED = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------- containment


def test_simulation_is_not_persisted():
    """The module must not import a session, a model or the database.

    This is the whole containment argument: positions are computed per request
    and thrown away, so they cannot reach the panel or the "% real" badge.
    """
    import app.sim.fleet as module

    source = module.__doc__ or ""
    assert "never persisted" in source.lower() or "not persisted" in source.lower()

    import inspect

    text = inspect.getsource(module)
    for forbidden in ("Session", "get_session", "session.add", "from ..models", "from ..db"):
        assert forbidden not in text, f"fleet simulation must not touch the database ({forbidden})"


def test_api_declares_the_fleet_as_synthetic():
    """The endpoint must own up to being fabricated."""
    from app.api.routes import get_fleet_positions

    payload = get_fleet_positions()
    assert payload.provenance.is_synthetic is True
    assert payload.provenance.source_name == "simulated_fleet_ais"


# ---------------------------------------------------------------- coherence


def test_snapshot_is_deterministic():
    assert fleet_snapshot(FIXED) == fleet_snapshot(FIXED)


def test_every_vessel_has_a_position_and_an_identity():
    fleet = fleet_snapshot(FIXED)
    assert len(fleet) == len(VOYAGES)
    assert len({v["imo"] for v in fleet}) == len(fleet), "IMO numbers must be unique"
    assert len({v["mmsi"] for v in fleet}) == len(fleet), "MMSI numbers must be unique"

    for v in fleet:
        assert -90 <= v["latitude"] <= 90
        assert -180 <= v["longitude"] <= 180
        assert v["speed_kn"] >= 0
        assert 0 <= v["course_deg"] <= 360
        assert 0 <= v["progress_pct"] <= 100


def test_reported_course_matches_the_direction_actually_travelled():
    """A hull pointing the wrong way down its own track is the obvious bug."""
    before = {v["imo"]: v for v in fleet_snapshot(FIXED)}
    after = {v["imo"]: v for v in fleet_snapshot(FIXED + timedelta(minutes=20))}

    checked = 0
    for imo, v in before.items():
        if v["status"] in ("at berth", "waiting at anchorage"):
            continue  # stationary or swinging at anchor; course is not a track
        w = after[imo]
        travelled = initial_bearing_deg(
            (v["latitude"], v["longitude"]), (w["latitude"], w["longitude"])
        )
        error = abs((travelled - v["course_deg"] + 180) % 360 - 180)
        assert error < 15, f"{v['name']} reports {v['course_deg']} but travels {travelled}"
        checked += 1

    assert checked, "no moving vessels in the sample; the cycle logic is wrong"


def test_moving_vessels_advance_and_berthed_ones_do_not():
    before = {v["imo"]: v for v in fleet_snapshot(FIXED)}
    after = {v["imo"]: v for v in fleet_snapshot(FIXED + timedelta(hours=1))}

    moved = [
        imo
        for imo, v in before.items()
        if (v["latitude"], v["longitude"]) != (after[imo]["latitude"], after[imo]["longitude"])
    ]
    assert len(moved) >= len(before) // 2, "most of the fleet should be under way at any time"

    for imo, v in before.items():
        if v["status"] == "at berth" and after[imo]["status"] == "at berth":
            assert v["speed_kn"] == 0.0


def test_every_vessel_reaches_every_phase_over_a_full_cycle():
    """The cycle must actually cycle, not park the fleet in one state."""
    seen: dict[str, set[str]] = {v["imo"]: set() for v in fleet_snapshot(FIXED)}
    for day in range(0, 240, 2):
        for v in fleet_snapshot(FIXED + timedelta(days=day)):
            seen[v["imo"]].add(v["status"])

    expected = {"under way", "waiting at anchorage", "at berth", "ballast to load port"}
    for imo, states in seen.items():
        assert states == expected, f"{imo} only ever reaches {sorted(states)}"


# ---------------------------------------------------------------- geography


def test_routes_only_run_between_ports_that_exist():
    codes = {p["code"] for p in load_ports_list()}
    classes = set(load_vessel_classes())

    for imo, _mmsi, name, klass, load_port, discharge_port, _cargo, _t, _o in VOYAGES:
        assert load_port in codes, f"{name} loads at unknown port {load_port}"
        assert discharge_port in codes, f"{name} discharges at unknown port {discharge_port}"
        assert klass in classes, f"{name} is an unknown class {klass}"
        assert (load_port in OUTBOUND and discharge_port in APPROACH) or (
            (load_port, discharge_port) in DIRECT_ROUTES
        ), f"{name} has no plotted route {load_port} -> {discharge_port}"


def test_route_endpoints_sit_on_their_real_port_coordinates():
    coords = {
        p["code"]: (float(p["latitude"]), float(p["longitude"])) for p in load_ports_list()
    }
    for _imo, _mmsi, name, _k, load_port, discharge_port, _c, _t, _o in VOYAGES:
        route = _route_for(load_port, discharge_port)
        assert haversine_nm(route[0], coords[load_port]) < 1.0, f"{name} does not start at its load port"
        assert (
            haversine_nm(route[-1], coords[discharge_port]) < 1.0
        ), f"{name} does not end at its discharge port"


def test_route_legs_are_short_enough_to_follow_the_sea():
    """Long legs are how a route ends up crossing a continent.

    The waypoints exist precisely so no single leg is a naive great circle
    between distant ports. Capping leg length keeps that property enforced as
    routes get edited.
    """
    for _imo, _mmsi, name, _k, load_port, discharge_port, _c, _t, _o in VOYAGES:
        route = _route_for(load_port, discharge_port)
        for a, b in zip(route, route[1:]):
            leg = haversine_nm(a, b)
            assert leg < 2200, f"{name} has a {leg:.0f} nm leg from {a} to {b}"


def _route_for(load_port: str, discharge_port: str):
    from app.sim.fleet import _route

    return _route(load_port, discharge_port)
