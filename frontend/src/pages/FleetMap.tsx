/* Fleet Map - simulated vessel positions on the routes this platform models.
 *
 * The world outline in /world.geo.json is built by scripts/build_basemap.py
 * from Natural Earth 1:10m (public domain): full coastline detail around the
 * 14 reference ports, thinned elsewhere. It is fetched as a static asset
 * rather than imported, so tsc never has to infer a type for a multi-megabyte
 * literal and it stays out of the JS bundle.
 *
 * Every figure on this screen is fabricated. See SYNTHETIC_DATA.md section 7. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts";
import ReactECharts from "echarts-for-react";
import { api, type Fleet, type FleetVessel, type Port } from "../api";
import { ProvenanceLine } from "../components/Provenance";

/* A dart pointing north, rotated per vessel to its course. SVG y grows
 * downward, so the bow is at negative y. */
const SHIP = "path://M0,-11 L6.5,8 L0,3.5 L-6.5,8 Z";

const STATUS_COLOR: Record<string, string> = {
  "under way": "#4493f8",
  "waiting at anchorage": "#d29922",
  "at berth": "#3fb950",
  "ballast to load port": "#8b949e",
};

const ROLE_COLOR: Record<string, string> = {
  discharge: "#4493f8",
  load: "#3fb950",
  transhipment: "#a371f7",
};

const REFRESH_MS = 5000;

/* Zoom is relative to "whole world across the panel", so a port approach
 * needs a big number: ~400 puts roughly 100 km across the map, enough to see
 * the berth, the anchorage and the traffic queued off it. The ceiling allows
 * roughly a 30 km view before the coastline - Natural Earth 110m, whose own
 * vertices are tens of kilometres apart - stops being worth magnifying. */
const WORLD_VIEW = { center: [78, 8] as [number, number], zoom: 1.35 };
const PORT_ZOOM = 400;
const MAX_ZOOM = 1200;

function coord(lat: number, lon: number): string {
  const ns = lat >= 0 ? "N" : "S";
  const ew = lon >= 0 ? "E" : "W";
  return `${Math.abs(lat).toFixed(3)}° ${ns}, ${Math.abs(lon).toFixed(3)}° ${ew}`;
}

