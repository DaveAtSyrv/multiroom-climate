"""The Multiroom Climate integration.

A Home Assistant smart thermostat that regulates the home to a weighted average of chosen room
sensors and auto-learns the bias of the wrapped thermostat's own sensor. See SPEC.md.

This module is intentionally minimal at the skeleton stage; setup wiring lands with the
config flow and coordinator (see SPEC.md section 6, build order).
"""
