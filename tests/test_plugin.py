"""Tests for MuniPlugin — this repository's plugin class.

These tests exercise ``__init__.py`` in this repo (imported as ``plugins.muni``
via the symlink CI creates). They deliberately do *not* import
``src.utils.muni`` or ``MessageFormatter.format_muni``: those were the
platform's pre-extraction copies of this logic, FiestaBoard has deleted them,
and testing them here proved nothing about this repository.
"""

import json
from pathlib import Path

import pytest
from unittest.mock import Mock, patch
from datetime import datetime, timezone, timedelta

from plugins.muni import COLOR_RED, MuniPlugin, Plugin


MANIFEST = {"id": "muni", "name": "SF Muni", "version": "1.0.0"}
RED_TILE = f"{{{COLOR_RED}}}"


def _visit(line="N", minutes=5, stop_name="Church St & Duboce Ave", occupancy="MANY_SEATS", **journey):
    """One 511 StopMonitoring visit, as the transit cache hands it to the plugin."""
    # Half a minute of slack: the plugin truncates, so a stamp made a few
    # milliseconds before the assertion would otherwise read one minute short.
    arrival = (datetime.now(timezone.utc) + timedelta(minutes=minutes, seconds=30)).isoformat()
    return {
        "MonitoredVehicleJourney": {
            "PublishedLineName": line,
            "Occupancy": occupancy,
            "MonitoredCall": {"StopPointName": stop_name, "ExpectedArrivalTime": arrival},
            **journey,
        }
    }


def _ready_cache(stops):
    """A transit cache that is up and answers ``get_stops_data`` with ``stops``."""
    cache = Mock()
    cache.is_ready.return_value = True
    cache.get_stops_data.return_value = stops
    return cache


class TestPluginIdentity:
    def test_module_exports_plugin_alias(self):
        """The loader imports the module and looks for ``Plugin``."""
        assert Plugin is MuniPlugin


class TestParseStopData:
    """What one stop's visits turn into -- including the board text itself.

    The ``formatted`` strings are what templates put on the board, so their
    exact shape is the contract here.
    """

    @pytest.fixture
    def plugin(self):
        return MuniPlugin(MANIFEST)

    def test_formats_line_name_and_sorted_minutes(self, plugin):
        visits = [_visit(minutes=12), _visit(minutes=4), _visit(minutes=19)]
        result = plugin._parse_stop_data(visits, "15726")
        assert result["formatted"] == "N-JUDAH: 4, 12, 19 MIN"
        assert result["lines"]["N"]["next_arrival"] == 4

    def test_keeps_only_the_next_three_arrivals(self, plugin):
        visits = [_visit(minutes=m) for m in (2, 5, 8, 11)]
        result = plugin._parse_stop_data(visits, "15726")
        assert result["formatted"] == "N-JUDAH: 2, 5, 8 MIN"

    def test_on_time_service_carries_no_red_tile(self, plugin):
        result = plugin._parse_stop_data([_visit(minutes=4), _visit(minutes=12)], "15726")
        assert result["is_delayed"] is False
        assert result["all_lines"]["is_delayed"] is False
        assert RED_TILE not in result["formatted"]
        assert RED_TILE not in result["all_lines"]["formatted"]

    def test_delay_prefixes_the_red_tile(self, plugin):
        visits = [_visit(minutes=7, Delay="PT5M"), _visit(minutes=15)]
        result = plugin._parse_stop_data(visits, "15726")
        assert result["is_delayed"] is True
        assert result["formatted"] == f"{RED_TILE}N-JUDAH: 7, 15 MIN"
        assert result["all_lines"]["formatted"] == f"{RED_TILE}N-JUDAH: 7, 15 MIN"

    def test_a_situation_ref_counts_as_a_delay(self, plugin):
        result = plugin._parse_stop_data([_visit(SituationRef={"SituationSimpleRef": "x"})], "15726")
        assert result["is_delayed"] is True

    def test_a_full_train_is_still_an_arrival(self, plugin):
        visits = [_visit(minutes=3, occupancy="FULL"), _visit(minutes=10, occupancy="FEW_SEATS")]
        result = plugin._parse_stop_data(visits, "15726")
        assert result["formatted"] == "N-JUDAH: 3, 10 MIN"
        assert result["all_lines"]["next_arrival"] == 3

    def test_stop_name_comes_from_the_visits(self, plugin):
        result = plugin._parse_stop_data([_visit(stop_name="Judah St & 9th Ave")], "15726")
        assert result["stop_name"] == "Judah St & 9th Ave"

    def test_stop_name_falls_back_to_the_stop_code(self, plugin):
        result = plugin._parse_stop_data([_visit(stop_name="")], "15726")
        assert result["stop_name"] == "15726"

    def test_unwraps_list_valued_fields(self, plugin):
        """511 sometimes wraps scalar fields in one-element lists."""
        visits = [_visit(line=["N"], occupancy=["MANY_SEATS"], stop_name=["Church St & Duboce Ave"])]
        result = plugin._parse_stop_data(visits, "15726")
        assert result["line"] == "N-JUDAH"
        assert result["stop_name"] == "Church St & Duboce Ave"

    def test_falls_back_to_departure_then_aimed_time(self, plugin):
        soon = (datetime.now(timezone.utc) + timedelta(minutes=6, seconds=30)).isoformat()
        later = (datetime.now(timezone.utc) + timedelta(minutes=9, seconds=30)).isoformat()
        visits = [
            {
                "MonitoredVehicleJourney": {
                    "PublishedLineName": "N",
                    "MonitoredCall": {"StopPointName": "Test", "ExpectedDepartureTime": soon},
                }
            },
            {
                "MonitoredVehicleJourney": {
                    "PublishedLineName": "N",
                    "MonitoredCall": {"StopPointName": "Test", "AimedArrivalTime": later},
                }
            },
        ]
        result = plugin._parse_stop_data(visits, "15726")
        assert result["formatted"] == "N-JUDAH: 6, 9 MIN"

    def test_a_visit_with_an_unreadable_time_is_dropped(self, plugin):
        visits = [_visit(minutes=4)]
        visits[0]["MonitoredVehicleJourney"]["MonitoredCall"]["ExpectedArrivalTime"] = "not a time"
        assert plugin._parse_stop_data(visits, "15726") is None


