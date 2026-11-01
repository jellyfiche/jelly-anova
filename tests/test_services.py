"""Tests for the actions and the controls (stop button, numbers, switches, select)."""

import dataclasses
import json
from pathlib import Path

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.jelly_anova import services as services_module
from custom_components.jelly_anova.connection import OvenNoReply, OvenOffline, OvenRefused
from custom_components.jelly_anova.const import DOMAIN

from .test_runtime import NO_TIMER, ONE_STAGE, report
from .test_setup import fake, setup  # noqa: F401 - fake is a fixture

PACKAGE = Path(__file__).parents[1] / "custom_components" / "jelly_anova"
SERVICES = ["start_cook", "start_custom_cook", "stop_cook", "update_cook", "next_stage", "update_stages"]


class Oven:
    """The integration set up with a fake connection that records every command."""

    def __init__(self, hass, entry, conn):
        self.hass, self.entry, self.conn = hass, entry, conn
        self.sent: list[tuple[str, dict | None]] = []
        self.errors: list[Exception] = []

        async def send(command, payload, timeout=10):
            self.sent.append((command, payload))
            if self.errors:
                raise self.errors.pop(0)
            return {"status": "ok"}

        async def ensure_open(timeout=10):
            return None

        conn.send, conn.ensure_open, conn.cooker_id = send, ensure_open, "cooker-abc"
        conn.on_status(True)

    @property
    def runtime(self):
        return self.entry.runtime_data

    def feed(self, state):
        self.conn.on_state(state)

    def commands(self, name=None):
        return [cmd for cmd, _ in self.sent if name is None or cmd == name]

    def payload(self, name):
        return [p for cmd, p in self.sent if cmd == name][-1]

    def entity(self, domain, key):
        entity_id = er.async_get(self.hass).async_get_entity_id(domain, DOMAIN, f"{self.entry.entry_id}_{key}")
        assert entity_id, f"{domain} {key} not created"
        return entity_id

    async def call(self, service, data=None, response=False):
        return await self.hass.services.async_call(
            DOMAIN, service, data or {}, blocking=True, return_response=response
        )


@pytest.fixture
async def oven(hass: HomeAssistant, fake):
    hass.config.units = US_CUSTOMARY_SYSTEM  # the target temperature number shows and takes °F
    entry = await setup(hass)
    made = Oven(hass, entry, fake.made[0])
    made.feed(report("idle"))
    await hass.async_block_till_done()
    return made


def running(oven, stages=None):
    state = report("cook", cook_id="ios-running", stages=stages or NO_TIMER)
    oven.feed(dataclasses.replace(state, target_c=176.67))


# --- start_cook ---------------------------------------------------------------


async def test_start_cook_fahrenheit(oven):
    await oven.call("start_cook", {"temperature_f": 350, "minutes": 30, "elements": ["rear"]})
    payload = oven.payload("CMD_APO_START")
    stage = payload["stages"][0]
    assert stage["do"]["temperatureBulbs"]["dry"]["setpoint"]["celsius"] == 176.67
    assert stage["do"]["timer"]["initial"] == 1800


async def test_start_cook_celsius_fields(oven):
    """The old oven_steam script's fields, as written."""
    await oven.call(
        "start_cook",
        {
            "device_id": "40b4b3036440bf2f2d76da8bf0d0ebd8",
            "heating_rear": True,
            "target_temperature_celsius": 100,
            "target_humidity": 100,
            "timer": {"hours": 0, "minutes": 25, "seconds": 0},
            "timer_mode": "Immediately",
        },
    )
    stage = oven.payload("CMD_APO_START")["stages"][0]
    assert stage["do"]["temperatureBulbs"]["dry"]["setpoint"]["celsius"] == 100
    assert stage["do"]["steamGenerators"]["steamPercentage"]["setpoint"] == 100
    assert stage["do"]["timer"]["initial"] == 1500
    assert stage["do"]["heatingElements"]["rear"] == {"on": True}
    assert stage["do"]["heatingElements"]["top"] == {"on": False}


async def test_start_returns_cook_id(oven):
    result = await oven.call("start_cook", {"temperature_f": 350}, response=True)
    sent_id = oven.payload("CMD_APO_START")["cookId"]
    assert result == {"cook_id": sent_id}
    assert sent_id.startswith("ios-")


