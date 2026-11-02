"""Tests for the oven's sensors and binary sensors. A fake connection stands in for the websocket."""

import copy
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.jelly_anova.binary_sensor import BINARY_SENSORS
from custom_components.jelly_anova.const import DOMAIN
from custom_components.jelly_anova.sensor import SENSORS
from custom_components.jelly_anova.state import parse_state

from .test_setup import fake, setup  # noqa: F401 - fake is a fixture

FIXTURES = Path(__file__).parent / "fixtures"
TRANSLATIONS = Path(__file__).parents[1] / "custom_components" / "jelly_anova" / "translations" / "en.json"
STRINGS = Path(__file__).parents[1] / "custom_components" / "jelly_anova" / "strings.json"

# Copied from design.md's Entities table, plus every sensor the old andr83 integration had.
# (domain, key, enabled by default)
DESIGN = [
    # Cook
    ("sensor", "mode", True),
    ("sensor", "active_stage", True),
    ("sensor", "stages_count", True),
    ("sensor", "cook_time", True),
    ("sensor", "timer", True),
    ("sensor", "timer_initial", True),
    ("sensor", "timer_mode", True),
    ("sensor", "cook_origin", True),
    # Temperature
    ("sensor", "temperature", True),
    ("sensor", "wet_temperature", True),
    ("sensor", "top_temperature", True),
    ("sensor", "bottom_temperature", True),
    ("sensor", "target_temperature", True),
    ("sensor", "temperature_probe", True),
    ("sensor", "target_temperature_probe", True),
    # Steam
    ("sensor", "steam_generator_mode", True),
    ("sensor", "relative_humidity", True),
    ("sensor", "target_humidity", True),
    ("sensor", "boiler_watts", True),
    ("sensor", "evaporator_watts", True),
    ("sensor", "boiler_temperature", True),
    ("sensor", "evaporator_temperature", True),
    # Power
    ("sensor", "top_watts", True),
    ("sensor", "bottom_watts", True),
    ("sensor", "rear_watts", True),
    ("sensor", "fan_speed", True),
    ("sensor", "vent_state", True),
    ("sensor", "steam_percentage", True),
    ("sensor", "target_steam_percentage", True),
    # Binary
    ("binary_sensor", "cooking", True),
    ("binary_sensor", "door_closed", True),
    ("binary_sensor", "probe_connected", True),
    ("binary_sensor", "vent_closed", False),
    ("binary_sensor", "water_low", True),
    ("binary_sensor", "water_tank_empty", True),
    ("binary_sensor", "water_tank_removed", True),
    ("binary_sensor", "waste_tank_full", True),
    ("binary_sensor", "waste_tank_removed", True),
    ("binary_sensor", "lamp_on", True),
    ("binary_sensor", "sous_vide", True),
    ("binary_sensor", "oven_empty", True),
    ("binary_sensor", "online", True),
    # Maintenance
    ("sensor", "top_element_hours", True),
    ("sensor", "bottom_element_hours", True),
    ("sensor", "rear_element_hours", True),
    ("sensor", "boiler_hours", True),
    ("sensor", "evaporator_hours", True),
    ("binary_sensor", "descale_needed", True),
    ("binary_sensor", "any_failed", True),
    ("sensor", "firmware", True),
    ("sensor", "firmware_updated", True),
    # Own total
    ("sensor", "total_cook_hours", True),
    # Reported but rarely useful (feature-audit.md): present, disabled by default
    ("binary_sensor", "cavity_overheated", False),
    ("binary_sensor", "triacs_failed", False),
    ("binary_sensor", "lamp_failed", False),
    ("binary_sensor", "top_element_failed", False),
    ("binary_sensor", "bottom_element_failed", False),
    ("binary_sensor", "rear_element_failed", False),
    ("binary_sensor", "boiler_failed", False),
    ("binary_sensor", "evaporator_failed", False),
    ("binary_sensor", "fan_failed", False),
    ("sensor", "boiler_pump_faults", False),
    ("sensor", "boiler_valve_faults", False),
    ("sensor", "internal_line_faults", False),
    ("sensor", "display_board_temperature", False),
    ("sensor", "exhaust_fan_speed", False),
    ("sensor", "led_fan_speed", False),
    ("sensor", "power_board_fan", False),
    ("sensor", "display_fan_speed", False),
    ("sensor", "hardware_version", False),
    ("sensor", "mains_voltage", False),
    ("sensor", "mains_frequency", False),
    ("sensor", "last_disconnected", False),
    ("sensor", "food_category", False),
    ("sensor", "food_confidence", False),
    ("sensor", "door_light_preference", False),
]

