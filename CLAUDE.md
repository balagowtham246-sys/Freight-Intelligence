# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Constraint-aware vessel, timing and contract decision support for bulk cargo procurement into India's East Coast ports. Built against the SIH problem statement in `Plan/PRB.txt` and the strategy in `Plan/freight-forecasting-platform-plan.md`. FastAPI + SQLAlchemy backend, React/Vite frontend, models fitted on ingested public data.

## Commands

All Python runs through the repo-root venv. On Windows the interpreter is `.venv/Scripts/python.exe`.

```bash
# setup
py -m venv .venv
./.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
cd frontend && npm install && cd ..

# daily pipeline: ingest, then fit
./.venv/Scripts/python.exe scripts/backfill.py --all
./.venv/Scripts/python.exe scripts/run_models.py

# serve
cd backend && ../.venv/Scripts/python.exe -m uvicorn app.main:app --reload   # :8000
cd frontend && npm run dev                                                   # :5173, proxies /api to :8000
```

Tests run from `backend/` (`pytest.ini` sets `pythonpath = .`, so running from the repo root fails to import `app`):

```bash
cd backend && ../.venv/Scripts/python.exe -m pytest              # full suite
cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_leakage.py
cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_timing.py::test_name
```

Useful narrower invocations:

```bash
./.venv/Scripts/python.exe scripts/backfill.py --dry-run                    # source reachability only
./.venv/Scripts/python.exe scripts/backfill.py --adapter paradip --start 2026-06-01
./.venv/Scripts/python.exe scripts/run_models.py --alerts-only
./.venv/Scripts/python.exe scripts/run_models.py --no-backtest              # skip walk-forward
./.venv/Scripts/python.exe scripts/run_models.py --target SYN_CDFI_TABONEO_GUANGZHOU_50K
cd frontend && npm run build                                               # tsc -b && vite build
./.venv/Scripts/python.exe scripts/build_basemap.py                        # regenerate the Fleet Map basemap
```

`build_basemap.py` is the only command here that needs the network and the only
one you will almost never run: its output, `frontend/public/world.geo.json`, is
committed. Re-run it if a port is added to `port_constraints.yaml`, since the
script keeps full coastline detail only around the ports it finds there.

There is no linter or formatter configured.

## Two invariants that constrain most changes

These are enforced in code and asserted by tests. Breaking either silently corrupts every number the platform reports, so treat a change that touches them as needing the corresponding test to still pass — not as a test to update.

**1. No lookahead in the panel.** A feature at date *t* may only use observations with `obs_date <= t`, and every driver is lagged at least one period. `ml/panel.py` enforces this structurally (shift-then-join, never join-then-shift); `tests/test_leakage.py` asserts it. A leak makes a model look excellent while making every backtest figure a lie. Two live traps the panel already handles: monthly Pink Sheet data is stamped at month start but published in arrears (lagged by `MONTHLY_PUBLICATION_LAG_DAYS`), and `ffill` is safe only because the panel is always bounded at an explicit `as_of`.

**2. Synthetic data must be disclosed.** Every ingested row carries `source_name`, `source_url`, `retrieved_at`, `is_synthetic` via `ProvenanceMixin`. `ingest/base.py` raises `DisclosureError` at write time if an `is_synthetic` row's source name is not declared in `SYNTHETIC_DATA.md`; `tests/test_synthetic_disclosure.py` stops the file drifting out of date. Adding a synthetic source means adding its disclosure entry, not disabling `settings.enforce_synthetic_disclosure`.

A corollary worth knowing: model output deliberately does **not** carry `ProvenanceMixin`. A forecast is not an observation, so it cannot inflate the "% real" badge. Do not add the mixin to `models/forecasts.py` tables.

## Architecture

Data flows one way, and each stage is testable without the one before it:

```
ingest/  ->  ml/panel  ->  ml/features  ->  ml/quantile + conformal  ->  opt/  ->  api/
                                         \-> ml/volatility, regime, waiting_model
                                         \-> ml/evaluate (gates everything downstream)
```

**`ingest/`** — one adapter per source behind `SourceAdapter` in `base.py`, registered in `ingest/__init__.py::ADAPTERS`. The base class handles provenance stamping, disclosure enforcement, and graceful degradation (a failed fetch falls back to the last known-good payload on disk and records the run as `stale` rather than taking the platform down). `BACKFILLABLE` marks sources whose history can be walked date by date; `DERIVED_LAST` marks adapters that derive from what is already held and must run after their inputs. Raw PDFs/workbooks are archived byte-for-byte to `backend/data/raw/` so a parser fix can be replayed against history rather than re-scraped.