async def test_error_message_plain(oven):
    with pytest.raises(ServiceValidationError, match=r"cannot go above 480 F"):
        await oven.call("start_cook", {"temperature_f": 500})
    assert oven.commands("CMD_APO_START") == []


async def test_old_probe_script_says_not_tested(oven):
    """The miso salmon script: the probe target is not proven, and the answer says so."""
    with pytest.raises(ServiceValidationError, match="probe target is not tested"):
        await oven.call(
            "start_cook",
            {
                "heating_rear": True,
                "heating_top": True,
                "sous_vide": False,
                "target_humidity": 50,
                "target_temperature_celsius": 204.4,
                "temperature_probe_celsius": 52.8,
            },
        )
    assert oven.commands("CMD_APO_START") == []


@pytest.mark.parametrize("mode", ["When Food Detected", "When Food Removed"])
async def test_food_timer_modes_not_tested(oven, mode):
    with pytest.raises(ServiceValidationError, match="not been tested"):
        await oven.call("start_cook", {"temperature_f": 350, "minutes": 10, "timer_mode": mode})


async def test_timer_mode_when_preheated_and_manual(oven):
    await oven.call("start_cook", {"temperature_f": 350, "minutes": 10, "timer_mode": "Manually"})
    timer = oven.payload("CMD_APO_START")["stages"][0]["do"]["timer"]
    assert timer["entry"] == {"conditions": {"or": {"userAction": {"=": True}}}}


async def test_both_names_for_one_value_refused(oven):
    with pytest.raises(ServiceValidationError, match="once"):
        await oven.call("start_cook", {"temperature_f": 350, "target_temperature_fahrenheit": 350})


async def test_timer_and_minutes_refused(oven):
    with pytest.raises(ServiceValidationError, match="timer"):
        await oven.call("start_cook", {"temperature_f": 350, "minutes": 5, "timer": {"minutes": 5}})


async def test_timer_seconds_must_be_whole_minutes(oven):
    with pytest.raises(ServiceValidationError, match="whole"):
        await oven.call("start_cook", {"temperature_f": 350, "timer": {"minutes": 1, "seconds": 30}})


async def test_sous_vide_target_humidity_is_humidity(oven):
    await oven.call(
        "start_cook", {"target_temperature_celsius": 60, "sous_vide": True, "target_humidity": 100, "minutes": 20}
    )
    steam = oven.payload("CMD_APO_START")["stages"][0]["do"]["steamGenerators"]
    assert steam == {"mode": "relative-humidity", "relativeHumidity": {"setpoint": 100}}


async def test_max_minutes_reaches_the_runtime(oven):
    await oven.call("start_cook", {"temperature_f": 350, "max_minutes": 45})
    assert oven.runtime._max_minutes == 45


# Each andr83 start_cook field, with a value, and what it must become.
OLD_FIELDS = [
    ("target_temperature_celsius", 150, "temperature_c", 150.0),
    ("target_temperature_fahrenheit", 300, "temperature_f", 300.0),
    ("sous_vide", False, "sous_vide", False),
    ("heating_top", True, "elements", frozenset({"top"})),
    ("heating_bottom", True, "elements", frozenset({"bottom"})),
    ("heating_rear", True, "elements", frozenset({"rear"})),
    ("temperature_probe_celsius", 52.8, "probe_c", 52.8),
    ("temperature_probe_fahrenheit", 127, "probe_f", 127.0),
    ("target_humidity", 40, "steam_percent", 40),
    ("timer", {"hours": 1, "minutes": 5, "seconds": 0}, "minutes", 65),
    ("timer_mode", "Manually", "timer_mode", "Manually"),
]
NEW_FIELDS = [
    ("temperature_f", 300, "temperature_f", 300.0),
    ("temperature_c", 150, "temperature_c", 150.0),
    ("probe_f", 127, "probe_f", 127.0),
    ("probe_c", 52.8, "probe_c", 52.8),
    ("minutes", 12, "minutes", 12),
    ("steam_percent", 30, "steam_percent", 30),
    ("humidity", 80, "humidity", 80),
    ("elements", ["top", "rear"], "elements", frozenset({"top", "rear"})),
    ("fan", 70, "fan", 70),
    ("vent", "open-mid", "vent", "open-mid"),
    ("rack", 2, "rack", 2),
    ("max_minutes", 90, "max_minutes", 90),
]


