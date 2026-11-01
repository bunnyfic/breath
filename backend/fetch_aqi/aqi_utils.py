"""Indian CPCB AQI helpers (PM2.5 and PM10 based, 24h rolling average)."""

# (conc_low, conc_high, index_low, index_high)
PM25_BREAKPOINTS = [
    (0, 30, 0, 50),
    (30, 60, 51, 100),
    (60, 90, 101, 200),
    (90, 120, 201, 300),
    (120, 250, 301, 400),
    (250, 380, 401, 500),
]

PM10_BREAKPOINTS = [
    (0, 50, 0, 50),
    (50, 100, 51, 100),
    (100, 250, 101, 200),
    (250, 350, 201, 300),
    (350, 430, 301, 400),
    (430, 510, 401, 500),
]


def sub_index(conc, breakpoints):
    """Linear interpolation of a pollutant concentration into a sub-index."""
    if conc is None:
        return None
    for c_lo, c_hi, i_lo, i_hi in breakpoints:
        if conc <= c_hi:
            return round((i_hi - i_lo) / (c_hi - c_lo) * (conc - c_lo) + i_lo)
    return 500


def rolling_mean(values, i, window=24, min_count=16):
    """Mean of up to `window` values ending at index i, ignoring None.
    Returns None if fewer than `min_count` valid values (CPCB needs 16 of 24)."""
    chunk = [v for v in values[max(0, i - window + 1): i + 1] if v is not None]
    if len(chunk) < min_count:
        return None
    return sum(chunk) / len(chunk)


def cpcb_aqi(pm25_24h, pm10_24h):
    """Overall AQI is the worst (max) sub-index."""
    subs = [
        s
        for s in (
            sub_index(pm25_24h, PM25_BREAKPOINTS),
            sub_index(pm10_24h, PM10_BREAKPOINTS),
        )
        if s is not None
    ]
    return max(subs) if subs else None


def category(aqi):
    if aqi is None:
        return None
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Satisfactory"
    if aqi <= 200:
        return "Moderate"
    if aqi <= 300:
        return "Poor"
    if aqi <= 400:
        return "Very Poor"
    return "Severe"