# Every sensor and binary sensor the old andr83 integration had.
OLD_SENSORS = [
    "mode", "temperature", "target_temperature", "temperature_probe", "target_temperature_probe",
    "rear_watts", "bottom_watts", "top_watts", "fan_speed", "steam_generator_mode",
    "relative_humidity", "target_humidity", "cook_time", "timer", "timer_initial", "timer_mode",
    "active_stage", "stages_count",
]  # fmt: skip
OLD_BINARY = ["sous_vide", "lamp_on", "door_closed", "water_tank_empty"]

# Every word an entity name may use. A typo is not in this list.
WORDS = set(
    """a active any boiler bottom board cavity category cook cooking descale display door element
    evaporator exhaust failed fan faults firmware food frequency full hardware hours humidity
    initial internal last led lamp light line low mains maintenance mode oven online origin
    overheated part power preference probe progress pump rear relative removed sous stage stages
    count steam switches tank target temperature time timer top total updated usage valve vent
    vide voltage waste water watts wet confidence connected closed disconnected empty generator
    percentage speed version needed status level number of current length connection descaling
    overheating position parts used started from bulb""".split()
)  # fmt: skip


def load(name):
    return copy.deepcopy(json.loads((FIXTURES / name).read_text())["payload"])


def fresh(payload):
    """The payload stamped now, so the oven counts as reporting."""
    payload["state"]["updatedTimestamp"] = dt_util.utcnow().isoformat()
    return payload


async def push(hass, entry, payload):
    runtime = entry.runtime_data
    runtime.on_status(True)
    runtime.on_state(parse_state(payload, runtime.state))
    await hass.async_block_till_done()


def entity_id(hass, domain, key):
    registry = er.async_get(hass)
    found = registry.async_get_entity_id(domain, DOMAIN, f"{hass.data['_entry_id']}_{key}")
    assert found is not None, f"no {domain} with key {key}"
    return found


@pytest.fixture
async def entry(hass, fake):  # noqa: F811
    entry = await setup(hass)
    hass.data["_entry_id"] = entry.entry_id
    return entry


def state_of(hass, domain, key):
    return hass.states.get(entity_id(hass, domain, key))


async def test_every_design_entity_exists(hass, entry):
    registry = er.async_get(hass)
    for domain, key, enabled in DESIGN:
        found = registry.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{key}")
        assert found is not None, f"missing {domain} {key}"
        assert (registry.async_get(found).disabled_by is None) is enabled, f"{key} enabled default"


async def test_nothing_unlisted_exists(hass, entry):
    registry = er.async_get(hass)
    expected = {f"{entry.entry_id}_{key}" for _, key, _ in DESIGN}
    # Sensors and binary sensors only: the controls (button, number, switch, select) are in test_services.
    made = {
        e.unique_id for e in registry.entities.values() if e.platform == DOMAIN and e.domain in ("sensor", "binary_sensor")
    }
    assert made == expected


def test_old_integration_keys_are_kept():
    keys = {d.key for d in SENSORS}
    assert set(OLD_SENSORS) <= keys
    assert set(OLD_BINARY) <= {d.key for d in BINARY_SENSORS}


async def test_device_is_named_and_ids_follow_the_names(hass, entry):
    registry = er.async_get(hass)
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.name for d in devices] == ["Anova Precision Oven"]
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_temperature_probe") == (
        "sensor.anova_precision_oven_probe_temperature"
    )
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_sous_vide") == (
        "binary_sensor.anova_precision_oven_sous_vide_mode"
    )