@pytest.mark.parametrize(("name", "value", "attr", "expected"), OLD_FIELDS + NEW_FIELDS)
def test_every_old_start_option_present(name, value, attr, expected):
    data = services_module.START_COOK_SCHEMA({name: value})
    request = services_module.cook_request(data)
    assert getattr(request, attr) == expected


def test_device_id_accepted_and_ignored():
    data = services_module.START_COOK_SCHEMA({"device_id": "abc", "temperature_f": 350})
    assert services_module.cook_request(data).temperature_f == 350


def test_unknown_start_field_refused():
    with pytest.raises(vol.Invalid):
        services_module.START_COOK_SCHEMA({"temperature_f": 350, "no_such_field": 1})


def test_old_fields_are_in_services_yaml():
    text = (PACKAGE / "services.yaml").read_text()
    for name, *_ in OLD_FIELDS + NEW_FIELDS:
        if name != "device_id":
            assert f"\n    {name}:" in text, name


# --- start_custom_cook, stop_cook, the rest ----------------------------------


async def test_custom_cook_invalid_json_is_plain(oven):
    with pytest.raises(ServiceValidationError, match="not valid JSON"):
        await oven.call("start_custom_cook", {"config": "{not json"})
    assert oven.commands("CMD_APO_START") == []


async def test_custom_cook_sends_stages_and_returns_id(oven):
    stages = json.dumps(ONE_STAGE, indent=2)
    result = await oven.call("start_custom_cook", {"config": stages, "max_minutes": 20}, response=True)
    payload = oven.payload("CMD_APO_START")
    assert payload["stages"] == ONE_STAGE
    assert result == {"cook_id": payload["cookId"]}
    assert oven.runtime._max_minutes == 20


async def test_stop_cook_always_sent_while_idle(oven):
    await oven.call("stop_cook")
    assert oven.commands() == ["CMD_APO_STOP"]


async def test_stop_cook_with_other_cook_id_does_nothing(oven):
    running(oven)
    await oven.call("stop_cook", {"cook_id": "ios-a-later-cook"})
    assert oven.commands() == []
    await oven.call("stop_cook", {"cook_id": "ios-running"})
    assert oven.commands() == ["CMD_APO_STOP"]


async def test_update_cook_sends_change(oven):
    running(oven)
    await oven.call("update_cook", {"temperature_f": 400, "fan": 50})
    stage = oven.payload("CMD_APO_UPDATE_COOK_STAGE")
    assert stage["do"]["temperatureBulbs"]["dry"]["setpoint"]["celsius"] == 204.44
    assert stage["do"]["fan"]["speed"] == 50


async def test_update_cook_start_timer_and_timer(oven):
    running(oven, stages=ONE_STAGE)
    await oven.call("update_cook", {"start_timer": True, "timer": {"minutes": 20}})
    timer = oven.payload("CMD_APO_UPDATE_COOK_STAGE")["do"]["timer"]
    assert timer["initial"] == 1200 and "entry" not in timer


async def test_update_refused_field(oven):
    """A field this cook cannot take is a plain validation error; the oven refusing is a plain failure."""
    running(oven)
    with pytest.raises(ServiceValidationError, match="humidity"):
        await oven.call("update_cook", {"humidity": 50})
    assert oven.commands() == []
    oven.errors.append(OvenRefused("raw oven text 12345"))
    with pytest.raises(HomeAssistantError) as err:
        await oven.call("update_cook", {"fan": 50})
    assert "refused" in str(err.value) and "12345" not in str(err.value)


async def test_update_cook_nothing_to_change(oven):
    running(oven)
    with pytest.raises(ServiceValidationError, match="at least one"):
        await oven.call("update_cook", {})


async def test_update_cook_when_idle_is_plain(oven):
    with pytest.raises(HomeAssistantError, match="No cook is running"):
        await oven.call("update_cook", {"fan": 50})


