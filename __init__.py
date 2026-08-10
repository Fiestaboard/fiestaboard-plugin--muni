"""SF Muni plugin for FiestaBoard.

Displays Muni transit arrival times with support for multiple stops and lines.
"""

from typing import Any, Dict, List, Optional
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

import requests

from src.plugins.base import (
    Option,
    OptionsRequest,
    OptionsResult,
    OptionsUnavailable,
    PluginBase,
    PluginResult,
)

logger = logging.getLogger(__name__)

# Colors
COLOR_RED = 63
COLOR_ORANGE = 64

# 511.org catalog endpoints. The plugin's live arrivals come from the shared
# regional transit cache; the picker needs the *catalog* instead, which the
# cache does not carry, so it reads the same host with the same credential.
STOPS_API_URL = "http://api.511.org/transit/stops"
LINES_API_URL = "http://api.511.org/transit/lines"
OPERATOR_ID = "SF"

# The picker runs while a settings dialog is open, so the call has to give up
# long before the user does.
OPTIONS_TIMEOUT_SECONDS = 10

# "Church St & Duboce Ave", "Judah St/9th Ave", "3rd St at Palou Ave" -- 511
# names a stop after the pair of streets that meet there.
_CROSS_STREET_RE = re.compile(r"\s*(?:&|/|\bat\b)\s*", re.IGNORECASE)


# Core dispatches get_options into a throwaway instance, so anything cached on
# ``self`` is thrown away with it. The stop and route catalogs change on the
# order of service changes, not minutes, so they live here instead -- keyed by
# credential so two installs never read each other's list.
_CATALOG_TTL_SECONDS = 6 * 60 * 60
_catalog_cache: Dict[tuple, tuple] = {}
_catalog_cache_lock = threading.Lock()


def _catalog_get(key: tuple) -> Optional[List[Dict[str, Any]]]:
    """Return the cached catalog for *key*, or None when absent or stale."""
    with _catalog_cache_lock:
        entry = _catalog_cache.get(key)
    if not entry:
        return None
    fetched_at, value = entry
    if time.monotonic() - fetched_at > _CATALOG_TTL_SECONDS:
        return None
    return value


def _catalog_put(key: tuple, value: List[Dict[str, Any]]) -> None:
    """Remember *value* as the catalog for *key*."""
    with _catalog_cache_lock:
        _catalog_cache[key] = (time.monotonic(), value)


