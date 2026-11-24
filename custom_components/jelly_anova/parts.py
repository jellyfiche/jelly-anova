"""Where each entity sits: on the oven, or on one of its two parts, and in which section.

The oven is the main device. The parts hang off it (via_device), as a printer's material units
do: "Steam and water" is what gets filled, emptied and descaled; "Probe" is the plug-in probe.
Diagnostic holds what explains a fault and is not watched while cooking. Keys are entity
description keys; unique ids do not change, so existing entity ids stay as they are.
"""

from __future__ import annotations

STEAM = "steam"
PROBE = "probe"

PART_NAMES = {STEAM: "Steam and water", PROBE: "Probe"}
PART_MODELS = {STEAM: "Steam generator and tanks", PROBE: "Food probe"}

_PARTS = {
    STEAM: {
        # sensors
        "boiler_watts",
        "evaporator_watts",
        "boiler_temperature",
        "evaporator_temperature",
        "boiler_hours",
        "evaporator_hours",
        "boiler_pump_faults",
        "boiler_valve_faults",
        # binary sensors
        "water_low",
        "water_tank_empty",
        "water_tank_removed",
        "waste_tank_full",
        "waste_tank_removed",
        "descale_needed",
        "boiler_failed",
        "evaporator_failed",
    },
    PROBE: {"temperature_probe", "target_temperature_probe", "probe_connected"},
}

# Shown under Diagnostic (those already marked in their own description are not repeated here).
DIAGNOSTIC = frozenset(
    {
        "boiler_watts",
        "evaporator_watts",
        "boiler_temperature",
        "evaporator_temperature",
        "top_watts",
        "bottom_watts",
        "rear_watts",
        "cook_origin",
        "total_cook_hours",
        "any_failed",
    }
)


def part_of(key: str) -> str | None:
    """The part an entity belongs to, or None for the oven itself."""
    for part, keys in _PARTS.items():
        if key in keys:
            return part
    return None
