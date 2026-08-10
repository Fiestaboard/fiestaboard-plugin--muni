"""Tests for the Muni stop picker (``remote-options`` provider).

These cover ``MuniPlugin.get_options`` -- the catalog browser the settings
form calls while the dialog is open -- not ``fetch_data``.
"""

import json

import pytest
import requests

from src.plugins.base import Option, OptionsRequest, OptionsResult, OptionsUnavailable


STOPS_JSON = {
    "Contents": {
        "dataObjects": {
            "ScheduledStopPoint": [
                {
                    "id": "SF_15726",
                    "Name": "Church St & Duboce Ave",
                    "Location": {"Latitude": "37.7695", "Longitude": "-122.4290"},
                    "StopType": "onstreetTram",
                },
                {
                    "id": "SF_13220",
                    "Name": "Powell St & Market St",
                    "Location": {"Latitude": "37.7845", "Longitude": "-122.4079"},
                    "StopType": "onstreetBus",
                },
                {
                    "id": "SF_16636",
                    "Name": "Judah St & 19th Ave",
                    "Location": {"Latitude": "37.7608", "Longitude": "-122.4759"},
                    "StopType": "onstreetTram",
                },
            ]
        }
    }
}


LINES_JSON = {
    "content": [
        {
            "Id": "N",
            "Name": "JUDAH",
            "TransportMode": "tram",
            "PublicCode": "N",
            "Monitored": "true",
            "OperatorRef": "SF",
        },
        {
            "Id": "38",
            "Name": "GEARY",
            "TransportMode": "bus",
            "PublicCode": "38",
            "Monitored": "true",
            "OperatorRef": "SF",
        },
    ]
}


@pytest.fixture(autouse=True)
def empty_catalog_cache():
    """The catalog cache is module-level on purpose; keep tests independent."""
    import plugins.muni

    plugins.muni._catalog_cache.clear()
    yield
    plugins.muni._catalog_cache.clear()


def make_plugin(**config):
    """Build a MuniPlugin with *config* applied, as core's throwaway instance does."""
    from plugins.muni import MuniPlugin

    plugin = MuniPlugin({"id": "muni", "name": "SF Muni", "version": "1.0.0"})
    plugin.config = config
    return plugin


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.text = json.dumps(payload)
        self.status_code = status_code

    def raise_for_status(self):
        return None


class TestStopOptions:
    """The ``stops`` catalog offered to the picker."""

    def test_returns_whole_stop_catalog_not_just_configured_stops(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append((url, params, timeout))
            return FakeResponse(STOPS_JSON)

        monkeypatch.setattr(requests, "get", fake_get)

        plugin = make_plugin(api_key="key", stop_codes=["15726"])
        result = plugin.get_options(OptionsRequest(options_id="stops"))

        values = [option.value for option in result.options]
        assert sorted(values) == ["13220", "15726", "16636"], (
            f"expected every stop in the catalog, not the configured one, got {values}"
        )
        labels = {option.value: option.label for option in result.options}
        assert labels["15726"] == "Church St & Duboce Ave"

    def test_missing_api_key_raises_options_unavailable(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("get_options called 511.org without an API key")

        monkeypatch.setattr(requests, "get", refuse)

        plugin = make_plugin(stop_codes=["15726"])

        with pytest.raises(OptionsUnavailable) as excinfo:
            plugin.get_options(OptionsRequest(options_id="stops"))

        assert "511" in str(excinfo.value)

    def test_upstream_failure_becomes_options_unavailable(self, monkeypatch):
        def explode(*args, **kwargs):
            raise requests.exceptions.ConnectionError("no route to host")

        monkeypatch.setattr(requests, "get", explode)

        plugin = make_plugin(api_key="key")

        with pytest.raises(OptionsUnavailable):
            plugin.get_options(OptionsRequest(options_id="stops"))

    def test_rejected_api_key_becomes_options_unavailable(self, monkeypatch):
        def unauthorized(*args, **kwargs):
            response = requests.Response()
            response.status_code = 401
            raise requests.exceptions.HTTPError("401 Unauthorized", response=response)

        monkeypatch.setattr(requests, "get", unauthorized)

        plugin = make_plugin(api_key="wrong")

        with pytest.raises(OptionsUnavailable) as excinfo:
            plugin.get_options(OptionsRequest(options_id="stops"))

        assert "key" in str(excinfo.value).lower()

    def test_query_filters_stops_case_insensitively(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(STOPS_JSON))

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="stops", query="cHuRcH"))

        assert [option.value for option in result.options] == ["15726"]

    def test_query_also_matches_a_stop_code(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(STOPS_JSON))

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="stops", query="13220"))

        assert [option.value for option in result.options] == ["13220"]

    def test_limit_caps_the_page_and_reports_has_more_and_total(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(STOPS_JSON))

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="stops", limit=2))

        assert len(result.options) == 2
        assert result.has_more is True
        assert result.total == 3

    def test_limit_larger_than_the_catalog_reports_no_more(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(STOPS_JSON))

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="stops", limit=50))

        assert len(result.options) == 3
        assert result.has_more is False
        assert result.total == 3

    def test_total_counts_matches_not_the_whole_catalog_when_querying(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(STOPS_JSON))

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="stops", query="ave", limit=1))

        assert result.total == 2
        assert result.has_more is True