async def test_next_stage(oven):
    await oven.call("next_stage", {"stage_id": "ios-stage-2"})
    assert oven.sent == [("CMD_APO_START_STAGE", {"stageId": "ios-stage-2"})]


async def test_update_stages(oven):
    await oven.call("update_stages", {"config": json.dumps(ONE_STAGE)})
    assert oven.sent == [("CMD_APO_UPDATE_COOK_STAGES", {"stages": ONE_STAGE})]


async def test_update_stages_over_cap_refused(oven):
    hot = json.loads(json.dumps(ONE_STAGE))
    hot[0]["do"]["temperatureBulbs"]["dry"]["setpoint"]["celsius"] = 300
    with pytest.raises(ServiceValidationError, match="480 F"):
        await oven.call("update_stages", {"config": json.dumps(hot)})
    assert oven.sent == []


@pytest.mark.parametrize(
    ("error", "words"),
    [
        (OvenRefused("secret-token-xyz"), "refused"),
        (OvenNoReply("x"), "did not answer"),
        (OvenOffline("wss://devices.anovaculinary.io?token=secret-token-xyz"), "cannot be reached"),
    ],
)
async def test_oven_errors_become_plain_home_assistant_errors(oven, error, words):
    oven.errors.append(error)
    with pytest.raises(HomeAssistantError) as err:
        await oven.call("next_stage", {"stage_id": "s"})
    assert not isinstance(err.value, ServiceValidationError)
    assert words in str(err.value)
    assert "secret" not in str(err.value) and "token" not in str(err.value)