async def test_temperature_shows_fahrenheit(hass, entry):
    hass.config.units = US_CUSTOMARY_SYSTEM
    payload = fresh(load("idle.json"))
    payload["state"]["nodes"]["temperatureBulbs"]["dry"]["current"]["celsius"] = 218.33
    await push(hass, entry, payload)
    state = state_of(hass, "sensor", "temperature")
    assert state.attributes["unit_of_measurement"] == "°F"
    assert float(state.state) == pytest.approx(425, abs=0.5)


async def test_heating_uses_watts(hass, entry):
    payload = fresh(load("idle.json"))
    payload["state"]["nodes"]["heatingElements"]["rear"]["watts"] = 1500
    await push(hass, entry, payload)
    state = state_of(hass, "sensor", "rear_watts")
    assert state.state == "1500"
    assert state.attributes["unit_of_measurement"] == "W"
    assert state.attributes["device_class"] == "power"
    assert state_of(hass, "sensor", "top_watts").state == "0"


async def test_unavailable_when_stale(hass, entry, freezer):
    await push(hass, entry, fresh(load("idle.json")))
    assert state_of(hass, "sensor", "mode").state == "idle"
    assert state_of(hass, "binary_sensor", "door_closed").state == "on"
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state_of(hass, "sensor", "mode").state == STATE_UNAVAILABLE
    assert state_of(hass, "binary_sensor", "door_closed").state == STATE_UNAVAILABLE


async def test_unavailable_before_first_report(hass, entry):
    assert state_of(hass, "sensor", "mode").state == STATE_UNAVAILABLE


async def test_unknown_when_field_missing(hass, entry):
    payload = fresh(load("idle.json"))
    del payload["state"]["nodes"]["door"]
    del payload["state"]["nodes"]["heatingElements"]["top"]["watts"]
    del payload["state"]["nodes"]["steamGenerators"]["boiler"]["descaleRequired"]
    await push(hass, entry, payload)
    assert state_of(hass, "binary_sensor", "door_closed").state == STATE_UNKNOWN
    assert state_of(hass, "binary_sensor", "descale_needed").state == STATE_UNKNOWN
    assert state_of(hass, "sensor", "top_watts").state == STATE_UNKNOWN
    # No cook section and no timer in an idle report: unknown, never 0 or off.
    for key in ("active_stage", "stages_count", "cook_time", "timer", "timer_initial", "timer_mode", "cook_origin"):
        assert state_of(hass, "sensor", key).state == STATE_UNKNOWN, key
    assert state_of(hass, "binary_sensor", "sous_vide").state == STATE_UNKNOWN
    assert state_of(hass, "sensor", "temperature_probe").state == STATE_UNKNOWN
    assert state_of(hass, "sensor", "relative_humidity").state == STATE_UNKNOWN


async def test_false_is_off_not_unknown(hass, entry):
    await push(hass, entry, fresh(load("idle.json")))
    assert state_of(hass, "binary_sensor", "water_tank_empty").state == "off"
    assert state_of(hass, "binary_sensor", "lamp_on").state == "off"
    assert state_of(hass, "binary_sensor", "cooking").state == "off"


async def test_cook_sensors(hass, entry):
    payload = fresh(load("login_cook.json"))
    now = dt_util.utcnow()
    payload["state"]["nodes"]["timer"]["startedAtTimestamp"] = (now - timedelta(seconds=20)).isoformat()
    payload["state"]["cook"]["startedTimestamp"] = (now - timedelta(seconds=100)).isoformat()
    payload["state"]["updatedTimestamp"] = now.isoformat()
    await push(hass, entry, payload)
    assert state_of(hass, "sensor", "mode").state == "cook"
    assert state_of(hass, "binary_sensor", "cooking").state == "on"
    assert state_of(hass, "sensor", "active_stage").state == "1"
    assert state_of(hass, "sensor", "stages_count").state == "1"
    assert state_of(hass, "sensor", "timer_initial").state == "60"
    assert state_of(hass, "sensor", "timer_mode").state == "running"
    assert state_of(hass, "sensor", "timer").state == "60"  # 40 s left, shown as 1 minute
    assert state_of(hass, "sensor", "cook_time").state == "120"  # 100 s, rounded to 2 minutes
    assert state_of(hass, "sensor", "cook_origin").state == "ios"
    assert state_of(hass, "sensor", "steam_generator_mode").state == "steam-percentage"
    assert state_of(hass, "binary_sensor", "sous_vide").state == "off"