class TestTransitCacheWiring:
    """How the plugin configures and uses the platform's shared transit cache.

    ``_get_transit_cache`` imports ``get_transit_cache`` lazily, so the patch
    target is the source module rather than ``plugins.muni``.
    """

    CACHE_FACTORY = "src.utils.transit_cache.get_transit_cache"

    @pytest.fixture
    def plugin(self):
        plugin = MuniPlugin(MANIFEST)
        plugin.config = {"api_key": "test_key", "stop_codes": ["15726"]}
        return plugin

    def test_configures_the_cache_with_the_api_key(self, plugin):
        cache = _ready_cache({})
        with patch(self.CACHE_FACTORY, return_value=cache):
            plugin.fetch_data()
        cache.configure.assert_called_once_with(api_key="test_key", refresh_interval=90, enabled=True)

    def test_refresh_seconds_sets_the_cache_refresh_interval(self, plugin):
        plugin.config = {"api_key": "test_key", "stop_codes": ["15726"], "refresh_seconds": 120}
        cache = _ready_cache({})
        with patch(self.CACHE_FACTORY, return_value=cache):
            plugin.fetch_data()
        assert cache.configure.call_args.kwargs["refresh_interval"] == 120

    def test_starts_a_cache_that_is_not_ready(self, plugin):
        cache = Mock()
        cache.is_ready.return_value = False
        with patch(self.CACHE_FACTORY, return_value=cache):
            result = plugin.fetch_data()
        cache.start.assert_called_once_with()
        assert not result.available
        assert result.error == "Transit cache not ready"

    def test_does_not_restart_a_ready_cache(self, plugin):
        cache = _ready_cache({})
        with patch(self.CACHE_FACTORY, return_value=cache):
            plugin.fetch_data()
        cache.start.assert_not_called()

    def test_reuses_the_cache_across_fetches(self, plugin):
        cache = _ready_cache({})
        with patch(self.CACHE_FACTORY, return_value=cache) as factory:
            plugin.fetch_data()
            plugin.fetch_data()
        assert factory.call_count == 1
        assert cache.configure.call_count == 1

    def test_cache_setup_failure_is_reported_not_raised(self, plugin):
        with patch(self.CACHE_FACTORY, side_effect=RuntimeError("no 511 credentials")):
            result = plugin.fetch_data()
        assert not result.available
        assert result.error == "Transit cache not available"

    def test_asks_the_cache_for_the_configured_stops(self, plugin):
        cache = _ready_cache({"15726": [_visit()]})
        with patch(self.CACHE_FACTORY, return_value=cache):
            plugin.fetch_data()
        cache.get_stops_data.assert_called_once_with("SF", ["15726"])

    def test_arrivals_from_the_cache_reach_the_result(self, plugin):
        cache = _ready_cache({"15726": [_visit(stop_name="Test Stop", minutes=5)]})
        with patch(self.CACHE_FACTORY, return_value=cache):
            result = plugin.fetch_data()
        assert result.available
        assert result.data["line"] == "N-JUDAH"
        assert result.data["stop_name"] == "Test Stop"
        assert result.data["formatted"] == "N-JUDAH: 5 MIN"