TWIN_STOPS_JSON = {
    "Contents": {
        "dataObjects": {
            "ScheduledStopPoint": [
                {
                    "id": "SF_15726",
                    "Name": "Church St & Duboce Ave",
                    "Location": {"Latitude": "37.7699", "Longitude": "-122.4290"},
                },
                {
                    "id": "SF_15727",
                    "Name": "Church St & Duboce Ave",
                    "Location": {"Latitude": "37.7691", "Longitude": "-122.4290"},
                },
            ]
        }
    }
}


class TestOptionLabelling:
    """Same-named stops on opposite corners have to be told apart."""

    def test_description_carries_the_stop_code(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(STOPS_JSON))

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="stops", query="church"))

        assert result.options[0].description == "Stop 15726"

    def test_twin_stops_are_separated_by_which_side_they_sit_on(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(TWIN_STOPS_JSON))

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="stops"))

        descriptions = {option.value: option.description for option in result.options}
        assert descriptions["15726"] == "Stop 15726 · north side"
        assert descriptions["15727"] == "Stop 15727 · south side"

    def test_stops_are_grouped_by_their_primary_street(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(STOPS_JSON))

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="stops"))

        groups = {option.value: option.group for option in result.options}
        assert groups["15726"] == "Church St"
        assert groups["13220"] == "Powell St"

    def test_stops_are_offered_in_alphabetical_order(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(STOPS_JSON))

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="stops"))

        assert [option.label for option in result.options] == [
            "Church St & Duboce Ave",
            "Judah St & 19th Ave",
            "Powell St & Market St",
        ]


class TestRouteScopedStops:
    """``depends_on: route`` narrows the stop list to one line."""

    STOPS_BY_LINE = {
        "N": {
            "Contents": {
                "dataObjects": {
                    "ScheduledStopPoint": [
                        {"id": "SF_15726", "Name": "Church St & Duboce Ave"},
                        {"id": "SF_16636", "Name": "Judah St & 19th Ave"},
                    ]
                }
            }
        },
        "38": {
            "Contents": {
                "dataObjects": {
                    "ScheduledStopPoint": [
                        {"id": "SF_13220", "Name": "Geary Blvd & Park Presidio Blvd"},
                    ]
                }
            }
        },
    }

    @pytest.fixture
    def by_line(self, monkeypatch):
        seen = []

        def fake_get(url, params=None, timeout=None):
            seen.append(params)
            line_id = (params or {}).get("line_id")
            if line_id is None:
                return FakeResponse(STOPS_JSON)
            return FakeResponse(self.STOPS_BY_LINE[line_id])

        monkeypatch.setattr(requests, "get", fake_get)
        return seen

    def test_each_route_returns_only_its_own_stops(self, by_line):
        plugin = make_plugin(api_key="key")

        judah = plugin.get_options(OptionsRequest(options_id="stops", parent={"route": "N"}))
        geary = plugin.get_options(OptionsRequest(options_id="stops", parent={"route": "38"}))

        assert sorted(option.value for option in judah.options) == ["15726", "16636"]
        assert sorted(option.value for option in geary.options) == ["13220"]
        assert [params["line_id"] for params in by_line] == ["N", "38"]

    def test_no_route_asks_for_the_unfiltered_catalog(self, by_line):
        plugin = make_plugin(api_key="key")

        result = plugin.get_options(OptionsRequest(options_id="stops", parent={"route": ""}))

        assert "line_id" not in by_line[0]
        assert len(result.options) == 3