async def test_sous_vide_on(hass, entry):
    await push(hass, entry, fresh(load("app_sous_vide.json")))
    assert state_of(hass, "binary_sensor", "sous_vide").state == "on"
    assert state_of(hass, "sensor", "relative_humidity").state == "64"
    assert state_of(hass, "sensor", "target_humidity").state == "100"


async def test_end_stage_shows_last_real_stage(hass, entry):
    """The hidden end stage is not counted, and its index is never shown as a stage."""
    from custom_components.jelly_anova.const import END_STAGE_TITLE

    payload = fresh(load("login_cook.json"))
    cook = payload["state"]["cook"]
    end = {"id": "end", "title": END_STAGE_TITLE, "do": {"type": "cook"}}
    cook["stages"].append(end)
    cook["activeStageIndex"] = 1
    cook["activeStageId"] = "end"
    await push(hass, entry, payload)
    assert state_of(hass, "sensor", "stages_count").state == "1"
    assert state_of(hass, "sensor", "active_stage").state == "1"


async def test_total_hours_state_class(hass, entry):
    entry.runtime_data._total_hours = 2.5  # the runtime's own count
    await push(hass, entry, fresh(load("idle.json")))
    state = state_of(hass, "sensor", "total_cook_hours")
    assert state.attributes["state_class"] == "total_increasing"
    assert state.attributes["unit_of_measurement"] == "h"
    assert state.attributes["device_class"] == "duration"
    assert float(state_of(hass, "sensor", "total_cook_hours").state) == 2.5


async def test_usage_hours_and_firmware(hass, entry):
    await push(hass, entry, fresh(load("idle.json")))
    assert float(state_of(hass, "sensor", "rear_element_hours").state) == 35.3
    assert state_of(hass, "sensor", "firmware").state == "11_235_1.4.7_01.01.26"
    assert state_of(hass, "sensor", "firmware_updated").attributes["device_class"] == "timestamp"
    assert state_of(hass, "binary_sensor", "online").state == "on"


async def test_disabled_values_come_from_the_report():
    """Disabled entities have no state in Home Assistant, so read their descriptions."""
    payload = load("idle.json")
    payload["state"]["nodes"]["cavityCamera"]["detection"] = [
        {"type": "food", "categoryId": 202, "confidence": 0.965}
    ]
    state = parse_state(payload, None)
    values = {d.key: d.value_fn(state) for d in SENSORS if d.value_fn}
    assert values["food_category"] == 202
    assert values["food_confidence"] == pytest.approx(96.5)
    assert values["led_fan_speed"] == "mid"
    assert values["mains_voltage"] == 120
    assert values["mains_frequency"] == 60
    assert values["display_board_temperature"] == 41
    assert values["door_light_preference"] == "on"
    assert values["hardware_version"] == "nxpke1-i500"
    flags = {d.key: d.value_fn(state) for d in BINARY_SENSORS}
    assert flags["triacs_failed"] is False
    assert flags["cavity_overheated"] is False
    assert flags["top_element_failed"] is False
    assert flags["lamp_failed"] is None  # idle fixture has no lamp failed field


def _names():
    strings = json.loads(STRINGS.read_text())["entity"]
    return {
        (domain, key): value["name"]
        for domain, group in strings.items()
        if domain in ("sensor", "binary_sensor")  # the controls' names are checked in test_services
        for key, value in group.items()
    }


def test_every_description_has_a_translated_name():
    names = _names()
    for domain, descriptions in (("sensor", SENSORS), ("binary_sensor", BINARY_SENSORS)):
        for description in descriptions:
            assert description.translation_key == description.key
            assert (domain, description.key) in names, description.key
    assert len(names) == len(SENSORS) + len(BINARY_SENSORS)


def test_translations_match_strings():
    strings = json.loads(STRINGS.read_text())
    assert json.loads(TRANSLATIONS.read_text()) == strings