class TestMuniPluginClass:
    """Tests for MuniPlugin class (plugins/muni/__init__.py)."""

    @pytest.fixture
    def plugin(self):
        from plugins.muni import MuniPlugin
        manifest = {"id": "muni", "name": "SF Muni", "version": "1.0.0"}
        return MuniPlugin(manifest)

    def test_plugin_id(self, plugin):
        assert plugin.plugin_id == "muni"

    def test_validate_config_valid(self, plugin):
        assert plugin.validate_config({"api_key": "k", "stop_codes": ["1"]}) == []

    def test_validate_config_missing_key(self, plugin):
        errors = plugin.validate_config({"stop_codes": ["1"]})
        assert len(errors) == 1

    def test_validate_config_missing_stops(self, plugin):
        errors = plugin.validate_config({"api_key": "k"})
        assert len(errors) == 1

    def test_validate_config_empty(self, plugin):
        errors = plugin.validate_config({})
        assert len(errors) == 2

    def test_normalize_line_code_known(self, plugin):
        assert plugin._normalize_line_code("JUDAH") == "N"
        assert plugin._normalize_line_code("N-JUDAH") == "N"
        assert plugin._normalize_line_code("CHURCH") == "J"
        assert plugin._normalize_line_code("TARAVAL") == "L"
        assert plugin._normalize_line_code("OCEAN VIEW") == "M"
        assert plugin._normalize_line_code("THIRD") == "T"
        assert plugin._normalize_line_code("SHUTTLE") == "S"
        assert plugin._normalize_line_code("MARKET") == "F"

    def test_normalize_line_code_single_letter(self, plugin):
        assert plugin._normalize_line_code("N") == "N"
        assert plugin._normalize_line_code("J") == "J"

    def test_normalize_line_code_dash(self, plugin):
        assert plugin._normalize_line_code("KT-OTHER") == "KT"

    def test_normalize_line_code_unknown(self, plugin):
        assert plugin._normalize_line_code("XY") == "XY"

    @pytest.mark.parametrize(
        "code,name",
        [
            ("N", "N-JUDAH"),
            ("J", "J-CHURCH"),
            ("K", "K-INGLESIDE"),
            ("L", "L-TARAVAL"),
            ("M", "M-OCEAN VIEW"),
            ("T", "T-THIRD"),
            ("S", "S-SHUTTLE"),
            ("F", "F-MARKET"),
            ("n", "N-JUDAH"),
            ("Z", "Z"),
        ],
    )
    def test_get_display_line_name(self, plugin, code, name):
        assert plugin._get_display_line_name(code) == name

    def test_calculate_minutes_until_future(self, plugin):
        ts = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        assert 9 <= plugin._calculate_minutes_until(ts) <= 11

    def test_calculate_minutes_until_past(self, plugin):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        assert plugin._calculate_minutes_until(ts) == 0

    def test_calculate_minutes_until_z_suffix(self, plugin):
        future = datetime.now(timezone.utc) + timedelta(minutes=15)
        ts = future.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert 14 <= plugin._calculate_minutes_until(ts) <= 16

    def test_calculate_minutes_until_invalid(self, plugin):
        assert plugin._calculate_minutes_until("bad") is None

    def test_parse_stop_data_empty(self, plugin):
        assert plugin._parse_stop_data([], "123") is None

    def test_parse_stop_data_basic(self, plugin):
        now = datetime.now(timezone.utc)
        visits = [
            {
                "MonitoredVehicleJourney": {
                    "PublishedLineName": "N",
                    "Occupancy": "MANY_SEATS",
                    "MonitoredCall": {
                        "StopPointName": "Church St & Duboce",
                        "ExpectedArrivalTime": (now + timedelta(minutes=5)).isoformat(),
                    }
                }
            },
            {
                "MonitoredVehicleJourney": {
                    "PublishedLineName": "N",
                    "Occupancy": "FEW_SEATS",
                    "MonitoredCall": {
                        "StopPointName": "Church St & Duboce",
                        "ExpectedArrivalTime": (now + timedelta(minutes=12)).isoformat(),
                    }
                }
            },
        ]
        result = plugin._parse_stop_data(visits, "15726")
        assert result is not None
        assert result["stop_code"] == "15726"
        assert "N" in result["lines"]
        assert result["lines"]["N"]["line"] == "N-JUDAH"
        assert result["all_lines"]["next_arrival"] is not None
        assert result["line"] == "N-JUDAH"

    def test_parse_stop_data_list_fields(self, plugin):
        now = datetime.now(timezone.utc)
        visits = [{
            "MonitoredVehicleJourney": {
                "PublishedLineName": ["N"],
                "Occupancy": None,
                "MonitoredCall": {
                    "StopPointName": ["Test Stop"],
                    "ExpectedArrivalTime": (now + timedelta(minutes=5)).isoformat(),
                }
            }
        }]
        result = plugin._parse_stop_data(visits, "15726")
        assert result["line"] == "N-JUDAH"
        assert result["stop_name"] == "Test Stop"

    def test_parse_stop_data_delayed(self, plugin):
        now = datetime.now(timezone.utc)
        visits = [{
            "MonitoredVehicleJourney": {
                "PublishedLineName": "N",
                "Delay": "PT5M",
                "MonitoredCall": {
                    "StopPointName": "Test",
                    "ExpectedArrivalTime": (now + timedelta(minutes=5)).isoformat(),
                }
            }
        }]
        result = plugin._parse_stop_data(visits, "15726")
        assert result is not None
        assert result["is_delayed"] is True
        assert "{63}" in result["formatted"]

    def test_parse_stop_data_no_published_line(self, plugin):
        now = datetime.now(timezone.utc)
        visits = [{
            "MonitoredVehicleJourney": {
                "MonitoredCall": {
                    "StopPointName": "Test",
                    "ExpectedArrivalTime": (now + timedelta(minutes=5)).isoformat(),
                }
            }
        }]
        assert plugin._parse_stop_data(visits, "15726") is None

    def test_fetch_data_no_stops(self, plugin):
        plugin._config = {}
        result = plugin.fetch_data()
        assert not result.available

    def test_fetch_data_no_cache(self, plugin):
        plugin._config = {"api_key": "k", "stop_codes": ["1"]}
        with patch.object(plugin, '_get_transit_cache', return_value=None):
            result = plugin.fetch_data()
            assert not result.available

    def test_fetch_data_cache_not_ready(self, plugin):
        plugin._config = {"api_key": "k", "stop_codes": ["1"]}
        mock_cache = Mock()
        mock_cache.is_ready.return_value = False
        with patch.object(plugin, '_get_transit_cache', return_value=mock_cache):
            result = plugin.fetch_data()
            assert not result.available

    def test_fetch_data_no_arrivals(self, plugin):
        plugin._config = {"api_key": "k", "stop_codes": ["1"]}
        mock_cache = Mock()
        mock_cache.is_ready.return_value = True
        mock_cache.get_stops_data.return_value = {"1": []}
        with patch.object(plugin, '_get_transit_cache', return_value=mock_cache):
            result = plugin.fetch_data()
            assert result.available
            assert result.data["formatted"] == "NO ARRIVALS"

    def test_fetch_data_success(self, plugin):
        plugin._config = {"api_key": "k", "stop_codes": ["15726"]}
        now = datetime.now(timezone.utc)
        visits = [{
            "MonitoredVehicleJourney": {
                "PublishedLineName": "N",
                "Occupancy": "MANY_SEATS",
                "MonitoredCall": {
                    "StopPointName": "Test Stop",
                    "ExpectedArrivalTime": (now + timedelta(minutes=5)).isoformat(),
                }
            }
        }]
        mock_cache = Mock()
        mock_cache.is_ready.return_value = True
        mock_cache.get_stops_data.return_value = {"15726": visits}
        with patch.object(plugin, '_get_transit_cache', return_value=mock_cache):
            result = plugin.fetch_data()
            assert result.available
            assert result.data["stop_count"] == 1
            assert result.data["stop_name"] == "Test Stop"

    def test_fetch_data_exception(self, plugin):
        plugin._config = {"api_key": "k", "stop_codes": ["1"]}
        mock_cache = Mock()
        mock_cache.is_ready.return_value = True
        mock_cache.get_stops_data.side_effect = RuntimeError("fail")
        with patch.object(plugin, '_get_transit_cache', return_value=mock_cache):
            result = plugin.fetch_data()
            assert not result.available

    def test_cleanup(self, plugin):
        plugin._cache = {"a": 1}
        plugin._transit_cache = "something"
        plugin.cleanup()
        assert plugin._cache is None
        assert plugin._transit_cache is None

    def test_config_change_reconfigures_transit_cache(self, plugin):
        """Test a config change makes the next lookup reconfigure the shared cache."""
        mock_cache = Mock()
        mock_cache.is_ready.return_value = True

        with patch('src.utils.transit_cache.get_transit_cache', return_value=mock_cache):
            plugin.config = {"api_key": "old_key", "stop_codes": ["12345"]}
            plugin._get_transit_cache()
            assert mock_cache.configure.call_count == 1

            # New credential: the memoized reference must not survive
            plugin.config = {"api_key": "new_key", "stop_codes": ["12345"]}
            assert plugin._transit_cache is None

            plugin._get_transit_cache()
            assert mock_cache.configure.call_count == 2
            assert mock_cache.configure.call_args.kwargs["api_key"] == "new_key"


