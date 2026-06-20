# Multiroom Climate — Specification

Status: **v1 design locked, build in progress.** This is the source of truth for what we're building.

## 1. Problem

A Daikin Skyport thermostat (`climate.daikin`) regulates to its **own** sensor, which runs warm
relative to the rest of the house. The owner manually sets ~67°F to hold the house average ~70°F.
The thermostat has no native multi-room / remote-sensor averaging. We want a Home Assistant
integration that targets a **weighted house average** and auto-discovers/compensates that bias.

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

- Weighted **house-average** targeting (`current_temperature` = weighted avg of chosen sensors).
- **Auto-learned thermostat-bias offset** (the "67 to hold 70" fix), continuously updated.
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

1. **Learned offset:** `K = band_center - house_average`, slow EMA, updated **only when settled**
   (`|error| <= deadband`, so the house is never sampled mid-recovery). Learned relative to the band
   we actuate (not the thermostat's own sensor) — one signed number that absorbs both the sensor
   bias and the band-center-vs-regulation gap, needing no extra sensor.
2. **Feedforward jump on change** (target change or day↔night transition): jump the band so
   `band_center = target + K`, bypassing the deadband + rate-limit gates. Signed `K` handles both
   heating and cooling. (Pure `decide()` returns the updated `K`; the caller persists it.)
3. **Band-shift trim (hold):** each tick `error = target - house_average`; if `|error| <= deadband`
   do nothing; else `step = clamp(Kp * error, -MAXSTEP, +MAXSTEP)`, and shift **both** band setpoints
   by `step` (clamped to equipment min/max, preserving the thermostat's min heat/cool gap).
4. **Two-speed cadence:** far from target → evaluate every ~2–3 min with larger steps; near target →
   slow ~12-min / 0.5°F trim. Modulating equipment ⇒ no short-cycle risk.
5. **Optimal start:** begin scheduled moves early by a fixed lead so the house is at temp *by* the
   scheduled time.
6. **Humidity bias:** in cooling season, while RH > target, subtract a bounded humidity term from the
   cool target (overcool); release as RH falls.
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
4. ✅ `coordinator.py` (read sensors → weighted house average + mirror wrapped HVAC mode). (#6)
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
     - 5d-4. ⬜ The flip — settable target (resolve single-vs-range HA modeling, verify against
       Versatile Thermostat's `over_climate`) + real `climate.set_temperature` + master kill switch.
       Advisor consult before building (first real writes).
6. ⬜ Humidity bias + fan-circulate layers.
7. ⬜ Optimal-start + day/night setback wiring.
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
