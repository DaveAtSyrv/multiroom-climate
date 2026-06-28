# Multiroom Climate

**Multi-room comfort for Daikin Skyport — in Home Assistant.**

[![Validate](https://github.com/DaveAtSyrv/multiroom-climate/actions/workflows/validate.yml/badge.svg)](https://github.com/DaveAtSyrv/multiroom-climate/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A custom Home Assistant integration that adds a smart thermostat which regulates your home to an
**average of the rooms you choose** instead of the single (often warm) thermostat sensor.
It automatically learns the bias between the thermostat's own sensor and your house average —
**separately for heating and cooling** — so the manual "set it to 67 to hold the house at 70" trick
becomes automatic and self-adjusting, and the right correction is applied as the system changes over.

I built this for my own house, where the Daikin Skyport sits in the warmest room and the rest of the
place always ran a couple degrees off. It's a personal project I'm sharing in case you have the same
problem — independent and community-built, not a product.

> **Not affiliated with, endorsed by, or sponsored by Daikin.** "Daikin" and "Skyport" are
> trademarks of their respective owners and are used here only to describe compatibility
> (nominative use). This is an independent, community-built integration.

## What it does

- Targets a **house-average temperature**, not the thermostat's own sensor.
- **Auto-learns the thermostat-sensor bias — separately for heating and cooling** — and compensates
  continuously (the sensor reads a different offset in each mode, so cooling often needs a larger
  correction than heating).
- **Feedforward + proportional trim** control: jumps fast on changes, holds gently at steady state.
- **Automatic heat/cool changeover** by sliding the thermostat's AUTO band (the equipment keeps its
  own compressor protection), applying the correct learned offset the moment the mode flips.
- **Day/night temperature setback** with a fixed optimal-start lead so the house is at temp *by* the
  scheduled time.
- **Humidity bias** (overcool slightly when humid in cooling season).
- **Fan-circulate** when rooms stratify.
- **Stale-sensor failsafe** + a **master enable** toggle that instantly hands control back to the
  thermostat.

## Use cases

- **A house that heats or cools unevenly.** When the thermostat sits in the warmest (or coolest) spot,
  the rest of the house runs off-target. Hold the average of the rooms you actually use instead.
- **Automating the "set it to 67 to hold 70" workaround.** If you already nudge the thermostat to
  compensate for its own sensor, this learns that bias and applies it for you, and keeps adjusting as
  conditions change — learning the heating and cooling nudges independently so a changeover doesn't
  start from the wrong correction.
- **Night setback without a smart-thermostat schedule.** Set a lower night temperature and a morning
  target; the optimal-start lead brings the house back to comfortable *by* the time you want it.
- **Comfort in humid weather.** When cooling, it overcools by a small, capped amount while humidity is
  high, so the house feels less clammy.
- **Rooms that drift apart.** When one room runs much warmer or cooler than the rest, the fan
  circulates to even things out, then backs off once they re-converge.

## Requirements & supported devices

**Before installing**, you'll need the following already in Home Assistant:

- A **`climate.*` thermostat entity** to wrap (see the compatibility notes below).
- **One or more remote temperature sensors** for the rooms you want to average (any `sensor.*`
  temperature entities — the room/remote sensors the thermostat itself can't see).
- A humidity sensor is optional (it enables the cooling-season overcool).

Multiroom Climate doesn't talk to any thermostat directly; it wraps the existing `climate.*` entity. A
thermostat works as long as that entity exposes a **heat/cool (AUTO) band** (separate low/high
setpoints, `target_temp_low` / `target_temp_high`, plus min/max bounds), because the controller
regulates by sliding that band.

- **Tested with:** Daikin Skyport thermostats, via the community
  [apetrycki/daikinskyport](https://github.com/apetrycki/daikinskyport) integration.
- **Should work with:** any `climate.*` entity that supports a heat/cool band (most multi-stage and
  heat-pump thermostats, in their AUTO mode).
- **Not supported:** single-setpoint thermostats (heat-only or cool-only — one target temperature, no
  AUTO band); there's nothing for the controller to slide.

A self-contained direct-API path (no wrapped entity required) is planned for v2.

## Installation

### HACS (custom repository)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=DaveAtSyrv&repository=multiroom-climate&category=integration)

Click the button to open HACS with this repository pre-filled, choose **Download**, then restart Home
Assistant. To add it by hand instead:

1. In HACS, open the **⋮** menu → **Custom repositories**.
2. Add `https://github.com/DaveAtSyrv/multiroom-climate` with category **Integration**.
3. Install **Multiroom Climate**, then restart Home Assistant.

### Manual

Copy `custom_components/multiroom_climate` into your Home Assistant `config/custom_components/`
directory and restart.

## Setup

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=multiroom_climate)

After installing and restarting, click the button above (or go to **Settings → Devices & Services →
Add Integration → Multiroom Climate**) and choose:

- **Thermostat to control** — the `climate.*` entity to drive (kept in its AUTO band).
- **Target temperature sensors** — the room sensors whose average is the temperature to
  hold (pick one or more).
- **Humidity sensor** *(optional)* — enables a slight cooling-season overcool while humidity is
  above target.

This creates two entities: a **climate** entity (named after the integration) that shows the house
average with a single settable target, and a **master enable switch** (`<name> control`) that is
**off by default**. The integration only observes until you turn the switch on — a fresh install
never touches your thermostat unexpectedly.

## Day/night schedule (options)

Open **Settings → Devices & Services → Multiroom Climate → Configure** to set an optional setback
schedule:

- **Use the day/night schedule** — the master toggle for the schedule.
- **Day / Night temperature** — the two setpoints, in your system's unit.
- **Day / Night start times** — when each period begins.
- **Optimal-start lead** — how many minutes early to begin each transition so the home reaches the
  new setpoint *by* the scheduled time.

Enabling a schedule takes effect at the next day↔night transition; a mid-period change won't override
a manual hold until then.

## Usage

- **Turn on the master switch** to let the integration regulate. On first enable (or whenever you
  haven't set a target yourself) it seeds the target to the current house average ("hold where we are
  now"); a target you've set explicitly is kept across an off→on toggle. It slides the thermostat's
  band from there.
- **Set the target** on the climate entity to the house temperature you want.
- **Turn the switch off** at any time to instantly hand full control back to the thermostat.
- Diagnostic `shadow_*` attributes on the climate entity expose what the controller is doing (target,
  learned cooling/heating offsets, sensor freshness, proposed band, scheduled setpoint) — handy while
  tuning.

## Examples

Entity IDs below assume the default name "Multiroom Climate" (`climate.multiroom_climate`,
`switch.multiroom_climate_control`) — adjust them to match your setup.

**Hand control to Multiroom Climate when you're home, back to the thermostat when away:**

```yaml
automation:
  - alias: Multiroom Climate on when home
    triggers:
      - trigger: state
        entity_id: person.you
        to: home
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.multiroom_climate_control

  - alias: Multiroom Climate off when away
    triggers:
      - trigger: state
        entity_id: person.you
        to: not_home
    actions:
      - action: switch.turn_off
        target:
          entity_id: switch.multiroom_climate_control
```

**Get notified when the stale-sensor failsafe trips** (the integration freezes the setpoint but
doesn't notify on its own — see [Known limitations](#known-limitations)):

```yaml
automation:
  - alias: Alert on Multiroom Climate failsafe
    triggers:
      - trigger: state
        entity_id: climate.multiroom_climate
        attribute: shadow_status
        to: failsafe
    actions:
      - action: notify.notify
        data:
          message: Multiroom Climate lost its room sensors and is holding the thermostat.
```

**Set the house target from a script** (e.g. for a scene or a dashboard button):

```yaml
script:
  comfortable:
    sequence:
      - action: climate.set_temperature
        target:
          entity_id: climate.multiroom_climate
        data:
          temperature: 71
```

## How it updates

Multiroom Climate is a **local-polling** integration — no cloud, no push. A single coordinator polls
every **60 seconds** and, on each tick:

1. Reads your selected room sensors (skipping any that are unavailable) and computes the house average.
2. Reads the wrapped thermostat — its mode, AUTO band, temperature bounds, **and its own sensor**.
3. Runs the control logic (offset learning, feedforward/trim, changeover, fan-circulate, day/night
   schedule).
4. **If the master switch is on**, writes the resulting band to the thermostat; **if off**, computes
   the same decision but only records it (`shadow_*`) without touching the thermostat.

Everything runs locally against entities already in Home Assistant, so there's no external API to
rate-limit. Setting the target on the climate entity triggers an immediate refresh rather than
waiting for the next poll.

### Anti-windup (why the band doesn't run away on a big setback)

When you ask for a large setback the equipment may not be able to keep up — it runs flat-out and its
own sensor sits well outside the band. The controller watches the thermostat's **own** temperature
for exactly this: while the equipment is saturated it stops sliding the band further in that
direction (it would actuate nothing and only "wind up" the band away from where it belongs) and
pauses learning the sensor-bias offset (so the bias is never learned from a non-steady-state band).
This is surfaced as `shadow_status: windup_blocked` / `within_deadband_saturated`.

This guard needs the wrapped thermostat to publish its **own** temperature — visible as
`shadow_thermostat_temperature`. If that attribute is `None`, the thermostat doesn't expose its
sensor and the guard is **inert** (the integration falls back to its prior behavior). The
saturation threshold is the `saturation_margin` tunable (default `2.0`, in your system's unit) — how
far outside the band the thermostat's sensor must sit to count as flat-out; it was tuned for °F, so
a °C setup or an undersized system may want it lower.

## Troubleshooting

The climate entity exposes `shadow_*` diagnostic attributes that explain what the controller is doing
each tick — start there. `shadow_status` is the one-word reason for the current tick.

- **The thermostat never moves.** The master switch is **off by default**; the integration only
  observes until you turn the `<name> control` switch on. With it off, the `shadow_*` attributes still
  show what it *would* do, so you can confirm the decision before enabling.
- **`shadow_status: no_thermostat_band`.** The wrapped thermostat isn't exposing a low/high band. Put
  it in its **heat/cool (AUTO)** mode — the controller regulates by sliding those setpoints, which
  only exist in that mode.
- **`shadow_status: waiting_for_first_reading`.** No usable sensor reading yet. Check that your
  selected room sensors report a numeric temperature; `shadow_sensors_fresh` / `shadow_sensors_total`
  show how many are usable.
- **The climate entity is unavailable.** The wrapped thermostat is itself unavailable or was removed —
  check the integration that provides it. If it's gone for good, a repair issue prompts you to restore
  it or reconfigure.
- **It's holding the wrong temperature.** The target is the **average** of the sensors you picked.
  Check the selected sensors (and that none read wildly off); reconfigure to change the set.
- **A schedule change didn't take effect.** Enabling a schedule mid-period is inert until the next
  day↔night transition — that's expected.
- **The fan won't switch (`shadow_fan_blocked`).** Fan-circulate only manages the on/auto pair; a
  manual fan speed, or a mode the thermostat doesn't advertise, is left untouched.

## Known limitations

v1 keeps the control model deliberately simple. Current limitations (most lifted in later versions):

- **Rooms are weighted equally.** The target is a plain average of the chosen sensors; per-room
  weighting is a v2 feature.
- **One house-wide schedule.** The day/night setback uses a single pair of setpoints for the whole
  house — no per-room target switching.
- **Fixed, symmetric optimal-start lead.** One lead time pulls *both* the day and night transitions
  earlier by the same amount; it isn't learned or occupancy-aware.
- **Humidity is a bounded overcool only.** In cooling season it overcools by a capped amount when
  humidity is high — not true dehumidify-demand control (that needs the v2 direct API).
- **Fan-circulate can't tell whose "on" it is.** If you set the thermostat fan to continuous yourself,
  it's returned to auto once the rooms re-converge — the integration doesn't distinguish a manual `on`
  from its own.
- **The stale-sensor failsafe doesn't notify yet.** If every sensor goes stale it freezes the setpoint
  and surfaces a `shadow_notify` message, but doesn't actually send a notification.
- **A deep setback recovers as fast as the equipment allows — no faster.** After a large day↔night
  setback the controller slides the band toward the new target, but the house only moves at the rate
  the HVAC (and passive warming/cooling) can manage. In cooling season especially, *warming back up*
  from a low night setpoint is largely passive, so a big morning swing can take a while. The
  optimal-start lead helps, but a very large setback will still lag. Prefer a modest setback if you
  want a tight morning target.

## Removing the integration

1. **Settings → Devices & Services → Multiroom Climate**, open the **⋮** menu on the entry and choose
   **Delete**. Repeat for each entry if you set up more than one thermostat.
2. *(If installed via HACS)* In **HACS**, open **Multiroom Climate → ⋮ → Remove**, then restart Home
   Assistant.

Deleting an entry removes its climate and switch entities and the device, and **deletes the
integration's stored control state** (the learned cooling/heating sensor-bias offsets and the held
target). The wrapped
thermostat itself is left untouched and returns to manual control — its current AUTO band is kept as-is
(already bias-compensated for current conditions), so there's no jump on handback.

## Status

A personal project, shared as-is. It's pre-1.0 and the control model is deliberately simple (see
[Known limitations](#known-limitations)) — actively developed, and not yet tested across a wide range
of setups. Issues and notes from your own install are welcome. See [SPEC.md](SPEC.md) for the full
design and roadmap.

## License

[MIT](LICENSE)