class MuniPlugin(PluginBase):
    """SF Muni transit plugin.
    
    Fetches arrival predictions from 511.org API via regional transit cache.
    Supports multiple stops with nested line data.
    """
    
    AGENCY = "SF"
    
    LINE_NAMES = {
        "N": "N-JUDAH",
        "J": "J-CHURCH",
        "K": "K-INGLESIDE",
        "L": "L-TARAVAL",
        "M": "M-OCEAN VIEW",
        "T": "T-THIRD",
        "S": "S-SHUTTLE",
        "F": "F-MARKET",
    }
    
    def __init__(self, manifest: Dict[str, Any]):
        """Initialize the muni plugin."""
        super().__init__(manifest)
        self._cache: Optional[Dict[str, Any]] = None
        self._transit_cache = None
    
    @property
    def plugin_id(self) -> str:
        return "muni"
    
    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        """Validate muni configuration."""
        errors = []
        
        if not config.get("api_key"):
            errors.append("511.org API key is required")
        
        stop_codes = config.get("stop_codes", [])
        if not stop_codes:
            errors.append("At least one stop code is required")
        
        return errors
    
    def _get_transit_cache(self):
        """Get or initialize transit cache."""
        if self._transit_cache is not None:
            return self._transit_cache
        
        try:
            from src.utils.transit_cache import get_transit_cache
            cache = get_transit_cache()
            
            api_key = self.config.get("api_key")
            refresh_interval = self.config.get("refresh_seconds", 90)
            
            cache.configure(
                api_key=api_key,
                refresh_interval=refresh_interval,
                enabled=True
            )
            
            if not cache.is_ready():
                cache.start()
            
            self._transit_cache = cache
            return cache
        except Exception as e:
            logger.error(f"Failed to initialize transit cache: {e}")
            return None
    
    def _normalize_line_code(self, line: str) -> str:
        """Normalize line code to single letter."""
        line_upper = line.upper()
        
        line_map = {
            "JUDAH": "N", "N-JUDAH": "N",
            "CHURCH": "J", "J-CHURCH": "J",
            "INGLESIDE": "K", "K-INGLESIDE": "K",
            "TARAVAL": "L", "L-TARAVAL": "L",
            "OCEAN VIEW": "M", "M-OCEAN VIEW": "M",
            "THIRD": "T", "T-THIRD": "T",
            "SHUTTLE": "S", "S-SHUTTLE": "S",
            "MARKET": "F", "F-MARKET": "F",
        }
        
        if line_upper in line_map:
            return line_map[line_upper]
        if len(line_upper) == 1:
            return line_upper
        if "-" in line_upper:
            return line_upper.split("-")[0]
        return line_upper
    
    def _get_display_line_name(self, line_code: str) -> str:
        """Get display-friendly line name."""
        return self.LINE_NAMES.get(line_code.upper(), line_code.upper())
    
    def _calculate_minutes_until(self, iso_timestamp: str) -> Optional[int]:
        """Calculate minutes until timestamp."""
        try:
            arrival_time = datetime.fromisoformat(iso_timestamp.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            delta = arrival_time - now
            return max(0, int(delta.total_seconds() / 60))
        except Exception:
            return None
    
    def _parse_stop_data(self, visits: List[Dict], stop_code: str) -> Optional[Dict]:
        """Parse arrival data for a single stop."""
        if not visits:
            return None
        
        arrivals_by_line = {}
        stop_name = ""
        
        for visit in visits:
            journey = visit.get("MonitoredVehicleJourney", {})
            
            published_line = journey.get("PublishedLineName", "")
            if isinstance(published_line, list):
                published_line = published_line[0] if published_line else ""
            if not published_line:
                continue
            
            line_code = self._normalize_line_code(published_line)
            
            monitored_call = journey.get("MonitoredCall", {})
            if not stop_name:
                stop_point_name = monitored_call.get("StopPointName", "")
                if isinstance(stop_point_name, list):
                    stop_point_name = stop_point_name[0] if stop_point_name else ""
                stop_name = stop_point_name
            
            expected_arrival = (
                monitored_call.get("ExpectedArrivalTime") or
                monitored_call.get("ExpectedDepartureTime") or
                monitored_call.get("AimedArrivalTime")
            )
            
            if expected_arrival:
                minutes = self._calculate_minutes_until(expected_arrival)
                if minutes is not None and minutes >= 0:
                    occupancy = journey.get("Occupancy", "UNKNOWN")
                    if isinstance(occupancy, list):
                        occupancy = occupancy[0] if occupancy else "UNKNOWN"
                    if occupancy is None:
                        occupancy = "UNKNOWN"
                    
                    is_full = str(occupancy).upper() == "FULL"
                    is_delayed = bool(journey.get("Delay") or journey.get("SituationRef"))
                    
                    arrival_data = {
                        "minutes": minutes,
                        "is_full": is_full,
                        "is_delayed": is_delayed,
                        "line_code": line_code,
                    }
                    
                    if line_code not in arrivals_by_line:
                        arrivals_by_line[line_code] = []
                    arrivals_by_line[line_code].append(arrival_data)
        
        if not arrivals_by_line:
            return None
        
        # Build lines dict
        lines = {}
        for line_code, line_arrivals in arrivals_by_line.items():
            line_arrivals.sort(key=lambda x: x["minutes"])
            top_arrivals = line_arrivals[:3]
            
            is_delayed = any(a.get("is_delayed") for a in top_arrivals)
            has_full = any(a.get("is_full") for a in top_arrivals)
            
            display_line = self._get_display_line_name(line_code)
            times = [str(a["minutes"]) for a in top_arrivals]
            formatted = f"{display_line}: {', '.join(times)} MIN"
            if is_delayed:
                formatted = f"{{63}}{formatted}"
            
            lines[line_code] = {
                "line": display_line,
                "line_code": line_code,
                "formatted": formatted,
                "next_arrival": top_arrivals[0]["minutes"] if top_arrivals else None,
                "is_delayed": is_delayed,
            }
        
        # Combined all_lines view
        all_arrivals = []
        for line_arrivals in arrivals_by_line.values():
            all_arrivals.extend(line_arrivals)
        all_arrivals.sort(key=lambda x: x["minutes"])
        top_all = all_arrivals[:3]
        
        all_line_codes = sorted(arrivals_by_line.keys())
        combined_display = "/".join([self._get_display_line_name(lc) for lc in all_line_codes])
        times = [str(a["minutes"]) for a in top_all]
        all_formatted = f"{combined_display}: {', '.join(times)} MIN"
        
        is_any_delayed = any(a.get("is_delayed") for a in top_all)
        if is_any_delayed:
            all_formatted = f"{{63}}{all_formatted}"
        
        # Get first line for backward compat
        first_line_code = all_line_codes[0] if all_line_codes else ""
        first_line_data = lines.get(first_line_code, {})
        
        return {
            "stop_code": stop_code,
            "stop_name": stop_name if stop_name else stop_code,
            "lines": lines,
            "all_lines": {
                "formatted": all_formatted,
                "next_arrival": top_all[0]["minutes"] if top_all else None,
                "is_delayed": is_any_delayed,
            },
            # Backward compat
            "line": first_line_data.get("line", ""),
            "formatted": first_line_data.get("formatted", ""),
            "is_delayed": first_line_data.get("is_delayed", False),
        }
    
    def fetch_data(self) -> PluginResult:
        """Fetch Muni arrival data."""
        stop_codes = self.config.get("stop_codes", [])
        if not stop_codes:
            return PluginResult(
                available=False,
                error="No stop codes configured"
            )
        
        cache = self._get_transit_cache()
        if not cache:
            return PluginResult(
                available=False,
                error="Transit cache not available"
            )
        
        if not cache.is_ready():
            return PluginResult(
                available=False,
                error="Transit cache not ready"
            )
        
        try:
            cached_data = cache.get_stops_data(self.AGENCY, stop_codes)
            stops_data = []
            
            for stop_code in stop_codes[:4]:
                visits = cached_data.get(stop_code, [])
                if visits:
                    parsed = self._parse_stop_data(visits, stop_code)
                    if parsed:
                        stops_data.append(parsed)
            
            if not stops_data:
                return PluginResult(
                    available=True,
                    data={
                        "stop_count": 0,
                        "stops": [],
                        "stop_name": "",
                        "stop_code": "",
                        "line": "",
                        "formatted": "NO ARRIVALS",
                        "is_delayed": False,
                    }
                )
            
            # Primary stop
            primary = stops_data[0]
            
            data = {
                # Primary stop
                "stop_name": primary["stop_name"],
                "stop_code": primary["stop_code"],
                "line": primary["line"],
                "formatted": primary["formatted"],
                "is_delayed": primary["is_delayed"],
                # Aggregate
                "stop_count": len(stops_data),
                # Array
                "stops": stops_data,
            }
            
            self._cache = data
            return PluginResult(available=True, data=data)
            
        except Exception as e:
            logger.exception("Error fetching Muni data")
            return PluginResult(available=False, error=str(e))
    
    # ------------------------------------------------------------------
    # Settings picker (remote-options)
    # ------------------------------------------------------------------

    def get_options(self, request: OptionsRequest) -> OptionsResult:
        """Browse the 511.org catalog so the user can pick stops by name."""
        api_key = str(self.config.get("api_key") or "").strip()
        if not api_key:
            raise OptionsUnavailable("Add your 511.org API key first — the stop list comes from 511.org.")

        if request.options_id == "stops":
            route = str((request.parent or {}).get("route") or "").strip()
            options = self._stop_options(self._fetch_stop_catalog(api_key, route))
        elif request.options_id == "routes":
            options = self._route_options(api_key)
        else:
            raise NotImplementedError(request.options_id)

        matches = self._apply_query(options, request.query)

        # ``total`` describes the answer to *this* question, so a search that
        # narrows 3,000 stops to 12 reports 12 rather than the catalog size.
        limit = request.limit if request.limit and request.limit > 0 else len(matches)
        page = matches[:limit]
        return OptionsResult(options=page, has_more=len(matches) > len(page), total=len(matches))

    @staticmethod
    def _apply_query(options: List[Option], query: str) -> List[Option]:
        """Keep the options whose label or stored value contains *query*.

        Matching is case-insensitive: people type "church", not "Church St".
        """
        needle = (query or "").strip().lower()
        if not needle:
            return options
        return [
            option
            for option in options
            if needle in option.label.lower() or needle in str(option.value).lower()
        ]

    def _stop_options(self, entries: List[Dict[str, Any]]) -> List[Option]:
        """Turn catalog entries into options a human can tell apart.

        The stop code alone is what the board needs, but it is exactly what the
        user cannot recognise, so the name leads and the code backs it up. San
        Francisco names both kerbs of an intersection identically, so twins get
        the side of the street they sit on as well.
        """
        twins: Dict[str, List[Dict[str, Any]]] = {}
        for entry in entries:
            twins.setdefault(entry["name"].casefold(), []).append(entry)

        options = []
        for entry in entries:
            description = f"Stop {entry['stop_code']}"
            siblings = twins[entry["name"].casefold()]
            side = self._side_of_street(entry, siblings) if len(siblings) > 1 else None
            if side:
                description = f"{description} · {side} side"
            options.append(
                Option(
                    value=entry["stop_code"],
                    label=entry["name"],
                    description=description,
                    group=self._primary_street(entry["name"]),
                )
            )
        return options

    @staticmethod
    def _primary_street(name: str) -> Optional[str]:
        """The street a stop is filed under -- the first half of "A St & B St"."""
        head = _CROSS_STREET_RE.split(name, maxsplit=1)[0].strip()
        return head or name.strip() or None

    @staticmethod
    def _side_of_street(entry: Dict[str, Any], siblings: List[Dict[str, Any]]) -> Optional[str]:
        """Which side of the intersection *entry* sits on, versus its twins.

        Compares the stop against the centre of the identically-named group, so
        the two kerbs of "Church St & Duboce Ave" read "north side" and "south
        side" instead of looking like a duplicate row.
        """
        located = [s for s in siblings if s.get("lat") is not None and s.get("lon") is not None]
        if entry.get("lat") is None or entry.get("lon") is None or len(located) < 2:
            return None

        centre_lat = sum(s["lat"] for s in located) / len(located)
        centre_lon = sum(s["lon"] for s in located) / len(located)
        # Degrees of longitude are shorter than degrees of latitude at this
        # latitude; scale before comparing so the dominant axis is the real one.
        north = entry["lat"] - centre_lat
        east = (entry["lon"] - centre_lon) * 0.79
        if not north and not east:
            return None
        if abs(north) >= abs(east):
            return "north" if north > 0 else "south"
        return "east" if east > 0 else "west"

    def _route_options(self, api_key: str) -> List[Option]:
        """Every Muni route, as options that scope the stop list."""
        entries = self._fetch_route_catalog(api_key)
        return [
            Option(value=entry["route_id"], label=entry["label"], group=entry["mode"])
            for entry in entries
        ]

    def _fetch_route_catalog(self, api_key: str) -> List[Dict[str, Any]]:
        """Return every Muni route 511.org publishes."""
        key = ("routes", "", api_key)
        cached = _catalog_get(key)
        if cached is not None:
            return cached

        params = {
            "api_key": api_key,
            "operator_id": OPERATOR_ID,
            "format": "json",
        }
        data = self._get_511_json(LINES_API_URL, params)

        entries = []
        for line in self._line_entries(data):
            route_id = str(line.get("Id") or line.get("id") or "")
            if not route_id:
                continue
            public_code = str(line.get("PublicCode") or route_id)
            name = str(line.get("Name") or "").strip()
            label = f"{public_code} — {name}" if name and name != public_code else public_code
            entries.append({"route_id": route_id, "label": label, "mode": self._mode_label(line)})
        _catalog_put(key, entries)
        return entries

    @staticmethod
    def _mode_label(line: Dict[str, Any]) -> Optional[str]:
        """Muni's rail lines and its bus lines are picked from different mental lists."""
        mode = str(line.get("TransportMode") or "").lower()
        if not mode:
            return None
        if mode in ("tram", "rail", "metro", "funicular"):
            return "Metro & streetcar"
        if mode in ("bus", "trolleybus", "coach"):
            return "Bus"
        return mode.capitalize()

    @staticmethod
    def _line_entries(data: Any) -> List[Dict[str, Any]]:
        """Pull the Line records out of whichever envelope 511.org used."""
        if isinstance(data, list):
            return [line for line in data if isinstance(line, dict)]
        if isinstance(data, dict):
            content = data.get("content")
            if isinstance(content, list):
                return [line for line in content if isinstance(line, dict)]
        return []

    def _fetch_stop_catalog(self, api_key: str, route: str = "") -> List[Dict[str, Any]]:
        """Return the SF Muni stops on *route*, or every stop when it is empty."""
        cached = _catalog_get(("stops", route, api_key))
        if cached is not None:
            return cached

        params = {
            "api_key": api_key,
            "operator_id": OPERATOR_ID,
            "format": "json",
        }
        if route:
            params["line_id"] = route
        data = self._get_511_json(STOPS_API_URL, params)

        points = data.get("Contents", {}).get("dataObjects", {}).get("ScheduledStopPoint", [])
        if isinstance(points, dict):
            # A collection of one comes back unwrapped from 511's XML-to-JSON
            # conversion, which is exactly what a narrow route filter returns.
            points = [points]

        entries = []
        for point in points:
            stop_id = str(point.get("id") or "")
            # 511 ids arrive as "SF_15726" for some feeds and bare "15726" for
            # others; StopMonitoring only ever accepts the bare code.
            stop_code = stop_id.split("_")[-1]
            if not stop_code:
                continue
            location = point.get("Location") or {}
            entries.append(
                {
                    "stop_code": stop_code,
                    "name": str(point.get("Name") or stop_code),
                    "lat": self._as_float(location.get("Latitude")),
                    "lon": self._as_float(location.get("Longitude")),
                }
            )
        # Alphabetical, because the picker is scanned by eye and 511 returns
        # the catalog in feed order.
        entries.sort(key=lambda entry: (entry["name"].casefold(), entry["stop_code"]))
        _catalog_put(("stops", route, api_key), entries)
        return entries

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        """511 sends coordinates as strings, and sometimes not at all."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _get_511_json(url: str, params: Dict[str, Any]) -> Any:
        """GET *url* from 511.org and decode its (occasionally BOM-prefixed) JSON.

        Everything 511.org can do to us here -- reject the key, rate-limit us,
        time out, answer with something that is not JSON -- is a "cannot answer
        right now", not a bug in the plugin, so it comes back as
        :class:`OptionsUnavailable` and the settings form shows a hint.
        """
        try:
            response = requests.get(url, params=params, timeout=OPTIONS_TIMEOUT_SECONDS)
            response.raise_for_status()
            content = response.text
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (401, 403):
                raise OptionsUnavailable("511.org rejected that API key — check it and try again.") from exc
            if status == 429:
                raise OptionsUnavailable("511.org is rate-limiting this key — try again in a minute.") from exc
            raise OptionsUnavailable(f"511.org returned an error ({status}) — try again shortly.") from exc
        except requests.exceptions.RequestException as exc:
            raise OptionsUnavailable("Could not reach 511.org — try again shortly.") from exc

        if content.startswith("\ufeff"):
            content = content[1:]
        try:
            return json.loads(content)
        except ValueError as exc:
            raise OptionsUnavailable("511.org sent a response this plugin could not read.") from exc

    def cleanup(self) -> None:
        """Cleanup resources."""
        self._transit_cache = None
        self._cache = None


# Export the plugin class
Plugin = MuniPlugin

