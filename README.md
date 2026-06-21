# Multiroom Climate

**Multi-room comfort for Daikin Skyport — in Home Assistant.**

A custom Home Assistant integration that adds a smart thermostat which regulates your home to an
**average of the rooms you choose** instead of the single (often warm) thermostat sensor.
It automatically learns the bias between the thermostat's own sensor and your house average — so the
manual "set it to 67 to hold the house at 70" trick becomes automatic and self-adjusting.

> **Not affiliated with, endorsed by, or sponsored by Daikin.** "Daikin" and "Skyport" are
> trademarks of their respective owners and are used here only to describe compatibility
> (nominative use). This is an independent, community-built integration.

## What it does

- Targets a **house-average temperature**, not the thermostat's own sensor.
- **Auto-learns the thermostat-sensor bias** and compensates continuously.
- **Feedforward + proportional trim** control: jumps fast on changes, holds gently at steady state.
- **Automatic heat/cool changeover** by sliding the thermostat's AUTO band (the equipment keeps its
  own compressor protection).
- **Day/night temperature setback** with a fixed optimal-start lead so the house is at temp *by* the
  scheduled time.
- **Humidity bias** (overcool slightly when humid in cooling season).
- **Fan-circulate** when rooms stratify.
- **Stale-sensor failsafe** + a **master enable** toggle that instantly hands control back to the
  thermostat.

## Installation

### HACS (custom repository)

1. In HACS, open the **⋮** menu → **Custom repositories**.
2. Add `https://github.com/DaveAtSyrv/multiroom-climate` with category **Integration**.
3. Install **Multiroom Climate**, then restart Home Assistant.

### Manual

Copy `custom_components/multiroom_climate` into your Home Assistant `config/custom_components/`
directory and restart.

## Setup

1. **Settings → Devices & Services → Add Integration → Multiroom Climate.**
2. Choose:
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
  learned offset, sensor freshness, proposed band, scheduled setpoint) — handy while tuning.

## How it updates

Multiroom Climate is a **local-polling** integration — no cloud, no push. A single coordinator polls
every **60 seconds** and, on each tick:

1. Reads your selected room sensors (skipping any that are unavailable) and computes the house average.
2. Reads the wrapped thermostat — its mode, AUTO band, and temperature bounds.
3. Runs the control logic (offset learning, feedforward/trim, changeover, fan-circulate, day/night
   schedule).
4. **If the master switch is on**, writes the resulting band to the thermostat; **if off**, computes
   the same decision but only records it (`shadow_*`) without touching the thermostat.

Everything runs locally against entities already in Home Assistant, so there's no external API to
rate-limit. Setting the target on the climate entity triggers an immediate refresh rather than
waiting for the next poll.

## Removing the integration

1. **Settings → Devices & Services → Multiroom Climate**, open the **⋮** menu on the entry and choose
   **Delete**. Repeat for each entry if you set up more than one thermostat.
2. *(If installed via HACS)* In **HACS**, open **Multiroom Climate → ⋮ → Remove**, then restart Home
   Assistant.

Deleting an entry removes its climate and switch entities and the device, and **deletes the
integration's stored control state** (the learned sensor-bias offset and the held target). The wrapped
thermostat itself is left untouched and returns to manual control — its current AUTO band is kept as-is
(already bias-compensated for current conditions), so there's no jump on handback.

## Status

Early development. See [SPEC.md](SPEC.md) for the full design and roadmap.

## Supported devices

Multiroom Climate doesn't talk to any thermostat directly; it wraps an existing Home Assistant
`climate.*` entity. A thermostat works as long as that entity exposes a **heat/cool (AUTO) band**
(separate low/high setpoints, `target_temp_low` / `target_temp_high`, plus min/max bounds), because
the controller regulates by sliding that band.

- **Tested with:** Daikin Skyport thermostats, via the community
  [apetrycki/daikinskyport](https://github.com/apetrycki/daikinskyport) integration.
- **Should work with:** any `climate.*` entity that supports a heat/cool band (most multi-stage and
  heat-pump thermostats, in their AUTO mode).
- **Not supported:** single-setpoint thermostats (heat-only or cool-only — one target temperature, no
  AUTO band); there's nothing for the controller to slide.

A self-contained direct-API path (no wrapped entity required) is planned for v2.

## License

[MIT](LICENSE)
