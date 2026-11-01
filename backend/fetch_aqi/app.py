"""Hourly ingestion: Open-Meteo air quality + weather for all cities -> DynamoDB."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3

from aqi_utils import category, cpcb_aqi, rolling_mean

AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
WX_URL = "https://api.open-meteo.com/v1/forecast"

AQ_VARS = ["pm2_5", "pm10", "carbon_monoxide", "nitrogen_dioxide", "sulphur_dioxide", "ozone"]
WX_VARS = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "surface_pressure"]

WRITE_HOURS = 3   # how many most-recent hours to (re)write each run
PAST_DAYS = 2     # history pulled so the 24h rolling mean is always computable
IST = timezone(timedelta(hours=5, minutes=30))

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

with open(os.path.join(os.path.dirname(__file__), "cities.json"), encoding="utf-8") as f:
    CITIES = json.load(f)


def fetch(url, extra_params):
    params = {
        "latitude": ",".join(str(c["lat"]) for c in CITIES),
        "longitude": ",".join(str(c["lon"]) for c in CITIES),
        "past_days": PAST_DAYS,
        "forecast_days": 1,
        "timezone": "Asia/Kolkata",
        **extra_params,
    }
    query = urllib.parse.urlencode(params, safe=",")
    with urllib.request.urlopen(f"{url}?{query}", timeout=30) as resp:
        data = json.load(resp)
    # Open-Meteo returns a dict for one location, a list for many
    return data if isinstance(data, list) else [data]


def dec(value):
    return Decimal(str(round(value, 3)))


def lambda_handler(event, context):
    aq = fetch(AQ_URL, {"hourly": ",".join(AQ_VARS)})
    wx = fetch(WX_URL, {"hourly": ",".join(WX_VARS)})

    now_str = datetime.now(IST).strftime("%Y-%m-%dT%H:00")
    ttl = int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp())
    written = 0

    with table.batch_writer(overwrite_by_pkeys=["city", "ts"]) as batch:
        for city, a, w in zip(CITIES, aq, wx):
            a_h, w_h = a["hourly"], w["hourly"]
            times = a_h["time"]
            w_index = {t: i for i, t in enumerate(w_h["time"])}

            idxs = [i for i, t in enumerate(times) if t <= now_str][-WRITE_HOURS:]
            for i in idxs:
                aqi = cpcb_aqi(
                    rolling_mean(a_h["pm2_5"], i),
                    rolling_mean(a_h["pm10"], i),
                )
                item = {"city": city["name"], "ts": times[i], "ttl": ttl}

                for var in AQ_VARS:
                    if a_h[var][i] is not None:
                        item[var] = dec(a_h[var][i])

                wi = w_index.get(times[i])
                if wi is not None:
                    for var in WX_VARS:
                        if w_h[var][wi] is not None:
                            item[var] = dec(w_h[var][wi])

                if aqi is not None:
                    item["aqi"] = aqi
                    item["category"] = category(aqi)

                batch.put_item(Item=item)
                written += 1

    return {"statusCode": 200, "body": json.dumps({"cities": len(CITIES), "rows_written": written})}