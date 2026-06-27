# Multiroom Climate — Specification

Status: **v1 design locked, build in progress.** This is the source of truth for what we're building.

## 1. Problem

A Daikin Skyport thermostat (`climate.daikin`) regulates to its **own** sensor, which runs warm
relative to the rest of the house. The owner manually sets ~67°F to hold the house average ~70°F.
The thermostat has no native multi-room / remote-sensor averaging. We want a Home Assistant
integration that targets a **house average** and auto-discovers/compensates that bias.

## 2. Architecture

- **v1 = Option W ("thermostat over climate")** — a custom integration that wraps an existing
  `climate.*` entity (the Daikin) plus chosen room sensors and runs our control engine. Proven,
  publishable pattern (same as Versatile Thermostat). Documents a runtime dependency on the
  underlying climate integration. **This is what we build first.**
- **v2 = Option D (direct Skyport API)** — fold in a **clean-room** Skyport API client so the
  integration is self-contained and publishable, dropping the dependency. Clean-room is **mandatory**:
  the reference community integration ships **no LICENSE** (all-rights-reserved) → we may reimplement
  only from documented API fields, never copy its source. Adds token-refresh/reauth handling.

The control engine is a **pure `controller.py` `decide(inputs, config) -> Action`** with zero HA
calls, so it's fully unit-testable and transport-agnostic — the same logic serves W (write to
`climate.set_temperature`) and D (write to the Skyport API).

## 3. v1 scope (locked)

- **House-average** targeting (`current_temperature` = average of chosen sensors).
- **Auto-learned thermostat-bias offsets** (the "67 to hold 70" fix), continuously updated — separate
  cooling and heating offsets, each learned and applied for its own mode.
- **Feedforward jump** on any change + **proportional** step when far + **slow trim** when close.
- **Automatic heat/cool changeover** via band-shift in AUTO (equipment owns compressor protection).
- **Day/night temperature setback** — whole-house target temp switches day↔night. *No per-room
  target switching in v1* (a single night temperature for the whole house).
