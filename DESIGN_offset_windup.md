# Design: fix offset/integral windup on large scheduled setbacks

Status: **APPROVED — Option A (equipment-saturation signal). Implementing.** User chose Option A on
2026-06-26. Build per §5 on branch `fix/offset-windup`.

## 1. Root cause (confirmed against a live run, 2026-06-25/26)

The control law is a velocity-form integral: each trim tick *adds* `clamp(kp*error, ±max_step)`
onto the current band (`controller.py:decide`). On a large scheduled **setback** (day 70 °F → night
64 °F) the feedforward jumps the band, then the integral trim keeps shifting the band the same
direction every `min_period_s` (default **720 s / 12 min**) by up to `max_step` (**0.5 °F**) for as
long as `|error| > deadband`.

Because the AC **could not track** the 5 °F pulldown (compressor flat-out: the Daikin's own
hallway sensor sat ~8 °F above `band_high`, never satisfied), `error` stayed negative for hours, so
the band wound **~8 °F below its steady-state position** (band_center observed at ~51.8 °F vs. a
true steady-state of ~60.3 °F).

Then two things compounded:

1. **Learning from a wound-up band.** When the house finally entered the deadband,
   `K (learned_offset)` learns toward `band_center − house_average` — but `band_center` was the
   wound-up 51.8, so `K` corrupted **−3.66 → ~−12.6** (observed −11.31).
2. **No band relaxation in-deadband.** Within the deadband `decide()` returns `set_band=False`, so
   the over-driven band is never relaxed back toward steady state; it stays wound up and `K`
   converges to the wrong value.

Consequence: the next feedforward (`band_center = target + K`) places the band **~7 °F too low** →
on re-enable the controller forces the house too cold. This is the user-facing failure.

## 2. The immediate bridge (live remediation — independent of the code fix)

The code fix can't reach the live box until release → HACS re-download → restart. For **today**:

1. **Reset the corrupted K:** Settings → Devices & Services → Multiroom Climate → ⋮ → **Delete**,
   then **re-add** it (same Daikin entity, the weighted-average temp sensor, the indoor humidity
   sensor). Delete+re-add wipes the entry's stored control state → `K` returns to 0.
   *(Reconfigure does NOT reset K — it deliberately keeps the learned offset, config_flow.py:111-113.)*
2. **Leave the day/night schedule OFF** (don't configure it). Constant-target operation only ever
   does small trims — there is no setback feedforward and no sustained-saturation windup, so `K`
   learns the true bias (~−3.66) safely. This keeps the core bias-compensation working today,
   without the buggy path.
3. Turn the master switch on. First tick feedforwards with `K=0` → band ≈ target (no cold-forcing),
   then trim re-learns the true bias gently.

Re-enable the schedule only after the fix below ships.

## 3. Why the naive fix deadlocks (the load-bearing insight)

Every band-geometry fix references `target + K`, and `K` can be wrong — which creates a
**cold-start deadlock**:

- Gate learning on `|band_center − (target+K)| ≤ tol`: at cold-start/post-reset `K=0`, but the band
  must migrate to `target + true_bias` (~−3.66) to hold the house. The instant the house reaches the
  deadband, `band_center` is ~3.66 from `target+K` → gate refuses to learn → `K` stays 0 → band
  stuck → **deadlock**.
- Anti-windup-alone has the same trap: clamp `band_center` to `target+K ± W`; any `W` small enough
  to bound the corruption is also small enough to strangle the legitimate steady-state bias.

The tension is fundamental to any fix expressed in **band-vs-K geometry**. The clean break is a
**saturation signal independent of K**: don't wind / don't learn when the *equipment* is maxed
(running flat-out and not satisfied). Driving `band_high` lower while the compressor is already
saturated adds **zero** extra cooling — so a *tight* anti-windup clamp costs nothing on legitimate
recovery time, and the "need a big W for fast pulldown" worry evaporates.

**But `ControllerInputs` today carries only `house_average` + the band — no wrapped-thermostat
temperature and no `hvac_action`.** So the saturation-signal fix needs a data-model change.

## 4. Approach decision — DECIDED: Option A (equipment-saturation signal)

### Option A — add an equipment-saturation signal (recommended, principled)
Add the wrapped thermostat's own temperature (and/or `hvac_action`) to `ControllerInputs` + wire it
in the coordinator. Then:
- **Anti-windup:** stop shifting the band further in the demand direction while the equipment is
  saturated (own-sensor far beyond the band / `hvac_action` shows full-out-and-unsatisfied).
- **Learning gate:** only update `K` when the equipment is *modulating* (not saturated) AND
  `|error| ≤ deadband` — guaranteeing the band sample is a real steady state.
- No `W` tuning, no cold-start deadlock (the gate keys on saturation, not band-vs-K geometry).
- Cost: data-model change + coordinator wiring + new tests.

### Option B — no new sensor (time-based, lighter)
Keep `ControllerInputs` as-is. Use a small anti-windup clamp on band excursion **plus** gate
learning on **time-stability** (band hasn't moved for N ticks) rather than band-vs-K geometry —
which sidesteps the deadlock via settle-time instead of an equipment signal.
- Cost: lower; no data-model change. Less principled; relies on settle-time heuristics and a clamp
  margin that can still mis-tune on unusual systems.

**Recommendation: Option A.** This is the core HVAC engine of a live home; the equipment signal is
the correct, deadlock-free fix and the extra plumbing is modest.

## 5. Build plan (once approach is chosen)

1. **Failing regression test first** — encode the exact live scenario: target 70→64 setback, a house
   that lags (AC saturated), `min_period_s` ticks over hours; assert `K` stays sane (≈ −3 to −4) and
   the band never winds > a bound below steady state. This is also how we'll know the fix works.
2. Implement the chosen anti-windup + learning gate (small CLs, velocity-form preserved — do NOT
   convert to proportional-position; the accumulation absorbs sensor bias, see controller.py docstring).
3. Add a **"Reset learned offset" button/service entity** so future corruption (or any bad state) is
   recoverable from the UI without delete+re-add. (Approach-independent — can land first.)
4. Keep 100% statement+branch coverage and hassfest green. `/simplify` the PR. Advisor before
   committing to the control-law change and before declaring done.
5. After merge: note the release → HACS re-download → restart steps; user can then re-enable the
   schedule.

## 6. Constraints
- Preserve the velocity-form integral (no proportional-position rewrite).
- Build only in this repo + venv; never touch the production HA box or the milker path.
