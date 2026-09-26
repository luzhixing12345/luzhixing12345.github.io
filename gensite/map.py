"""Project simplified province outlines into an SVG map of China."""

from __future__ import annotations

import json
import math
from pathlib import Path


_SUFFIXES = (
    "维吾尔自治区",
    "壮族自治区",
    "回族自治区",
    "特别行政区",
    "自治区",
    "省",
    "市",
)

_LON0 = math.radians(73.2)
_LON1 = math.radians(135.1)
_LAT0 = 17.8
_LAT1 = 53.7
_WIDTH = 800.0


def short_name(name: str) -> str:
    for suffix in _SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _mercator(lat: float) -> float:
    lat = max(min(lat, 85.0), -85.0)
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


_Y0 = _mercator(_LAT1)
_Y1 = _mercator(_LAT0)
_SPAN_X = _LON1 - _LON0
_SPAN_Y = _Y0 - _Y1
_HEIGHT = _WIDTH * (_SPAN_Y / _SPAN_X)


def _project(lon: float, lat: float) -> tuple[float, float]:
    x = (math.radians(lon) - _LON0) / _SPAN_X * _WIDTH
    y = (_Y0 - _mercator(lat)) / _SPAN_Y * _HEIGHT
    return x, y


def _rings_to_path(rings: list[list[list[float]]]) -> str:
    commands: list[str] = []
    for ring in rings:
        if len(ring) < 3:
            continue
        x, y = _project(ring[0][0], ring[0][1])
        commands.append(f"M{x:.1f} {y:.1f}")
        for lon, lat in ring[1:]:
            x, y = _project(lon, lat)
            commands.append(f"L{x:.1f} {y:.1f}")
        commands.append("Z")
    return "".join(commands)


def load_provinces(path: Path) -> list[dict[str, str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    provinces = []
    for item in raw:
        name = item["name"]
        provinces.append(
            {
                "id": short_name(name),
                "name": name,
                "d": _rings_to_path(item["rings"]),
            }
        )
    return provinces


def map_viewbox() -> str:
    pad = 8
    return f"{-pad:.0f} {-pad:.0f} {_WIDTH + pad * 2:.0f} {_HEIGHT + pad * 2:.0f}"