- **Fixed optimal-start lead** (~45 min; learned lead is a later enhancement).
- **Humidity bias (active, within v1's means)** — in cooling season, when RH is above target,
  overcool by a bounded amount to wring out moisture. *True dehumidify-demand control needs the API
  → v2.* Confirm at build time whether the wrapped `climate.*` exposes any usable humidity control.
- **Fan-circulate** — continuous fan when `max(room) - min(room)` exceeds a threshold; back to auto
  when rooms re-converge.
- **Stale-sensor failsafe** — if target sensors are unavailable/stale, freeze the setpoint + notify;
  never drive HVAC off a bad reading.
- **Master enable toggle** — OFF stops all writes and hands the thermostat back to manual control,
  immediately and cleanly (the hardware/escape-hatch kill switch).
- Full **config + options UI** (working out of the box after just: underlying climate entity + at
  least one target sensor).

Explicitly deferred: per-room/per-window target switching, learned optimal-start, direct
dehumidify-demand control, away/vacation modes (revisit post-v1).

## 4. Control algorithm

Daikin stays in **AUTO** (low=heat setpoint, high=cool setpoint). We slide the band; the thermostat
decides heat vs cool.

1. **Learned offsets (per regime):** `K = band_center - house_average`, slow EMA, updated **only when
   settled** (`|error| <= deadband`, so the house is never sampled mid-recovery). Learned relative to
   the band we actuate (not the thermostat's own sensor) — a signed number that absorbs both the sensor
   bias and the band-center-vs-regulation gap, needing no extra sensor. **Two are kept — `cool_offset`
   and `heat_offset`** — because the equipment regulates to a different band edge in each mode
   (`band_high` cooling, `band_low` heating), so the offset differs by roughly the band gap; each is
   updated only for the regime the equipment is actually running (`hvac_action`, with an `hvac_mode`
   fallback), so a season of cooling can't drag the heating calibration.
2. **Feedforward jump on change** (target change **or a heat↔cool changeover**): jump the band so
   `band_center = target + K`, bypassing the deadband + rate-limit gates, where `K` is the active
   *demand* regime's offset (selected by a sticky deadband-margin hysteresis on the error). Jumping on
   the regime flip — not only on a target change — keeps cooling responsive at a changeover (the band
   moves to the cooling offset immediately instead of crawling there via trim). (Pure `decide()`
   returns the updated offset + its regime; the caller persists them.)
3. **Band-shift trim (hold):** each tick `error = target - house_average`; if `|error| <= deadband`
   do nothing; else `step = clamp(Kp * error, -MAXSTEP, +MAXSTEP)`, and shift **both** band setpoints
   by `step` (clamped to equipment min/max, preserving the thermostat's min heat/cool gap).
4. **Two-speed cadence:** far from target → evaluate every ~2–3 min with larger steps; near target →
   slow ~12-min / 0.5°F trim. Modulating equipment ⇒ no short-cycle risk.
5. **Optimal start:** begin scheduled moves early by a fixed lead so the house is at temp *by* the
   scheduled time.
6. **Humidity bias:** while cooling and RH > target, subtract a bounded humidity term (`gain × excess`,
   capped) from the **effective target** — i.e. lower the *whole* regulation point a touch, not just the
   cool setpoint, so the band-shift loop holds the house slightly below target until the air dries; release
   as RH falls. **Gated on the HVAC mode being cooling-capable (COOL/HEAT_COOL), not on
   `house_average ≥ target`** — a temperature gate is self-defeating (overcooling lowers the house, which
   flips the gate off, which snaps back). Feedforward stays keyed on the *nominal* target so a target change
   doesn't re-jump on humidity swings; trim absorbs the overcool. `K` is target-independent, so learning at
   the overcooled steady state is fine.
7. **Fan-circulate:** stratification > threshold → fan `on`; < lower threshold → fan `auto`.
8. **Failsafe / OFF:** stale target → freeze + notify. Master enable OFF → stop writing entirely.

Defaults (tunable in advanced options): deadband 0.5°F, MAXSTEP/trim 0.5°F, min period 12 min,
Kp TBD during live tuning, optimal-start lead 45 min.

## 5. Component shape

```
custom_components/multiroom_climate/
  __init__.py        # async_setup_entry → coordinator; forward platforms; clean unload
  manifest.json      # domain, name, version, documentation, issue_tracker, codeowners, config_flow, iot_class
  const.py
  config_flow.py     # ConfigFlow + OptionsFlow (v2: + reauth)
  coordinator.py     # DataUpdateCoordinator: read wrapped climate + sensors each tick
  climate.py         # ClimateEntity: AUTO/HEAT/COOL/OFF, current_temperature = house avg, day/night presets
  controller.py      # PURE decide(inputs, config) -> Action  (no HA calls) — the unit-test core
  strings.json + translations/en.json
tests/               # pytest-homeassistant-custom-component; test_controller.py is pure (no hass)
hacs.json · LICENSE (MIT) · README.md · brand/ icons (coined mark, not Daikin's)
.github/workflows/   # hassfest + HACS validate
```

## 6. Build order (small, reviewable PRs — Google small-CL discipline)

Each PR is single-purpose, reviewed with `/simplify`, issues fixed, then merged.

1. ✅ Integration skeleton (manifest, `__init__`, const, CI) — *makes hassfest/HACS-validate run.* (#1)
2. ✅ **`controller.py` + `test_controller.py`** — the pure engine (offset learn, band-shift, trim,
   feedforward, failsafe). Highest value, fully unit-testable, no HA needed. (#3, #4)
3. ✅ `config_flow.py` + strings/translations. (`OptionsFlow` deferred to a later PR.) (#5)
4. ✅ `coordinator.py` (read sensors → house average + mirror wrapped HVAC mode). (#6)
5. `climate.py` — landed as small CLs to keep actuation honest (no confusing no-op half-states):
   - 5a. ✅ Read-only observe layer: `current_temperature` = house avg, mirror wrapped HVAC mode,
     no writes / no setpoint features. (#6)
   - 5b. ✅ Coordinator reads the wrapped AUTO band (`target_temp_low`/`high`); entity exposes it as
     diagnostic attributes. Still read-only. (#7)
   - 5c. ✅ Shadow mode — coordinator runs `controller.decide()` each tick against the live inputs
     (stateful: learned offset, last target/change, all in-memory) and exposes the *proposed*
     band + learned offset + target as `shadow_*` attributes. Writes nothing. (#8)
   - 5d. Actuate — decomposed into small CLs so the first PR that *writes to the real thermostat*
     is minimal and all its scaffolding is reviewed first:
     - 5d-1. ✅ Source the controller's safety bounds from the wrapped entity's `min_temp`/`max_temp`
       (system-unit, any equipment); gate `decide()` on bounds present; delete the °C-default
       conversion scaffolding. Still no writes. (#9)
     - 5d-2. ✅ Availability + failsafe — staleness policy: ≥1 fresh sensor regulates off survivors
       (exposes `fresh/total`); 0 fresh + already-regulating routes `decide(available=False)` →
       failsafe freeze + a *would-notify* message (surfaced as an attribute, not yet delivered);
       before the first reading it waits. Entity availability now follows the thermostat being
       reachable (not sensor freshness) so the failsafe/status stays visible. Still no writes. (#10)
     - 5d-3. ✅ Durable persistence — coordinator-held control state (learned offset, target,
       last target/change) saved to a `helpers.storage.Store` (debounced) and restored before the
       first refresh, so the slow-EMA bias survives restarts. Restoring the target also avoids a
       restart re-seed; persisting `last_target` keeps 5d-4's feedforward gate sound. (#11)
     - 5d-4a. ✅ The flip — real `climate.set_temperature` actuation behind a master **kill switch**
       (separate `switch` entity, `RestoreEntity`, **default off**). Writes only when
       `enabled AND proposed.set_band`; `last_change_ts` advances only on a *successful* write; a
       failed write is logged and swallowed. Enabling re-seeds the target ("hold where we are now");
       the wrapped band is the actuation interface, so our entity stays `supported_features = 0`. (#12)
     - 5d-4b. ✅ User-settable target — the entity advertises a single `TARGET_TEMPERATURE`
       (`HEAT_COOL` + single setpoint: attribute-level verified in-harness — no feature/mode-mismatch
       warning; frontend single-dial rendering is a feature-flag behavior not exercised by the
       backend harness); `async_set_temperature` hands it to `coordinator.async_set_target`, which
       feedforward-jumps the band. A user-set target is flagged + persisted so re-enable keeps it
       (auto-seeded targets still re-seed to "now"). (#13)
6. Humidity bias + fan-circulate layers:
   - 6a. ✅ Pure overcool logic in `decide()` — effective-target shift (`gain × RH-excess`, capped),
     mode-gated cooling flag wired live (`humidity=None` until 6b), feedforward stays on the nominal
     target. Unit-tested; no behavior change yet (RH is None).
   - 6b. ✅ Humidity sensor in the config flow (optional, single RH sensor) + RH read wired into the
     coordinator tick (stale/absent → `None` → overcool off; no humidity failsafe). Surfaced as a
     `shadow_humidity` attribute for observability. (#16)
   - 6c. Fan-circulate (continuous fan when room spread exceeds threshold) — shadow → actuate, like 5c→5d:
     - 6c-1. ✅ Pure `decide_fan(spread, circulating, config)` + spread computed in the coordinator and
       surfaced as `shadow_spread` / `shadow_fan_status` / `shadow_proposed_fan`. Spread-only (NOT
       gated on HVAC mode — stratification builds when idle); two-threshold hysteresis is the only
       anti-thrash; `spread=None` (<2 fresh sensors) holds. No fan write yet.
     - 6c-2. ✅ The `set_fan_mode` write behind the master enable switch (#18). Single `fan_mode_for`
       bool→string map; only manages the on/auto pair — a manual speed (low/medium/…) or unreadable
       mode is left untouched, and the target must be in the equipment's `fan_modes`. Blocked-but-
       wanted writes surface a `shadow_fan_blocked` reason (`fan_unmanaged`/`fan_mode_unsupported`) so
       circulation that can't fire is diagnosable during live tuning. No rate limit (hysteresis +
       desired≠current already prevent thrash). **Known v1 limitation:** we can't distinguish a user's
       manual `on` from our own, so a user-set `on` is returned to `auto` once spread drops (a
       provenance flag is v2).
7. Optimal-start + day/night setback:
   - 7a. ✅ Pure `scheduled_target(now_minutes, config)` — day/night setpoint by minutes-since-local-
     midnight, with a fixed optimal-start lead that pulls **both** transitions earlier (the night
     setback begins the lead early too — a deliberate v1 simplification; occupancy-aware start is v2).
     Half-open arc, wraparound-safe. No caller yet (pure-first, like `decide()` in #3/#4). (#19)
   - 7b. ✅ Schedule config (day/night temps + start times + lead) via the integration's first
     `OptionsFlow`. Temps collected/stored in the *system unit* (Fahrenheit default ≈70/64, not a
     nonsensical "21°F"); start times are `TimeSelector` "HH:MM:SS" → minutes via a pure
     `_time_to_minutes`; `_config_from_options` overlays them on `ControllerConfig` (empty options →
     defaults). An `add_update_listener` reloads the entry on options change so the coordinator
     rebuilds. A `schedule_enabled` toggle is stored for 7c's gate. Schedule values stay inert until
     7c calls `scheduled_target` (shadow-before-actuate, like 6a).
   - 7c. ✅ Wired into the coordinator (`_apply_schedule`, run each tick before `decide()`): local
     wall-clock → minutes via `dt_util.now()` (NOT `utcnow()`); on a *change* in `scheduled_target`
     vs the last tick it jumps the target (leaving `last_target` so `decide()` feedforward-jumps the
     band), so a mid-period manual hold — or the re-seed after an enable toggle — survives until the
     next transition. Gated off when no schedule is configured; the resulting write is still gated on
     the kill switch (like the existing seed). `last_scheduled` is **persisted** so a restart spanning
     a transition re-asserts the new setpoint instead of holding the old one for hours. Schedule
     targets are deliberately left auto-seeded (not `target_user_set`) so the re-enable handback
     keeps working. The current setpoint is surfaced as `shadow_scheduled`. **v1 note:** enabling a
     schedule mid-period is inert until that period ends (the first visible change is the next
     transition) — the correct reading of "on a *change*", documented so it's not a surprise.
8. ⬜ Brand assets, README polish, release `v0.1.0` as a custom HACS repo → tune live → submit to HACS
   default store. (v2: direct Skyport API + reauth.)

## 7. Quality bar

Target HA **Bronze** complete + key **Silver** reliability rules (entity-unavailable handling,
log-when-unavailable, config-entry unloading, parallel-updates; reauth lands with v2). hassfest +
HACS-validate green on every PR. `controller.py` carries strong unit coverage.

## 8. Naming / legal

Product mark **"Multiroom Climate"** (domain `multiroom_climate`) — deliberately contains no Daikin
trademark. "Daikin Skyport" appears only nominatively in description/README to convey compatibility.
MIT licensed. README leads with a non-affiliation disclaimer.

## Decision log

- 2026-06-19 — v1 = Option W (wrap `climate.daikin`); Option D (direct API) is v2.
- 2026-06-19 — Name `multiroom-climate`; trademark kept out of the product name, nominative in docs.
- 2026-06-19 — Night mode = whole-house night *temperature* only (no per-room target switching in v1).
- 2026-06-19 — Humidity bias **included in v1** as overcool-when-humid (true dehumidify demand → v2).
- 2026-06-19 — Master enable toggle = single kill switch returning full control to the thermostat.
- 2026-06-19 — MIT license; fixed optimal-start lead for v1 (learned lead later).
- 2026-06-19 — Repo private until v1 works, then public for HACS + home-assistant/brands submission.
- 2026-06-27 — Split the learned offset into separate `cool_offset`/`heat_offset`, selected by HVAC
  regime. The equipment regulates to a different band edge per mode, so one scalar is wrong right after
  a changeover (surfaced live: cooling ≈ −4 °F, heating ≈ −1 °F). Placement uses a sticky demand-regime
  hysteresis (with a regime-flip feedforward for responsiveness); learning attributes by `hvac_action`.
  Old single-offset state migrates into `cool_offset` on load (no store-version bump).
