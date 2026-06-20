# Multiroom Climate

**Multi-room comfort for Daikin Skyport — in Home Assistant.**

A custom Home Assistant integration that adds a smart thermostat which regulates your home to a
**weighted average of the rooms you choose** instead of the single (often warm) thermostat sensor.
It automatically learns the bias between the thermostat's own sensor and your house average — so the
manual "set it to 67 to hold the house at 70" trick becomes automatic and self-adjusting.

> **Not affiliated with, endorsed by, or sponsored by Daikin.** "Daikin" and "Skyport" are
> trademarks of their respective owners and are used here only to describe compatibility
> (nominative use). This is an independent, community-built integration.

## What it does

- Targets a **weighted house-average temperature**, not the thermostat's own sensor.
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

## Status

Early development. See [SPEC.md](SPEC.md) for the full design and roadmap.

## Compatibility

v1 wraps an existing `climate.*` entity (works today with the community
[apetrycki/daikinskyport](https://github.com/apetrycki/daikinskyport) integration for Daikin Skyport).
A self-contained direct-API path is planned for v2.

## License

[MIT](LICENSE)
