"""Normalize broker account equity without inventing investment returns."""
from __future__ import annotations

import math


def equity_series(raw, *, now, current=None):
    points = {}
    for at, value in zip(raw.get('timestamp') or [], raw.get('equity') or []):
        if (type(at) in (int, float) and type(value) in (int, float)
                and math.isfinite(at) and math.isfinite(value) and 0 < at <= now):
            key=int(at)
            if key not in points or at >= points[key][0]:
                points[key] = (at,float(value))
    if current:
        at, value = current
        if (type(at) in (int, float) and type(value) in (int, float)
                and math.isfinite(at) and math.isfinite(value) and 0 < at <= now
                and (not points or at >= max(row[0] for row in points.values()))):
            points[int(at)] = (at,float(value))
    return {'points': [(at,value[1]) for at,value in sorted(points.items())]}