function eta(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function FleetMap() {
  const [mapReady, setMapReady] = useState(false);
  const [ports, setPorts] = useState<Port[]>([]);
  const [fleet, setFleet] = useState<Fleet | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [destination, setDestination] = useState("all");
  const [live, setLive] = useState(true);
  const [error, setError] = useState<string | null>(null);

  /* Pan and zoom live in a ref, not state: the option object is rebuilt on
   * every poll, and without feeding the user's view back in, the map would
   * snap home to the world view every five seconds mid-inspection. */
  const view = useRef<{ center: [number, number]; zoom: number }>({ ...WORLD_VIEW });
  const chartRef = useRef<any>(null);
  const portsRef = useRef<Port[]>([]);
  portsRef.current = ports;

  /* Move the map without waiting for the next poll to rebuild the option.
   * The ref is updated too, so the rebuild five seconds later agrees with
   * what the user is already looking at instead of yanking them back. */
  const moveTo = useCallback((center: [number, number], zoom: number) => {
    view.current = { center, zoom };
    chartRef.current?.getEchartsInstance?.()?.setOption({ geo: { center, zoom } });
  }, []);

  const focusPort = useCallback(
    (code: string) => {
      if (code === "world") {
        moveTo(WORLD_VIEW.center, WORLD_VIEW.zoom);
        return;
      }
      const port = portsRef.current.find((x) => x.code === code);
      if (port) moveTo([port.longitude, port.latitude], PORT_ZOOM);
    },
    [moveTo],
  );

  useEffect(() => {
    let cancelled = false;
    fetch("/world.geo.json")
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} fetching world.geo.json`);
        return r.json();
      })
      .then((geo) => {
        if (cancelled) return;
        echarts.registerMap("world", geo);
        setMapReady(true);
      })
      .catch((e) => setError(String(e)));
    api.ports().then(setPorts).catch((e) => setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let stopped = false;
    const poll = () =>
      api
        .fleet()
        .then((f) => {
          if (!stopped) {
            setFleet(f);
            setError(null);
          }
        })
        .catch((e) => {
          if (!stopped) setError(String(e));
        });

    poll();
    if (!live) {
      return () => {
        stopped = true;
      };
    }
    const timer = setInterval(poll, REFRESH_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [live]);

  const vessels = fleet?.vessels ?? [];
  const shown = useMemo(
    () =>
      destination === "all"
        ? vessels
        : vessels.filter((v) => v.destination_port === destination),
    [vessels, destination],
  );

  const active = vessels.find((v) => v.imo === selected) ?? null;

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const v of vessels) c[v.status] = (c[v.status] ?? 0) + 1;
    return c;
  }, [vessels]);

  const onEvents = useMemo(
    () => ({
      click: (params: any) => {
        if (params.seriesName === "Vessels" && params.data?.imo) {
          setSelected(params.data.imo as string);
        } else if (params.seriesName === "Ports" && params.data?.code) {
          focusPort(params.data.code as string);
        }
      },
      georoam: (_params: any, chart: any) => {
        const geo = chart?.getOption?.()?.geo?.[0];
        if (geo?.center && typeof geo.zoom === "number") {
          view.current = { center: geo.center, zoom: geo.zoom };
        }
      },
    }),
    [focusPort],
  );

  const option = useMemo(() => {
    if (!mapReady) return null;
    return {
      backgroundColor: "#11161d",
      textStyle: { color: "#8b949e", fontFamily: "Inter, sans-serif" },
      tooltip: {
        trigger: "item",
        backgroundColor: "#0e1116",
        borderColor: "#2a3038",
        textStyle: { color: "#e6edf3", fontSize: 12 },
      },
      geo: {
        map: "world",
        roam: true,
        tooltip: { show: false },
        center: view.current.center,
        zoom: view.current.zoom,
        scaleLimit: { min: 1, max: MAX_ZOOM },
        itemStyle: { areaColor: "#232b36", borderColor: "#3d4753", borderWidth: 0.6 },
        emphasis: {
          itemStyle: { areaColor: "#232b36", borderColor: "#3d4753" },
          label: { show: false },
        },
      },
      series: [
        {
          name: "Routes",
          type: "lines",
          coordinateSystem: "geo",
          polyline: true,
          silent: true,
          lineStyle: { color: "#4493f8", width: 1, opacity: 0.13 },
          data: shown.map((v) => ({ coords: v.route })),
        },
        {
          name: "Selected route",
          type: "lines",
          coordinateSystem: "geo",
          polyline: true,
          silent: true,
          z: 5,
          lineStyle: { color: "#4493f8", width: 1.6, opacity: 0.75 },
          data: active ? [{ coords: active.route }] : [],
        },
        {
          name: "Ports",
          type: "scatter",
          coordinateSystem: "geo",
          symbol: "circle",
          symbolSize: 7,
          z: 8,
          data: ports.map((p) => ({
            name: p.name,
            value: [p.longitude, p.latitude],
            itemStyle: {
              color: ROLE_COLOR[p.role] ?? "#8b949e",
              borderColor: "#0e1116",
              borderWidth: 1,
            },
            role: p.role,
            code: p.code,
          })),
          label: {
            show: true,
            position: "right",
            distance: 6,
            color: "#8b949e",
            fontSize: 10,
            formatter: (p: any) => p.data.name,
          },
          labelLayout: { hideOverlap: true },
          tooltip: {
            formatter: (p: any) =>
              `<b>${p.data.name}</b> (${p.data.code})<br/>${p.data.role} port` +
              `<br/><span style="color:#8b949e">click to zoom in</span>`,
          },
        },
        {
          name: "Vessels",
          type: "scatter",
          coordinateSystem: "geo",
          symbol: SHIP,
          symbolSize: 13,
          z: 12,
          data: shown.map((v) => ({
            name: v.name,
            value: [v.longitude, v.latitude],
            symbolRotate: v.course_deg,
            imo: v.imo,
            itemStyle: {
              color: STATUS_COLOR[v.status] ?? "#8b949e",
              borderColor: v.imo === selected ? "#e6edf3" : "transparent",
              borderWidth: v.imo === selected ? 1.5 : 0,
              opacity: v.imo === selected || !selected ? 1 : 0.62,
            },
            tip:
              `<b>${v.name}</b><br/>${v.status}<br/>` +
              `${v.source_port_name} → ${v.destination_port_name}<br/>` +
              `<span style="color:#8b949e">click for detail</span>`,
          })),
          tooltip: { formatter: (p: any) => p.data.tip },
        },
      ],
    };
  }, [mapReady, shown, ports, active, selected]);

  const closePopup = useCallback(() => setSelected(null), []);

  const dischargePorts = ports.filter((p) => p.role !== "load");

  return (
    <div>
      <div className="page-head">
        <h2>Fleet Map</h2>
        <p>
          Live positions for the bulk carriers on the routes this platform models, load port
          to discharge berth. Tap a vessel for its particulars. Positions are{" "}
          <b>simulated</b>, not observed: no AIS feed is wired, so ships are moved along
          hand-plotted sea routes using the real port coordinates and vessel particulars from
          the reference data. Nothing here is stored or fed to any model.
        </p>
      </div>

      {error && (
        <div className="panel">
          <div className="error">{error}</div>
        </div>
      )}

      <div className="panel">
        <div className="form-row">
          <div className="field">
            <label>Destination</label>
            <select value={destination} onChange={(e) => setDestination(e.target.value)}>
              <option value="all">All ports</option>
              {dischargePorts.map((p) => (
                <option key={p.code} value={p.code}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Zoom to</label>
            <select defaultValue="world" onChange={(e) => focusPort(e.target.value)}>
              <option value="world">Whole world</option>
              <optgroup label="Discharge">
                {ports.filter((x) => x.role === "discharge").map((x) => (
                  <option key={x.code} value={x.code}>{x.name}</option>
                ))}
              </optgroup>
              <optgroup label="Transhipment">
                {ports.filter((x) => x.role === "transhipment").map((x) => (
                  <option key={x.code} value={x.code}>{x.name}</option>
                ))}
              </optgroup>
              <optgroup label="Load">
                {ports.filter((x) => x.role === "load").map((x) => (
                  <option key={x.code} value={x.code}>{x.name}</option>
                ))}
              </optgroup>
            </select>
          </div>
          <div className="field">
            <label>Tracking</label>
            <button
              className="primary"
              onClick={() => setLive((v) => !v)}
              style={live ? undefined : { background: "var(--muted)" }}
            >
              {live ? "Live · pause" : "Paused · resume"}
            </button>
          </div>
          <div className="field">
            <label>Last fix</label>
            <div style={{ fontSize: 13, paddingTop: 6 }}>
              {fleet ? new Date(fleet.as_of).toLocaleTimeString() : "—"}
            </div>
          </div>
        </div>
      </div>

      <div className="grid grid-4" style={{ marginBottom: 16 }}>
        <div className="stat">
          <div className="label">Under way</div>
          <div className="value" style={{ color: "var(--accent)" }}>
            {counts["under way"] ?? 0}
          </div>
          <div className="sub">laden passage</div>
        </div>
        <div className="stat">
          <div className="label">At anchorage</div>
          <div className="value" style={{ color: "var(--warn)" }}>
            {counts["waiting at anchorage"] ?? 0}
          </div>
          <div className="sub">waiting for a berth</div>
        </div>
        <div className="stat">
          <div className="label">At berth</div>
          <div className="value" style={{ color: "var(--good)" }}>
            {counts["at berth"] ?? 0}
          </div>
          <div className="sub">discharging</div>
        </div>
        <div className="stat">
          <div className="label">In ballast</div>
          <div className="value" style={{ color: "var(--muted)" }}>
            {counts["ballast to load port"] ?? 0}
          </div>
          <div className="sub">returning to load</div>
        </div>
      </div>

      <div className="panel">
        <h3>Vessel positions &mdash; {shown.length} tracked</h3>
        <div className="fleet-map-wrap">
          {option ? (
            <ReactECharts
              ref={chartRef}
              option={option}
              style={{ height: 540 }}
              onEvents={onEvents}
              notMerge={false}
              lazyUpdate
            />
          ) : (
            <div className="loading">Loading chart&hellip;</div>
          )}

          {active && (
            <div className="fleet-popup">
              <div className="fleet-popup-head">
                <span
                  className="fleet-dot"
                  style={{ background: STATUS_COLOR[active.status] ?? "#8b949e" }}
                />
                <span className="fleet-popup-title">{active.name}</span>
                <button className="fleet-popup-close" onClick={closePopup} aria-label="Close">
                  &times;
                </button>
              </div>
              <dl className="fleet-popup-body">
                <dt>IMO</dt>
                <dd>{active.imo}</dd>
                <dt>MMSI</dt>
                <dd>{active.mmsi}</dd>
                <dt>Position</dt>
                <dd>{coord(active.latitude, active.longitude)}</dd>
                <dt>Status</dt>
                <dd>{active.status}</dd>
                <dt>Source port</dt>
                <dd>
                  {active.source_port_name} ({active.source_port})
                </dd>
                <dt>Destination</dt>
                <dd>
                  {active.destination_port_name} ({active.destination_port})
                </dd>
                <dt>Class</dt>
                <dd>
                  {active.vessel_class} &middot; {active.dwt.toLocaleString()} dwt
                </dd>
                <dt>Cargo</dt>
                <dd>
                  {active.laden
                    ? `${active.cargo.replace(/_/g, " ")} · ${active.cargo_tonnes.toLocaleString()} t`
                    : "in ballast"}
                </dd>
                <dt>Speed</dt>
                <dd>
                  {active.speed_kn.toFixed(1)} kn &middot; course {active.course_deg.toFixed(0)}
                  &deg;
                </dd>
                <dt>Remaining</dt>
                <dd>{active.distance_remaining_nm.toLocaleString()} nm</dd>
                <dt>ETA</dt>
                <dd>{eta(active.eta)}</dd>
              </dl>
              <div className="fleet-progress">
                <div
                  className="fleet-progress-bar"
                  style={{ width: `${active.progress_pct}%` }}
                />
              </div>
              <div className="fleet-popup-foot">
                {active.progress_pct.toFixed(0)}% of{" "}
                {active.route_distance_nm.toLocaleString()} nm
                <span className="badge badge-synthetic" style={{ marginLeft: 8 }}>
                  simulated
                </span>
              </div>
            </div>
          )}
        </div>

        <div className="fleet-legend">
          {Object.entries(STATUS_COLOR).map(([status, colour]) => (
            <span key={status} className="fleet-legend-item">
              <span className="fleet-dot" style={{ background: colour }} />
              {status}
            </span>
          ))}
          <span className="fleet-legend-sep" />
          {Object.entries(ROLE_COLOR).map(([role, colour]) => (
            <span key={role} className="fleet-legend-item">
              <span className="fleet-dot fleet-dot-port" style={{ background: colour }} />
              {role} port
            </span>
          ))}
        </div>

        {fleet && <ProvenanceLine p={fleet.provenance} />}
      </div>

      <div className="panel">
        <h3>Fleet</h3>
        <div className="table-scroll">
          <table className="fleet-table">
            <thead>
              <tr>
                <th>Vessel</th>
                <th>IMO</th>
                <th>Class</th>
                <th>Status</th>
                <th>From</th>
                <th>To</th>
                <th>Position</th>
                <th className="num">Speed (kn)</th>
                <th className="num">Remaining (nm)</th>
                <th className="num">Progress</th>
                <th>ETA</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((v: FleetVessel) => (
                <tr
                  key={v.imo}
                  onClick={() => setSelected(v.imo)}
                  className={v.imo === selected ? "fleet-row-selected" : undefined}
                  style={{ cursor: "pointer" }}
                >
                  <td>{v.name}</td>
                  <td className="muted">{v.imo}</td>
                  <td className="muted">{v.vessel_class}</td>
                  <td>
                    <span
                      className="fleet-dot"
                      style={{ background: STATUS_COLOR[v.status] ?? "#8b949e" }}
                    />
                    {v.status}
                  </td>
                  <td className="muted">{v.source_port_name}</td>
                  <td>{v.destination_port_name}</td>
                  <td className="muted">{coord(v.latitude, v.longitude)}</td>
                  <td className="num">{v.speed_kn.toFixed(1)}</td>
                  <td className="num">{v.distance_remaining_nm.toLocaleString()}</td>
                  <td className="num">{v.progress_pct.toFixed(0)}%</td>
                  <td className="muted">{eta(v.eta)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {fleet && <ProvenanceLine p={fleet.provenance} />}
      </div>
    </div>
  );
}
