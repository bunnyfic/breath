"""Forecast API: GET /cities, GET /forecast?city=Delhi, GET /history?city=Delhi&hours=72"""
import json
import os
import math
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

from aqi_utils import category, cpcb_aqi, rolling_mean
from lgbm_predict import load_model, predict

HERE = os.path.dirname(__file__)
AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
WX_URL = "https://api.open-meteo.com/v1/forecast"
AQ_VARS = ["pm2_5", "pm10", "carbon_monoxide", "nitrogen_dioxide", "sulphur_dioxide", "ozone"]
WX_VARS = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "surface_pressure"]
IST = timezone(timedelta(hours=5, minutes=30))
CACHE_SECONDS = 20 * 60
NAN = float("nan")

# ---- loaded once per cold start -------------------------------------------------
with open(os.path.join(HERE, "cities.json"), encoding="utf-8") as f:
    CITIES = json.load(f)
CITY_BY_NAME = {c["name"].lower(): c for c in CITIES}

with open(os.path.join(HERE, "model", "meta.json"), encoding="utf-8") as f:
    META = json.load(f)
FEATURES = META["features"]
HORIZONS = META["horizons"]
LAGS = META["lags"]
CITY_ID = META["city_id"]
MODELS = {h: load_model(os.path.join(HERE, "model", f"aqi_lgbm_h{h}.txt")) for h in HORIZONS}

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"]) if os.environ.get("TABLE_NAME") else None
_cache = {}


# ---- helpers ----------------------------------------------------------------------
def fetch_json(url, params):
    query = urllib.parse.urlencode(params, safe=",")
    with urllib.request.urlopen(f"{url}?{query}", timeout=15) as resp:
        return json.load(resp)


def num(v):
    return NAN if v is None else float(v)


def build_forecast(city):
    common = {
        "latitude": city["lat"], "longitude": city["lon"],
        "past_days": 4, "forecast_days": 1, "timezone": "Asia/Kolkata",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_aq = pool.submit(fetch_json, AQ_URL, {**common, "hourly": ",".join(AQ_VARS)})
        f_wx = pool.submit(fetch_json, WX_URL, {**common, "hourly": ",".join(WX_VARS)})
        aq, wx = f_aq.result()["hourly"], f_wx.result()["hourly"]

    times = aq["time"]
    now_str = datetime.now(IST).strftime("%Y-%m-%dT%H:00")
    idx = max((i for i, t in enumerate(times) if t <= now_str and aq["pm2_5"][i] is not None), default=None)
    if idx is None or idx < 24 * 3:
        raise RuntimeError("Not enough recent data from the upstream API")

    pm25, pm10 = aq["pm2_5"], aq["pm10"]
    aqi = [cpcb_aqi(rolling_mean(pm25, i), rolling_mean(pm10, i)) for i in range(idx + 1)]

    w_index = {t: i for i, t in enumerate(wx["time"])}
    wi = w_index.get(times[idx])

    now_dt = datetime.strptime(times[idx], "%Y-%m-%dT%H:%M")
    feats = {"aqi_now": num(aqi[idx])}
    for L in LAGS:
        feats[f"aqi_lag{L}"] = num(aqi[idx - L])
    r6 = [v for v in aqi[idx - 5: idx + 1] if v is not None]
    r24 = [v for v in aqi[idx - 23: idx + 1] if v is not None]
    feats["aqi_roll6"] = sum(r6) / len(r6) if len(r6) >= 4 else NAN
    feats["aqi_roll24"] = sum(r24) / len(r24) if len(r24) >= 16 else NAN
    for var in ("pm2_5", "pm10", "nitrogen_dioxide", "sulphur_dioxide", "ozone", "carbon_monoxide"):
        feats[var] = num(aq[var][idx])
    feats["pm2_5_lag24"] = num(pm25[idx - 24])
    for var in WX_VARS:
        feats[var] = num(wx[var][wi]) if wi is not None else NAN
    feats["hour"] = now_dt.hour
    feats["dow"] = now_dt.weekday()
    feats["month"] = now_dt.month
    feats["hour_sin"] = math.sin(2 * math.pi * now_dt.hour / 24)
    feats["hour_cos"] = math.cos(2 * math.pi * now_dt.hour / 24)
    feats["city_id"] = CITY_ID[city["name"]]

    x = [feats[name] for name in FEATURES]

    forecast = []
    for h in HORIZONS:
        value = max(0, min(500, round(predict(MODELS[h], x))))
        forecast.append({
            "hours_ahead": h,
            "time": (now_dt + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M"),
            "aqi": value,
            "category": category(value),
        })

    current = aqi[idx]
    return {
        "city": city["name"],
        "state": city["state"],
        "as_of": times[idx],
        "current": {
            "aqi": current,
            "category": category(current),
            **{v: aq[v][idx] for v in AQ_VARS},
            **{v: (wx[v][wi] if wi is not None else None) for v in WX_VARS},
        },
        "forecast": forecast,
    }


def get_history(city_name, hours):
    if TABLE is None:
        return []
    start = (datetime.now(IST) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:00")
    items, kwargs = [], {"KeyConditionExpression": Key("city").eq(city_name) & Key("ts").gte(start)}
    while True:
        resp = TABLE.query(**kwargs)
        items.extend(resp["Items"])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return [
        {"ts": i["ts"], "aqi": i.get("aqi"), "category": i.get("category"),
         "pm2_5": i.get("pm2_5"), "pm10": i.get("pm10")}
        for i in items
    ]


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"Not serializable: {type(o)}")


def respond(status, body, max_age=300):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": f"max-age={max_age}",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=_json_default),
    }


# ---- entry point ------------------------------------------------------------------
def lambda_handler(event, context):
    path = (event.get("rawPath") or "").rstrip("/")
    params = event.get("queryStringParameters") or {}

    if path.endswith("/cities"):
        return respond(200, [{"name": c["name"], "state": c["state"], "lat": c["lat"], "lon": c["lon"]} for c in CITIES], 3600)

    if path.endswith("/forecast") or path.endswith("/history"):
        name = (params.get("city") or "").strip()
        city = CITY_BY_NAME.get(name.lower())
        if not name:
            return respond(400, {"error": "Missing ?city= parameter"}, 0)
        if city is None:
            return respond(404, {"error": f"Unknown city '{name}'. Call /cities for the list."}, 0)

        if path.endswith("/history"):
            try:
                hours = max(1, min(168, int(params.get("hours", 72))))
            except ValueError:
                return respond(400, {"error": "hours must be a number"}, 0)
            return respond(200, {"city": city["name"], "history": get_history(city["name"], hours)}, 300)

        cached = _cache.get(city["name"])
        if cached and cached[0] > time.time():
            return respond(200, cached[1])
        try:
            payload = build_forecast(city)
        except Exception as exc:  # upstream API down, not enough data, etc.
            print("forecast error:", repr(exc))
            return respond(502, {"error": "Could not build forecast right now. Try again shortly."}, 0)
        _cache[city["name"]] = (time.time() + CACHE_SECONDS, payload)
        return respond(200, payload)

    return respond(404, {"error": "Not found"}, 0)