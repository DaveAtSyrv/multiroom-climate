# Windup fix — review findings & follow-ups

Review of the offset-windup work: PR #51 (`fix/offset-windup`, the saturation-gated anti-windup +
offset-learning guard), PR #50 (merged — the learned-offset override number), and
`DESIGN_offset_windup.md`. This file accumulates prioritized, triaged findings.

## Correctness review (PR #51): CLEAN — no bugs found

A dedicated correctness/edge-case review was run (previously only `/simplify` = quality had run).
Verified correct: feedforward-with-persisted-K (can't wind; K frozen during prior saturation so the
persisted value is a good steady-state sample), `windup_blocked` deliberately not stamping
`last_change_ts` (so recovery trims fire immediately once saturation clears), failsafe path bypasses
the saturation machinery, `_demand_saturation` sign/boundary logic (directional block correct for
both cooling and heating), `None`-safety via `convert(...)`, and the bounds-clamp interaction.
**No code changes required for correctness.**

## Findings (prioritized)

### P1 — Observability: the saturation guard can be silently inert  (confidence 75)
When the wrapped thermostat exposes no `current_temperature` (or it's transiently `None` during an
entity/integration reload or HACS update), `_demand_saturation` returns 0 and the guard is OFF —
**indistinguishable from active** from logs or `shadow_*`. For an install that already hit this bug,
there's no way to confirm the protection is armed.
- **Fix options (small CL):** (a) a diagnostic attribute (e.g. `shadow_saturation` / a
  `guard_armed` bool) surfaced in `CoordinatorData` + diagnostics; and/or (b) a debounced repair
  issue when `thermostat_temperature is None` for N consecutive ticks while regulating — mirrors the
  existing `_reconcile_thermostat_repair`. Cheapest: a `_LOGGER.debug` + a diagnostic bool.
- **Recommend:** the diagnostic attribute now (cheap, observable, no control-law change); repair
  issue optional. **Needs user OK (touches coordinator data) — not a control-law change.**

### P2 — Docs: README doesn't cover the new behavior
No mention of the equipment-saturation guard, nor the passive-warming recovery caveat after a setback
in cooling season (observed live: the house held ~64° for hours the morning after the 64° setback).
- Add a short "anti-windup uses the wrapped thermostat's own sensor (needs that attribute)" note.
- Add to **Known limitations**: a deep setback's recovery is gated by equipment capacity + passive
  warming (cooling season), so it can be slow.
- Low risk, medium value (honest user expectations). No approval needed beyond a go-ahead.

### P2 — Docs: `saturation_margin` tuning + unit note  (confidence 45–70)
`saturation_margin = 2.0` is degree-valued in the system unit (validated for °F here). Surface the
tuning lever where operators look (options/README, not only the docstring): lower it if a zone sits
chronically near-saturation (the K-freeze edge case below) or for a °C deployment (2.0 °C is a wider,
less sensitive margin).

### P3 — K-freeze in a never-de-saturates regime  (confidence 70) — design-correct, document only
If the house sits comfortably in-deadband while the thermostat's own sensor stays just past the
margin (an undersized / chronically under-cooled zone), K freezes and never converges; a post-reset
K=0 could stay 0, making the next feedforward overshoot. Self-corrects via trim; observable via the
`within_deadband_saturated` reason. **No code change** — fold into the tuning note (P2).

### P3 — Heating season unvalidated on hardware
The heating-saturated (`+1`) path is unit-tested but the live incident was cooling-only. Fold into
the post-deploy live-verification (watch a heating setback too).

### P3 — End-to-end test coverage is cooling-only
The coordinator-level (wiring) test `test_saturated_thermostat_blocks_windup_trim` exercises the
*cooling*-saturated `windup_blocked` path. The heating-saturated (`+1`) path and the
`within_deadband_saturated` learning-freeze are covered at the **controller unit level** but not
end-to-end through the coordinator. The wiring is symmetric, so this is completeness, not a gap in
behavior — add two sibling coordinator tests if/when P1 is implemented (same fixture, different
`current_temperature`). Low value alone; fold into the P1 CL.

## Nitpicks (not actioned)
- `DESIGN_*` / `FOLLOWUPS_*` docs in repo root — minor clutter; could move to `docs/`.
- Resetting K via the #50 number doesn't immediately re-feedforward the band (waits for the next
  target change); trim corrects — acceptable.

## Recommended order
1. **(P1)** diagnostic "guard armed / saturation" attribute — small CL + test, needs user OK.
2. **(P2)** README docs update (saturation guard + slow-setback-recovery caveat).
3. **(P2/P3)** `saturation_margin` tuning + unit note in docs.
4. Heating-season + overall live verification → the post-deploy monitoring loop offer.

Status: **REVIEW COMPLETE & TRIAGED.** All listed review areas covered (correctness — clean; docs;
`saturation_margin` unit-dependence; silent-inertness; recovery speed; observability; test
completeness). No control-law changes proposed (PR #51 is correctness-clean and CI-green/mergeable).
Items P1–P3 are **user-approved-then-implement** — none are started, nothing here is manufactured.
The review loop stopped here to avoid padding the list; re-arm it or just say "do P1/P2" to implement.