class TestManifestMetadata:
    """Validate manifest.json rich variable metadata."""

    @pytest.fixture(autouse=True)
    def load_manifest(self):
        manifest_path = Path(__file__).resolve().parent.parent / "manifest.json"
        with open(manifest_path) as f:
            self.manifest = json.load(f)
        self.variables = self.manifest["variables"]

    def test_required_top_level_fields(self):
        for field in ("id", "name", "version", "variables"):
            assert field in self.manifest, f"Missing top-level field: {field}"

    def test_simple_is_dict(self):
        assert isinstance(self.variables["simple"], dict), (
            "variables.simple must be a dict, not a list"
        )

    def test_simple_vars_have_metadata(self):
        required_keys = {"description", "type", "example"}
        for var_name, meta in self.variables["simple"].items():
            missing = required_keys - set(meta.keys())
            assert not missing, (
                f"Simple var '{var_name}' missing keys: {missing}"
            )

    def test_simple_vars_have_group(self):
        groups = self.variables.get("groups", {})
        for var_name, meta in self.variables["simple"].items():
            grp = meta.get("group")
            assert grp, f"Simple var '{var_name}' has no group"
            assert grp in groups, (
                f"Simple var '{var_name}' references unknown group '{grp}'"
            )

    def test_groups_defined(self):
        groups = self.variables.get("groups", {})
        assert len(groups) >= 1, "At least one group must be defined"
        for gid, gmeta in groups.items():
            assert "label" in gmeta, f"Group '{gid}' missing 'label'"

    def test_arrays_preserved(self):
        arrays = self.variables.get("arrays", {})
        assert "stops" in arrays
        stops = arrays["stops"]
        assert "item_fields" in stops
        assert "label_field" in stops

    def test_sub_arrays_preserved(self):
        stops = self.variables["arrays"]["stops"]
        sub = stops.get("sub_arrays", {})
        assert "lines" in sub, "sub_arrays.lines must be present"
        lines = sub["lines"]
        assert lines["key_type"] == "dynamic"
        assert "key_field" in lines
        assert "item_fields" in lines

    def test_max_lengths_use_dot_star_notation(self):
        ml = self.manifest.get("max_lengths", {})
        assert ml, "max_lengths must be present"
        for key in ml:
            assert "." in key, (
                f"max_lengths key '{key}' should use dot-star notation"
            )