class TestUpstreamQuirks:
    """511.org's real answers are messier than its specification."""

    def test_a_byte_order_mark_does_not_break_the_catalog(self, monkeypatch):
        class BomResponse:
            text = "﻿" + json.dumps(STOPS_JSON)

            def raise_for_status(self):
                return None

        monkeypatch.setattr(requests, "get", lambda *a, **k: BomResponse())

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="stops"))

        assert len(result.options) == 3

    def test_routes_sent_as_a_bare_array_are_understood(self, monkeypatch):
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: FakeResponse(LINES_JSON["content"])
        )

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="routes"))

        assert sorted(option.value for option in result.options) == ["38", "N"]

    def test_a_response_that_is_not_json_becomes_options_unavailable(self, monkeypatch):
        class HtmlResponse:
            text = "<html>maintenance</html>"

            def raise_for_status(self):
                return None

        monkeypatch.setattr(requests, "get", lambda *a, **k: HtmlResponse())

        plugin = make_plugin(api_key="key")

        with pytest.raises(OptionsUnavailable):
            plugin.get_options(OptionsRequest(options_id="stops"))

    def test_a_single_stop_sent_unwrapped_is_still_a_catalog(self, monkeypatch):
        payload = {
            "Contents": {
                "dataObjects": {
                    "ScheduledStopPoint": {"id": "SF_15726", "Name": "Church St & Duboce Ave"}
                }
            }
        }
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="stops"))

        assert [option.value for option in result.options] == ["15726"]

    def test_twins_across_a_street_are_told_apart_east_and_west(self, monkeypatch):
        payload = {
            "Contents": {
                "dataObjects": {
                    "ScheduledStopPoint": [
                        {
                            "id": "SF_14000",
                            "Name": "Judah St & 9th Ave",
                            "Location": {"Latitude": "37.7614", "Longitude": "-122.4665"},
                        },
                        {
                            "id": "SF_14001",
                            "Name": "Judah St & 9th Ave",
                            "Location": {"Latitude": "37.7614", "Longitude": "-122.4655"},
                        },
                    ]
                }
            }
        }
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="stops"))

        descriptions = {option.value: option.description for option in result.options}
        assert descriptions["14000"] == "Stop 14000 · west side"
        assert descriptions["14001"] == "Stop 14001 · east side"


class TestUnknownCatalog:
    """A catalog this plugin does not serve is a programming error, not a hint."""

    def test_unknown_options_id_raises_not_implemented(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("an unknown options_id must not reach 511.org")

        monkeypatch.setattr(requests, "get", refuse)

        plugin = make_plugin(api_key="key")

        with pytest.raises(NotImplementedError) as excinfo:
            plugin.get_options(OptionsRequest(options_id="vehicles"))

        assert "vehicles" in str(excinfo.value)


class TestCatalogCaching:
    """The catalog outlives the throwaway instance core dispatches into."""

    def test_a_second_instance_reuses_the_fetched_catalog(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(url)
            return FakeResponse(STOPS_JSON)

        monkeypatch.setattr(requests, "get", fake_get)

        make_plugin(api_key="key").get_options(OptionsRequest(options_id="stops"))
        second = make_plugin(api_key="key").get_options(OptionsRequest(options_id="stops"))

        assert len(calls) == 1, f"expected the catalog to be reused, 511.org was called {len(calls)} times"
        assert len(second.options) == 3

    def test_a_different_api_key_does_not_read_another_installs_catalog(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params["api_key"])
            return FakeResponse(STOPS_JSON)

        monkeypatch.setattr(requests, "get", fake_get)

        make_plugin(api_key="key-a").get_options(OptionsRequest(options_id="stops"))
        make_plugin(api_key="key-b").get_options(OptionsRequest(options_id="stops"))

        assert calls == ["key-a", "key-b"]

    def test_the_route_list_is_cached_too(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(url)
            return FakeResponse(LINES_JSON)

        monkeypatch.setattr(requests, "get", fake_get)

        make_plugin(api_key="key").get_options(OptionsRequest(options_id="routes"))
        make_plugin(api_key="key").get_options(OptionsRequest(options_id="routes"))

        assert len(calls) == 1


class TestRouteOptions:
    """The ``routes`` catalog that scopes the stop list."""

    def test_lists_the_operators_routes(self, monkeypatch):
        requested = []

        def fake_get(url, params=None, timeout=None):
            requested.append((url, params))
            return FakeResponse(LINES_JSON)

        monkeypatch.setattr(requests, "get", fake_get)

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="routes"))

        assert requested[0][0] == "http://api.511.org/transit/lines"
        assert requested[0][1]["operator_id"] == "SF"
        by_value = {option.value: option for option in result.options}
        assert set(by_value) == {"N", "38"}
        assert by_value["N"].label == "N — JUDAH"
        assert by_value["38"].label == "38 — GEARY"

    def test_routes_are_grouped_by_mode(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(LINES_JSON))

        plugin = make_plugin(api_key="key")
        result = plugin.get_options(OptionsRequest(options_id="routes"))

        groups = {option.value: option.group for option in result.options}
        assert groups == {"N": "Metro & streetcar", "38": "Bus"}
