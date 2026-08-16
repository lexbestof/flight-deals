import os
from datetime import date, timedelta
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

DUFFEL_API = "https://api.duffel.com/air/offer_requests"
DUFFEL_TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN", "")

app = FastAPI(title="Kinshasa Flight Deals")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[x.strip() for x in os.getenv("ALLOWED_ORIGINS", "*").split(",")],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

ROUTES = [
    {"key": "paris", "label": "Paris ↔ Kinshasa", "origin": "PAR", "destination": "FIH", "max_price_eur": 750.0},
    {"key": "brussels", "label": "Bruxelles ↔ Kinshasa", "origin": "BRU", "destination": "FIH", "max_price_eur": 650.0},
]

DEPARTURE_DATES = [date(2026, 8, 18), date(2026, 8, 19), date(2026, 8, 20)]
RETURN_DATES = [date(2026, 8, 29), date(2026, 8, 30), date(2026, 8, 31)]


def segment_summary(slice_data: dict[str, Any]) -> dict[str, Any]:
    segments = slice_data.get("segments") or []
    if not segments:
        return {}
    first, last = segments[0], segments[-1]
    carriers = []
    for seg in segments:
        name = (seg.get("operating_carrier") or {}).get("name")
        if name and name not in carriers:
            carriers.append(name)
    return {
        "from": (first.get("origin") or {}).get("iata_code"),
        "to": (last.get("destination") or {}).get("iata_code"),
        "departing_at": first.get("departing_at"),
        "arriving_at": last.get("arriving_at"),
        "stops": max(0, len(segments) - 1),
        "airlines": carriers,
    }


async def search_one(client, route, departure, return_date):
    payload = {
        "data": {
            "slices": [
                {"origin": route["origin"], "destination": route["destination"], "departure_date": departure.isoformat()},
                {"origin": route["destination"], "destination": route["origin"], "departure_date": return_date.isoformat()},
            ],
            "passengers": [{"type": "adult"}],
            "cabin_class": "economy",
            "max_connections": 2,
        }
    }

    r = await client.post(
        DUFFEL_API,
        params={"return_offers": "true", "supplier_timeout": "15000", "view": "offers"},
        headers={
            "Authorization": f"Bearer {DUFFEL_TOKEN}",
            "Duffel-Version": "v2",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Duffel {r.status_code}: {r.text[:300]}")

    offers = r.json().get("data", {}).get("offers") or []
    good = []

    for offer in offers:
        if offer.get("total_currency") != "EUR":
            continue
        try:
            amount = float(offer["total_amount"])
        except (KeyError, TypeError, ValueError):
            continue
        if amount > route["max_price_eur"]:
            continue

        slices = offer.get("slices") or []
        good.append({
            "id": offer.get("id"),
            "route_key": route["key"],
            "route": route["label"],
            "price": amount,
            "currency": "EUR",
            "threshold": route["max_price_eur"],
            "departure_date": departure.isoformat(),
            "return_date": return_date.isoformat(),
            "owner": (offer.get("owner") or {}).get("name"),
            "expires_at": offer.get("expires_at"),
            "live_mode": bool(offer.get("live_mode")),
            "outbound": segment_summary(slices[0]) if len(slices) > 0 else {},
            "return": segment_summary(slices[1]) if len(slices) > 1 else {},
        })

    return good


async def run_full_search():
    if not DUFFEL_TOKEN:
        raise RuntimeError("DUFFEL_ACCESS_TOKEN n'est pas configuré.")

    all_offers = []
    async with httpx.AsyncClient(timeout=40.0) as client:
        for route in ROUTES:
            for dep in DEPARTURE_DATES:
                for ret in RETURN_DATES:
                    if ret <= dep:
                        continue
                    offers = await search_one(client, route, dep, ret)
                    all_offers.extend(offers)

    all_offers.sort(key=lambda x: x["price"])
    return all_offers


@app.get("/")
async def root():
    return {
        "status": "ok",
        "search_window": {
            "departure": [d.isoformat() for d in DEPARTURE_DATES],
            "return": [d.isoformat() for d in RETURN_DATES],
        },
        "thresholds": {r["label"]: r["max_price_eur"] for r in ROUTES},
    }


@app.get("/search")
async def search():
    try:
        offers = await run_full_search()

    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail=f"Timeout lors de la communication avec Duffel: {exc}"
        )

    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Erreur réseau avec Duffel: {exc}"
        )

    except RuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc)
        )

    by_route = {}

    for route in ROUTES:
        matches = [
            o for o in offers
            if o["route_key"] == route["key"]
        ]

        by_route[route["key"]] = {
            "label": route["label"],
            "threshold": route["max_price_eur"],
            "offers": matches[:20],
        }

    return {
        "best_offer": offers[0] if offers else None,
        "total_matches": len(offers),
        "routes": by_route,
        "departure_dates": [
            d.isoformat()
            for d in DEPARTURE_DATES
        ],
        "return_dates": [
            d.isoformat()
            for d in RETURN_DATES
        ],
        "notice": "Les prix peuvent changer ou expirer avant réservation.",
    }