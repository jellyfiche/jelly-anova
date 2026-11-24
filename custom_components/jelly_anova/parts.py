"""Where each entity sits: on the oven, or on its Maintenance device, and in which section.

The oven holds what is watched and set while cooking (the probe included). Maintenance hangs off
it (via_device) and holds what is checked between cooks: water and tanks, wear, health. Live
readings that explain a cook (element, boiler and evaporator power) are Diagnostic on the oven.
Keys are entity description keys; unique ids do not change, so existing entity ids stay.
"""

from __future__ import annotations

MAINTENANCE = "maintenance"

PART_NAMES = {MAINTENANCE: "Maintenance"}

# Parts of earlier releases (0.4.0): their devices are removed once their entities have moved.
RETIRED_PARTS = ("steam", "probe")

_PARTS = {
    MAINTENANCE: {
        # water (binary sensors)
        "water_low",
        "water_tank_empty",
        "water_tank_removed",
        "waste_tank_full",
        "waste_tank_removed",
        "descale_needed",
        # wear
        "top_element_hours",
        "bottom_element_hours",
        "rear_element_hours",
        "boiler_hours",
        "evaporator_hours",
        "total_cook_hours",
        # health
        "any_failed",
        "online",
        "firmware",
        "firmware_updated",
        # rarely useful, off by default
        "cavity_overheated",
        "triacs_failed",
        "lamp_failed",
        "top_element_failed",
        "bottom_element_failed",
        "rear_element_failed",
        "boiler_failed",
        "evaporator_failed",
        "fan_failed",
        "internal_line_faults",
        "boiler_pump_faults",
        "boiler_valve_faults",
        "mains_voltage",
        "mains_frequency",
        "hardware_version",
        "display_board_temperature",
        "exhaust_fan_speed",
        "led_fan_speed",
        "power_board_fan",
        "display_fan_speed",
        "last_disconnected",
    },
}

# Shown under Diagnostic on the oven (the off-by-default ones carry it in their own description).
DIAGNOSTIC = frozenset(
    {
        "top_watts",
        "bottom_watts",
        "rear_watts",
        "boiler_watts",
        "evaporator_watts",
        "boiler_temperature",
        "evaporator_temperature",
        "cook_origin",
    }
)


def part_of(key: str) -> str | None:
    """The part an entity belongs to, or None for the oven itself."""
    for part, keys in _PARTS.items():
        if key in keys:
            return part
    return None