def test_entity_names_spelled():
    for (domain, key), name in _names().items():
        for word in re.findall(r"[A-Za-z]+", name):
            assert word.lower() in WORDS, f"{domain} {key}: {word!r} is not in the spell-checked word list"
        assert name == name.strip() and name[0].isupper()


async def _cook_with_timer(hass, entry, mode, started, initial=3600, run_s=0, cook_s=0):
    payload = fresh(load("login_cook.json"))
    now = dt_util.utcnow()
    node = payload["state"]["nodes"]["timer"]
    node["mode"] = mode
    if started:
        node["startedAtTimestamp"] = (now - timedelta(seconds=run_s)).isoformat()
    else:
        node.pop("startedAtTimestamp", None)
    payload["state"]["cook"]["stages"][0]["do"]["timer"]["initial"] = initial
    payload["state"]["cook"]["startedTimestamp"] = (now - timedelta(seconds=cook_s)).isoformat()
    await push(hass, entry, payload)


async def _timer_node(hass, entry, node):
    """The login_cook report with the timer node exactly as the oven sent it."""
    payload = fresh(load("login_cook.json"))
    payload["state"]["nodes"]["timer"] = node
    await push(hass, entry, payload)


async def test_finished_timer_is_not_shown_as_live(hass, entry):
    # Real shape after a timer ended (captures/battery-timer_end): the node is just idle,
    # while the stage still holds do.timer.initial.
    await _timer_node(hass, entry, {"mode": "idle"})
    assert state_of(hass, "sensor", "timer").state == STATE_UNKNOWN
    assert state_of(hass, "sensor", "timer_mode").state == "idle"
    assert state_of(hass, "sensor", "timer_initial").state == "60"  # the stage's own setting


async def test_waiting_timer_shows_its_full_time(hass, entry):
    # Real shape of a timer waiting for a manual start (captures/battery-timer_manual).
    await _timer_node(hass, entry, {"mode": "idle", "initial": 60})
    assert state_of(hass, "sensor", "timer").state == "60"


async def test_completed_timer_is_zero(hass, entry):
    await _timer_node(hass, entry, {"mode": "completed"})
    assert state_of(hass, "sensor", "timer").state == "0"


async def test_end_stage_hides_stage_values(hass, entry):
    from custom_components.jelly_anova.const import END_STAGE_TITLE

    payload = fresh(load("login_cook.json"))
    cook = payload["state"]["cook"]
    end = {"id": "end", "title": END_STAGE_TITLE, "do": {"type": "cook", "timer": {"initial": 43200}}}
    cook["stages"].append(end)
    cook["activeStageIndex"] = 1
    cook["activeStageId"] = "end"
    payload["state"]["nodes"]["temperatureBulbs"]["dry"]["setpoint"]["celsius"] = 25
    payload["state"]["nodes"]["timer"]["mode"] = "running"
    await push(hass, entry, payload)
    for key in ("target_temperature", "timer", "timer_initial", "timer_mode", "target_temperature_probe"):
        assert state_of(hass, "sensor", key).state == STATE_UNKNOWN, key
    assert state_of(hass, "binary_sensor", "sous_vide").state == STATE_UNKNOWN
    # the real stage, by contrast, shows its target
    await push(hass, entry, fresh(load("login_cook.json")))
    assert float(state_of(hass, "sensor", "target_temperature").state) == 60


async def test_cook_time_and_timer_change_once_a_minute(hass, entry):
    await _cook_with_timer(hass, entry, "running", True, run_s=61, cook_s=61)
    first = (state_of(hass, "sensor", "cook_time").state, state_of(hass, "sensor", "timer").state)
    await _cook_with_timer(hass, entry, "running", True, run_s=80, cook_s=80)
    assert (state_of(hass, "sensor", "cook_time").state, state_of(hass, "sensor", "timer").state) == first
    assert int(first[0]) % 60 == 0 and int(first[1]) % 60 == 0
    await _cook_with_timer(hass, entry, "running", True, run_s=130, cook_s=130)
    assert state_of(hass, "sensor", "cook_time").state != first[0]
