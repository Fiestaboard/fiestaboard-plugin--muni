"""Tests for MuniPlugin multi-stop support.

Every configured stop gets its own entry under ``stops``, the first one doubles
as the top-level "primary" stop, and the list is capped at four.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from plugins.muni import MuniPlugin


def _visit(stop_name, minutes=5, line="N"):
    """One 511 StopMonitoring visit, as the transit cache hands it to the plugin."""
    # Half a minute of slack: the plugin truncates, so a stamp made a few
    # milliseconds before the assertion would otherwise read one minute short.
    arrival = (datetime.now(timezone.utc) + timedelta(minutes=minutes, seconds=30)).isoformat()
    return {
        "MonitoredVehicleJourney": {
            "PublishedLineName": line,
            "MonitoredCall": {"StopPointName": stop_name, "ExpectedArrivalTime": arrival},
        }
    }


def _ready_cache(stops):
    cache = Mock()
    cache.is_ready.return_value = True
    cache.get_stops_data.return_value = stops
    return cache


@pytest.fixture
def plugin():
    return MuniPlugin({"id": "muni", "name": "SF Muni", "version": "1.0.0"})


class TestMuniMultiStop:
    def test_each_configured_stop_gets_its_own_entry(self, plugin):
        plugin.config = {"api_key": "k", "stop_codes": ["15726", "15727", "15728"]}
        cache = _ready_cache(
            {
                "15726": [_visit("Stop A")],
                "15727": [_visit("Stop B")],
                "15728": [_visit("Stop C")],
            }
        )
        with patch.object(plugin, "_get_transit_cache", return_value=cache):
            result = plugin.fetch_data()

        assert result.available
        assert result.data["stop_count"] == 3
        assert [s["stop_code"] for s in result.data["stops"]] == ["15726", "15727", "15728"]
        assert [s["stop_name"] for s in result.data["stops"]] == ["Stop A", "Stop B", "Stop C"]

    def test_first_configured_stop_is_the_primary(self, plugin):
        """The legacy top-level variables mirror ``stops[0]``."""
        plugin.config = {"api_key": "k", "stop_codes": ["15726", "15727"]}
        cache = _ready_cache({"15726": [_visit("Stop A", 4)], "15727": [_visit("Stop B", 9)]})
        with patch.object(plugin, "_get_transit_cache", return_value=cache):
            data = plugin.fetch_data().data

        primary = data["stops"][0]
        assert data["stop_code"] == primary["stop_code"] == "15726"
        assert data["stop_name"] == primary["stop_name"] == "Stop A"
        assert data["formatted"] == primary["formatted"] == "N-JUDAH: 4 MIN"

    def test_only_the_first_four_stops_are_reported(self, plugin):
        codes = ["1", "2", "3", "4", "5"]
        plugin.config = {"api_key": "k", "stop_codes": codes}
        cache = _ready_cache({code: [_visit(f"Stop {code}")] for code in codes})
        with patch.object(plugin, "_get_transit_cache", return_value=cache):
            data = plugin.fetch_data().data

        assert data["stop_count"] == 4
        assert [s["stop_code"] for s in data["stops"]] == ["1", "2", "3", "4"]

    def test_a_stop_without_arrivals_is_skipped_not_fatal(self, plugin):
        plugin.config = {"api_key": "k", "stop_codes": ["15726", "15727"]}
        cache = _ready_cache({"15726": [], "15727": [_visit("Stop B")]})
        with patch.object(plugin, "_get_transit_cache", return_value=cache):
            result = plugin.fetch_data()

        assert result.available
        assert result.data["stop_count"] == 1
        assert result.data["stop_code"] == "15727"

    def test_no_stop_codes_is_reported_before_touching_the_cache(self, plugin):
        plugin.config = {"api_key": "k", "stop_codes": []}
        with patch.object(plugin, "_get_transit_cache") as get_cache:
            result = plugin.fetch_data()

        assert not result.available
        assert result.error == "No stop codes configured"
        get_cache.assert_not_called()
