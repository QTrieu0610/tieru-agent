"""Read-only current weather tool using wttr.in's JSON endpoint."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from tieru.tools.registry import Tool

_WEATHER_TIMEOUT_SECONDS = 10.0
_USER_AGENT = "Tieru/0.3 weather tool"


def current_weather(location: str) -> str:
    """Return a concise current-weather summary for a user-provided location."""
    location = location.strip()
    if not location:
        raise ValueError("location must not be empty")
    encoded = urllib.parse.quote(location, safe="")
    request = urllib.request.Request(
        f"https://wttr.in/{encoded}?format=j1",
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=_WEATHER_TIMEOUT_SECONDS) as response:
        payload: dict[str, Any] = json.loads(response.read())
    conditions = payload.get("current_condition") or []
    if not conditions or not isinstance(conditions[0], dict):
        raise ValueError("weather service returned no current conditions")
    condition = conditions[0]
    descriptions = condition.get("weatherDesc") or []
    description = (
        str(descriptions[0].get("value", "unknown"))
        if descriptions and isinstance(descriptions[0], dict)
        else "unknown"
    )
    temperature = str(condition.get("temp_C", "unknown"))
    feels_like = str(condition.get("FeelsLikeC", "unknown"))
    humidity = str(condition.get("humidity", "unknown"))
    return (
        f"Current weather in {location}: {description}, {temperature}°C "
        f"(feels like {feels_like}°C), humidity {humidity}%."
    )


def make_tool() -> Tool:
    return Tool(
        name="weather",
        description="Get the current weather for a city or other named location.",
        input_schema={
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "minLength": 1,
                    "description": "City or named location, for example Hanoi, Vietnam",
                }
            },
            "required": ["location"],
            "additionalProperties": False,
        },
        fn=current_weather,
        timeout_seconds=_WEATHER_TIMEOUT_SECONDS,
        risk="low",
        read_only=True,
        capabilities=("network.read",),
        default_policy="allow",
        operation="weather",
        fixed_target="wttr.in",
        resource_type="network",
    )