**`domain/`** — the deterministic half. `loadable_tonnes.py` is ship physics (displacement, TPC, water density) answering how many tonnes actually load and which of four limits bound: draft, deadweight, hold volume, or a hard dimensional rejection. `feasibility.py` screens vessel classes and berths, with lightering/transhipment as a first-class routing rather than an exception. `alerts.py` holds seven risk rules, each a pure function over already-held data. Nothing here is learned or forecast — that is what makes it auditable, and it is why it gates the probabilistic layers rather than the reverse.

**`ml/`** — pure functions over DataFrames, with one exception: `service.py` is the only module that touches the database. There is no pickled-model store by design; fits take seconds, the daily job refits anyway, and a serialised booster is a version-skew hazard. The platform persists *outputs* (forecasts, metrics, alerts). If fits ever become slow, add a store behind `service.fitted_models` and nothing else changes.

**`opt/`** — `scenarios.py` draws correlated futures through a hand-built Gaussian copula, mapping uniforms through the *calibrated* predictive quantiles so scenarios inherit the conformal calibration. `timing.py` solves entry timing as optimal stopping by backward induction on `E[cost] + λ·CVaR₉₅[cost]`. Feasibility screens before any of this runs.

**`sim/`** — fabricated data for display only. Currently one module: `fleet.py`, which moves an invented fleet along hand-plotted sea routes for the Fleet Map screen, using the real port coordinates and vessel particulars. Nothing here is persisted, ingested, or fed to a model, and the package name is the boundary marker. Disclosed in `SYNTHETIC_DATA.md` §7b.

**`api/`** — FastAPI routes in `routes.py`, Pydantic schemas in `schemas.py`. 21 endpoints. `db.py` is the single seam for the SQLite-to-Postgres upgrade.

**`frontend/src/pages/`** — eight screens. `api.ts` is a hand-written typed client; Vite proxies `/api` to `:8000` in dev.

## The "no edge" rule

This is the project's defining behaviour and it is load-bearing across three modules. Dry bulk freight is near a random walk in log space, so the forecaster must earn the right to speak.

`evaluate.py::HorizonMetrics.has_edge` returns true only when **all** of: at least `MIN_FOLDS` walk-forward folds, the median beats every baseline's *point* forecast, and the distribution beats every baseline under a proper score. Requiring point skill is what stops the platform claiming an edge on the strength of merely having drawn an interval.

`timing.py::_signal_strength` forces `no_edge` when that flag is false or the model has never been measured. In that state the engine still returns full feasibility and cost arithmetic — real and useful — but explicitly declines to time the market and falls back to the deadline-driven answer. The UI badges it.

When adding a model or horizon, wire it through `evaluate.py` rather than around it. An unmeasured model is treated as having no edge, not as fine.

Related: the FFA forward curve is exchange-licensed and not held, so `evaluate.py::assert_benchmark_allowed` refuses to fabricate that comparison and the platform reports *benchmark unavailable*.

## Backtesting protocol

Strictly walk-forward with an embargo equal to the forecast horizon — without it the last training label overlaps the first test window and the two share a future, inflating skill exactly where skill is weakest. Never a random split: neighbouring days in a near-unit-root series are almost the same observation. The same reasoning governs the conformal calibration fold in `quantile.py`, which is the most recent *block*, not a sample.

Metrics: pinball loss, CRPS, MASE, empirical 80% coverage, PIT histogram, and skill against random-walk / drift / seasonal-naive baselines from `baselines.py`.

## Reference data

`backend/data/reference/port_constraints.yaml` (14 ports, 27 berths — 8 East Coast Indian plus load ports in Australia, Indonesia, Mozambique, the US) and `vessel_classes.yaml` (8 classes, 5 cargo grades). These are versioned inputs read by `domain/reference.py`, not derived data. Port limits carry a `source` field; keep it populated when editing.

Values the engine takes as explicit inputs rather than inventing: tide height (defaults to zero), bunker price, demurrage rate. `SYNTHETIC_DATA.md` records the disclosed defaults. Do not substitute a plausible-looking figure for a missing one.

## Current data situation

The freight target is the Shanghai Shipping Exchange CDFI. The real history endpoint is subscriber-gated and the free one yields two days, so the forecaster currently trains on a disclosed synthetic development series under `SYN_`-prefixed series codes, generated by `ingest/synthetic_history.py` and documented in `SYNTHETIC_DATA.md`. Real CDFI accumulates daily; once it passes 250 observations the pipeline switches to it automatically and the synthetic series stops being used. The two never mix — `SYN_` codes are distinct series.

`DATA_SOURCES.md` lists every source, live and identified-but-not-wired, with access terms and the reason each unused one is unused.