async def test_actions_exist_without_an_entry_and_say_so(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    for service in SERVICES:
        assert hass.services.has_service(DOMAIN, service)
    with pytest.raises(ServiceValidationError, match="not set up"):
        await hass.services.async_call(DOMAIN, "stop_cook", {}, blocking=True)


async def test_actions_say_so_after_unload(hass, fake):
    entry = await setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(ServiceValidationError, match="not set up"):
        await hass.services.async_call(DOMAIN, "stop_cook", {}, blocking=True)
    assert hass.services.has_service(DOMAIN, "stop_cook")


def test_start_actions_return_a_response():
    from homeassistant.core import SupportsResponse

    assert services_module.RESPONSES == {
        "start_cook": SupportsResponse.OPTIONAL,
        "start_custom_cook": SupportsResponse.OPTIONAL,
    }


# --- entities ------------------------------------------------------------------


async def test_stop_button_sends_while_state_idle(oven):
    assert oven.runtime.state.mode == "idle"
    await oven.hass.services.async_call(
        "button", "press", {"entity_id": oven.entity("button", "stop")}, blocking=True
    )
    assert oven.commands() == ["CMD_APO_STOP"]


async def test_stop_button_stays_pressable_without_reports(oven):
    oven.conn.on_status(False)
    await oven.hass.async_block_till_done()
    state = oven.hass.states.get(oven.entity("button", "stop"))
    assert state.state != "unavailable"


async def test_number_sends_update(oven):
    running(oven)
    entity_id = oven.entity("number", "target_temperature")
    await oven.hass.async_block_till_done()
    assert float(oven.hass.states.get(entity_id).state) == pytest.approx(350, abs=0.1)
    await oven.hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": 400}, blocking=True)
    stage = oven.payload("CMD_APO_UPDATE_COOK_STAGE")
    assert stage["do"]["temperatureBulbs"]["dry"]["setpoint"]["celsius"] == 204.44


async def test_number_unavailable_idle(oven):
    entity_id = oven.entity("number", "target_temperature")
    assert oven.hass.states.get(entity_id).state == "unavailable"
    running(oven)
    await oven.hass.async_block_till_done()
    assert oven.hass.states.get(entity_id).state != "unavailable"
    oven.feed(report("idle"))
    await oven.hass.async_block_till_done()
    assert oven.hass.states.get(entity_id).state == "unavailable"


async def test_number_out_of_range_is_a_plain_error(oven):
    running(oven)
    entity_id = oven.entity("number", "target_temperature")
    with pytest.raises(ServiceValidationError):
        await oven.hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": 500}, blocking=True)
    assert oven.commands() == []


async def test_fan_number_works_idle(oven):
    entity_id = oven.entity("number", "fan")
    assert oven.hass.states.get(entity_id).state != "unavailable"
    await oven.hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": 60}, blocking=True)
    assert oven.sent == [("CMD_APO_SET_FAN", {"speed": 60})]


async def test_door_light_switch_follows_last_command(oven):
    entity_id = oven.entity("switch", "door_light")
    await oven.hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    assert oven.sent[-1] == ("CMD_APO_SET_LAMP", {"on": True})
    state = oven.hass.states.get(entity_id)
    assert state.state == "on" and state.attributes["assumed_state"] is True
    await oven.hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert oven.sent[-1] == ("CMD_APO_SET_LAMP", {"on": False})
    assert oven.hass.states.get(entity_id).state == "off"


async def test_failed_door_light_command_does_not_change_the_switch(oven):
    entity_id = oven.entity("switch", "door_light")
    oven.errors.append(OvenRefused("no"))
    with pytest.raises(HomeAssistantError):
        await oven.hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    assert oven.hass.states.get(entity_id).state != "on"


async def test_door_light_preference_switch(oven):
    entity_id = oven.entity("switch", "door_light_preference")
    await oven.hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert oven.sent == [("CMD_APO_SET_LAMP_PREFERENCE", {"on": False})]


async def test_display_unit_select(oven):
    entity_id = oven.entity("select", "display_unit")
    await oven.hass.services.async_call(
        "select", "select_option", {"entity_id": entity_id, "option": "fahrenheit"}, blocking=True
    )
    assert oven.sent == [("CMD_APO_SET_TEMPERATURE_UNIT", {"temperatureUnit": "F"})]
    await oven.hass.services.async_call(
        "select", "select_option", {"entity_id": entity_id, "option": "celsius"}, blocking=True
    )
    assert oven.sent[-1] == ("CMD_APO_SET_TEMPERATURE_UNIT", {"temperatureUnit": "C"})


# --- strings ---------------------------------------------------------------------


def test_services_have_names_and_descriptions():
    import yaml

    strings = json.loads((PACKAGE / "strings.json").read_text())
    english = json.loads((PACKAGE / "translations" / "en.json").read_text())
    assert strings == english
    yaml_services = yaml.safe_load((PACKAGE / "services.yaml").read_text())
    assert set(yaml_services) == set(SERVICES)
    for name, definition in yaml_services.items():
        text = strings["services"][name]
        assert text["name"] and text["description"]
        for field in definition.get("fields", {}):
            assert text["fields"][field]["name"] and text["fields"][field]["description"], (name, field)
        assert set(text.get("fields", {})) == set(definition.get("fields", {})), name


def test_fahrenheit_selectors_and_no_typos():
    import yaml

    text = (PACKAGE / "services.yaml").read_text()
    data = yaml.safe_load(text)
    start = data["start_cook"]["fields"]
    assert start["temperature_f"]["selector"]["number"]["unit_of_measurement"] == "°F"
    assert start["target_temperature_celsius"]["selector"]["number"]["unit_of_measurement"] == "°C"
    assert start["vent"]["selector"]["select"]["options"] == ["closed", "open-mid", "open-max"]
    assert start["timer_mode"]["selector"]["select"]["options"] == [
        "Immediately", "When Preheated", "Manually", "When Food Detected", "When Food Removed",
    ]  # fmt: skip
    assert start["fan"]["selector"]["number"]["min"] == 0 and start["fan"]["selector"]["number"]["max"] == 100
    strings = json.dumps(json.loads((PACKAGE / "strings.json").read_text())["services"])
    for typo in ("teh ", "recieve", "seperate", "temprature", "occured"):
        assert typo not in strings and typo not in text
    assert "  " not in strings


def test_control_names_spelled():
    words = set("stop target temperature fan door light preference display unit celsius fahrenheit".split())
    controls = json.loads((PACKAGE / "strings.json").read_text())["entity"]
    for domain in ("button", "number", "switch", "select"):
        for key, value in controls[domain].items():
            for word in value["name"].lower().split():
                assert word in words, f"{domain} {key}: {word!r}"
    assert controls["select"]["display_unit"]["state"] == {"celsius": "Celsius", "fahrenheit": "Fahrenheit"}
