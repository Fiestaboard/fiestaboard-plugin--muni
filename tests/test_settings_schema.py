"""The settings schema that drives the stop picker, and what it must not break."""

import json
from pathlib import Path

import pytest

from src.plugins.manifest import validate_manifest


_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "manifest.json"

# The keys ``ui:options`` accepted before FiestaBoard 8.24.2. Using any other
# key made load_manifest() return None, which uninstalls the plugin in
# practice rather than degrading the widget.
_PRE_8_24_2_UI_OPTIONS_KEYS = {"options_id", "depends_on", "multiple", "cache_seconds"}


@pytest.fixture
def manifest():
    return json.loads(_MANIFEST_PATH.read_text())


@pytest.fixture
def properties(manifest):
    return manifest["settings_schema"]["properties"]


class TestRemoteOptionsDeclaration:
    def test_stop_codes_is_a_remote_options_field(self, properties):
        stop_codes = properties["stop_codes"]

        assert stop_codes["ui:widget"] == "remote-options"
        assert stop_codes["ui:options"]["options_id"] == "stops"
        assert stop_codes["ui:options"]["multiple"] is True

    def test_stop_codes_depends_on_the_route_field(self, properties):
        assert properties["stop_codes"]["ui:options"]["depends_on"] == ["route"]
        assert properties["route"]["ui:widget"] == "remote-options"
        assert properties["route"]["ui:options"]["options_id"] == "routes"

    def test_stop_search_is_answered_by_the_plugin(self, properties):
        # 3,000-odd stops never all reach the browser, so the filter box has to
        # be handed to get_options rather than applied to a fetched page.
        assert properties["stop_codes"]["ui:options"]["server_search"] is True

    def test_manifest_passes_cores_validator(self, manifest):
        is_valid, errors = validate_manifest(manifest)

        assert (is_valid, errors) == (True, [])

    def test_core_floor_covers_every_ui_options_key_used(self, manifest):
        used_keys = set()
        for prop in manifest["settings_schema"]["properties"].values():
            used_keys |= set(prop.get("ui:options") or {})

        newer_keys = used_keys - _PRE_8_24_2_UI_OPTIONS_KEYS
        if not newer_keys:
            pytest.skip("manifest only uses the original four ui:options keys")

        floor = manifest["fiestaboard_version"]
        assert floor.startswith(">="), floor
        version = tuple(int(part) for part in floor[2:].split("."))
        assert version >= (8, 24, 2), (
            f"{sorted(newer_keys)} are rejected by cores older than 8.24.2, which would "
            f"refuse to load the whole plugin, but the manifest allows {floor}"
        )


class TestStoredConfigCompatibility:
    """Stop codes people already have saved must keep working untouched."""

    LEGACY_CONFIG = {
        "enabled": True,
        "api_key": "stored-key",
        "stop_codes": ["15726", "15419"],
        "refresh_seconds": 60,
    }

    def test_stop_codes_is_still_a_flat_array_of_strings(self, properties):
        stop_codes = properties["stop_codes"]

        assert stop_codes["type"] == "array"
        assert stop_codes["items"] == {"type": "string"}
        assert stop_codes["maxItems"] == 4

    def test_stop_codes_is_still_required(self, manifest):
        assert "stop_codes" in manifest["settings_schema"]["required"]

    def test_a_config_saved_before_the_picker_still_validates(self):
        from plugins.muni import MuniPlugin

        plugin = MuniPlugin({"id": "muni", "name": "SF Muni", "version": "1.0.0"})

        assert plugin.validate_config(self.LEGACY_CONFIG) == []

    def test_a_config_saved_before_the_picker_round_trips_unchanged(self):
        assert json.loads(json.dumps(self.LEGACY_CONFIG)) == self.LEGACY_CONFIG

    def test_the_route_filter_is_optional_so_old_configs_stay_valid(self, manifest):
        assert "route" not in manifest["settings_schema"]["required"]

    def test_the_picker_writes_the_shape_that_is_already_stored(self, monkeypatch):
        import requests

        from src.plugins.base import OptionsRequest
        from plugins.muni import MuniPlugin

        payload = {
            "Contents": {
                "dataObjects": {
                    "ScheduledStopPoint": [
                        {"id": "SF_15726", "Name": "Church St & Duboce Ave"},
                        {"id": "15419", "Name": "Judah St & 9th Ave"},
                    ]
                }
            }
        }

        class Response:
            text = json.dumps(payload)

            def raise_for_status(self):
                return None

        monkeypatch.setattr(requests, "get", lambda *a, **k: Response())

        plugin = MuniPlugin({"id": "muni", "name": "SF Muni", "version": "1.0.0"})
        plugin.config = dict(self.LEGACY_CONFIG)
        result = plugin.get_options(OptionsRequest(options_id="stops"))

        values = [option.value for option in result.options]
        assert all(isinstance(value, str) for value in values), values
        # A stop the user already has stored is offered back as the identical
        # scalar, so re-saving from the picker cannot rewrite their config.
        assert "15419" in values
