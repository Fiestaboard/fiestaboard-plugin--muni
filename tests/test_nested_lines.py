"""Tests for MuniPlugin's nested per-line data.

Templates reach this data as ``{{muni.stops.0.lines.N.formatted}}`` and
``{{muni.stops.0.all_lines.formatted}}``; the older flat variables
(``{{muni.line}}``, ``{{muni.formatted}}``) keep working because they mirror
the first line of the first stop. Rendering itself belongs to the platform's
template engine, which is not a plugin API, so these tests pin the *shape*
the plugin produces rather than render it.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from plugins.muni import MuniPlugin


def _visit(line, minutes, stop_name="Judah & 34th"):
    # Half a minute of slack: the plugin truncates, so a stamp made a few
    # milliseconds before the assertion would otherwise read one minute short.
    arrival = (datetime.now(timezone.utc) + timedelta(minutes=minutes, seconds=30)).isoformat()
    return {
        "MonitoredVehicleJourney": {
            "PublishedLineName": line,
            "MonitoredCall": {"StopPointName": stop_name, "ExpectedArrivalTime": arrival},
        }
    }


@pytest.fixture
def plugin():
    return MuniPlugin({"id": "muni", "name": "SF Muni", "version": "1.0.0"})


@pytest.fixture
def two_line_stop(plugin):
    """N arrivals at 5 and 15 minutes, a J arrival at 8, at one stop."""
    visits = [_visit("N", 5), _visit("J", 8), _visit("N", 15)]
    return plugin._parse_stop_data(visits, "15210")


class TestNestedLineStructure:
    def test_each_line_is_addressable_by_its_code(self, two_line_stop):
        lines = two_line_stop["lines"]
        assert set(lines) == {"N", "J"}
        assert lines["N"]["line"] == "N-JUDAH"
        assert lines["N"]["formatted"] == "N-JUDAH: 5, 15 MIN"
        assert lines["N"]["next_arrival"] == 5
        assert lines["J"]["formatted"] == "J-CHURCH: 8 MIN"
        assert lines["J"]["next_arrival"] == 8

    def test_all_lines_merges_every_line_in_arrival_order(self, two_line_stop):
        all_lines = two_line_stop["all_lines"]
        assert all_lines["formatted"] == "J-CHURCH/N-JUDAH: 5, 8, 15 MIN"
        assert all_lines["next_arrival"] == 5
        assert all_lines["is_delayed"] is False

    def test_flat_stop_variables_mirror_the_first_line(self, two_line_stop):
        """Line codes are sorted, so J comes before N here."""
        assert two_line_stop["line"] == "J-CHURCH"
        assert two_line_stop["formatted"] == two_line_stop["lines"]["J"]["formatted"]
        assert two_line_stop["is_delayed"] is False

    def test_flat_top_level_variables_mirror_the_first_stop(self, plugin):
        plugin.config = {"api_key": "k", "stop_codes": ["15210"]}
        cache = Mock()
        cache.is_ready.return_value = True
        cache.get_stops_data.return_value = {"15210": [_visit("N", 5), _visit("N", 15)]}
        with patch.object(plugin, "_get_transit_cache", return_value=cache):
            data = plugin.fetch_data().data

        stop = data["stops"][0]
        assert data["line"] == stop["line"] == "N-JUDAH"
        assert data["formatted"] == stop["formatted"] == stop["lines"]["N"]["formatted"]
        assert data["formatted"] == "N-JUDAH: 5, 15 MIN"
