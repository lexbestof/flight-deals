import os
import asyncio
import math
import time

from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware


# ============================================================
# CONFIGURATION
# ============================================================

DUFFEL_API = "https://api.duffel.com/air/offer_requests"

DUFFEL_TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN", "")

# Temps maximum laissé à Duffel pour interroger ses fournisseurs.
SUPPLIER_TIMEOUT_MS = 15000

# Timeout côté application, volontairement supérieur au
# supplier_timeout de Duffel.
HTTP_TIMEOUT_SECONDS = 40.0

# Petite pause entre deux Offer Requests Duffel.
REQUEST_DELAY_SECONDS = 1.0

# Nombre maximum de nouvelles tentatives après un HTTP 429.
MAX_RATE_LIMIT_RETRIES = 3

# On conserve les résultats pendant 5 minutes.
CACHE_TTL_SECONDS = 5 * 60


# ============================================================
# APPLICATION FASTAPI
# ============================================================

app = FastAPI(
    title="Kinshasa Flight Deals"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        x.strip()
        for x in os.getenv(
            "ALLOWED_ORIGINS",
            "*"
        ).split(",")
    ],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ============================================================
# ROUTES ET DATES DE RECHERCHE
# ============================================================

ROUTES = [
    {
        "key": "paris",
        "label": "Paris ↔ Kinshasa",
        "origin": "PAR",
        "destination": "FIH",
        "max_price_eur": 750.0,
    },
    {
        "key": "brussels",
        "label": "Bruxelles ↔ Kinshasa",
        "origin": "BRU",
        "destination": "FIH",
        "max_price_eur": 650.0,
    },
]


DEPARTURE_DATES = [
    date(2026, 8, 18),
    date(2026, 8, 19),
    date(2026, 8, 20),
]


RETURN_DATES = [
    date(2026, 8, 29),
    date(2026, 8, 30),
    date(2026, 8, 31),
]


# ============================================================
# CACHE
# ============================================================

# Cache simple en mémoire.
#
# Il permet d'éviter de refaire immédiatement 18 recherches
# Duffel lorsqu'un utilisateur reclique sur le bouton.
_search_cache = {
    "offers": None,
    "created_at": 0.0,
}

# Empêche deux recherches Duffel complètes en même temps.
_search_lock = asyncio.Lock()


def cache_is_valid() -> bool:
    """
    Retourne True si nous avons encore des résultats récents.
    """

    if _search_cache["offers"] is None:
        return False

    age = time.monotonic() - _search_cache["created_at"]

    return age < CACHE_TTL_SECONDS


# ============================================================
# FORMATAGE DES SEGMENTS DE VOL
# ============================================================

def segment_summary(
    slice_data: dict[str, Any]
) -> dict[str, Any]:

    segments = slice_data.get("segments") or []

    if not segments:
        return {}

    first = segments[0]
    last = segments[-1]

    carriers = []

    for segment in segments:

        carrier = (
            segment.get("operating_carrier")
            or {}
        )

        name = carrier.get("name")

        if name and name not in carriers:
            carriers.append(name)

    return {
        "from": (
            first.get("origin")
            or {}
        ).get("iata_code"),

        "to": (
            last.get("destination")
            or {}
        ).get("iata_code"),

        "departing_at":
            first.get("departing_at"),

        "arriving_at":
            last.get("arriving_at"),

        "stops":
            max(0, len(segments) - 1),

        "airlines":
            carriers,
    }


# ============================================================
# GESTION DU RATE LIMIT DUFFEL
# ============================================================

def get_rate_limit_wait_seconds(
    response: httpx.Response
) -> int:
    """
    Lit l'en-tête ratelimit-reset envoyé par Duffel.

    Duffel fournit normalement une date HTTP, par exemple :

    Tue, 24 Nov 2020 08:22:00 GMT

    On calcule combien de secondes il faut attendre.
    """

    reset_header = response.headers.get(
        "ratelimit-reset"
    )

    # Valeur de secours si l'en-tête est absent
    # ou impossible à interpréter.
    fallback_wait = 5

    if not reset_header:
        return fallback_wait

    try:

        reset_time = parsedate_to_datetime(
            reset_header
        )

        if reset_time.tzinfo is None:
            reset_time = reset_time.replace(
                tzinfo=timezone.utc
            )

        now = datetime.now(timezone.utc)

        seconds = (
            reset_time - now
        ).total_seconds()

        # +1 seconde de marge.
        return max(
            1,
            math.ceil(seconds) + 1
        )

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):

        return fallback_wait


async def post_duffel_with_retry(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
) -> httpx.Response:
    """
    Envoie une Offer Request à Duffel.

    En cas de HTTP 429, on respecte automatiquement
    l'en-tête ratelimit-reset avant de réessayer.
    """

    for attempt in range(
        MAX_RATE_LIMIT_RETRIES + 1
    ):

        response = await client.post(
            DUFFEL_API,

            params={
                "return_offers": "true",
                "supplier_timeout":
                    str(SUPPLIER_TIMEOUT_MS),
                "view": "offers",
            },

            headers={
                "Authorization":
                    f"Bearer {DUFFEL_TOKEN}",

                "Duffel-Version":
                    "v2",

                "Accept":
                    "application/json",

                "Content-Type":
                    "application/json",
            },

            json=payload,
        )

        # Tout va bien : pas de rate limit.
        if response.status_code != 429:
            return response

        # Nous sommes déjà à la dernière tentative.
        if attempt >= MAX_RATE_LIMIT_RETRIES:

            raise RuntimeError(
                "Duffel 429 : limite de requêtes "
                "atteinte après plusieurs tentatives. "
                "Réessaie dans quelques minutes."
            )

        wait_seconds = (
            get_rate_limit_wait_seconds(
                response
            )
        )

        print(
            "Duffel rate limit 429. "
            f"Nouvelle tentative dans "
            f"{wait_seconds} seconde(s)."
        )

        await asyncio.sleep(
            wait_seconds
        )

    # Cette ligne ne devrait normalement
    # jamais être atteinte.
    raise RuntimeError(
        "Impossible de contacter Duffel."
    )


# ============================================================
# RECHERCHE D'UNE COMBINAISON DE DATES
# ============================================================

async def search_one(
    client: httpx.AsyncClient,
    route: dict[str, Any],
    departure: date,
    return_date: date,
):

    payload = {
        "data": {
            "slices": [
                {
                    "origin":
                        route["origin"],

                    "destination":
                        route["destination"],

                    "departure_date":
                        departure.isoformat(),
                },
                {
                    "origin":
                        route["destination"],

                    "destination":
                        route["origin"],

                    "departure_date":
                        return_date.isoformat(),
                },
            ],

            "passengers": [
                {
                    "type": "adult"
                }
            ],

            "cabin_class":
                "economy",

            "max_connections":
                2,
        }
    }


    response = await post_duffel_with_retry(
        client,
        payload,
    )


    if response.status_code >= 400:

        raise RuntimeError(
            f"Duffel {response.status_code}: "
            f"{response.text[:500]}"
        )


    data = response.json()

    offers = (
        data
        .get("data", {})
        .get("offers")
        or []
    )


    good_offers = []


    for offer in offers:

        # Nous voulons uniquement des tarifs EUR.
        if offer.get("total_currency") != "EUR":
            continue


        try:

            amount = float(
                offer["total_amount"]
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            continue


        # On ignore les billets au-dessus du plafond.
        if amount > route["max_price_eur"]:
            continue


        slices = (
            offer.get("slices")
            or []
        )


        good_offers.append(
            {
                "id":
                    offer.get("id"),

                "route_key":
                    route["key"],

                "route":
                    route["label"],

                "price":
                    amount,

                "currency":
                    "EUR",

                "threshold":
                    route["max_price_eur"],

                "departure_date":
                    departure.isoformat(),

                "return_date":
                    return_date.isoformat(),

                "owner":
                    (
                        offer.get("owner")
                        or {}
                    ).get("name"),

                "expires_at":
                    offer.get("expires_at"),

                "live_mode":
                    bool(
                        offer.get("live_mode")
                    ),

                "outbound":
                    segment_summary(
                        slices[0]
                    )
                    if len(slices) > 0
                    else {},

                "return":
                    segment_summary(
                        slices[1]
                    )
                    if len(slices) > 1
                    else {},
            }
        )


    return good_offers


# ============================================================
# RECHERCHE COMPLÈTE
# ============================================================

async def perform_full_search():

    if not DUFFEL_TOKEN:

        raise RuntimeError(
            "DUFFEL_ACCESS_TOKEN "
            "n'est pas configuré."
        )


    all_offers = []


    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS
    ) as client:


        for route in ROUTES:

            for departure in DEPARTURE_DATES:

                for return_date in RETURN_DATES:

                    if return_date <= departure:
                        continue


                    print(
                        "Recherche Duffel : "
                        f"{route['origin']} → "
                        f"{route['destination']} "
                        f"{departure} / "
                        f"{return_date}"
                    )


                    offers = await search_one(
                        client,
                        route,
                        departure,
                        return_date,
                    )


                    all_offers.extend(
                        offers
                    )


                    # Important :
                    # on évite d'enchaîner immédiatement
                    # les Offer Requests.
                    await asyncio.sleep(
                        REQUEST_DELAY_SECONDS
                    )


    all_offers.sort(
        key=lambda offer:
            offer["price"]
    )


    return all_offers


# ============================================================
# RECHERCHE AVEC CACHE
# ============================================================

async def run_full_search():

    # Cas le plus rapide :
    # le résultat récent est déjà disponible.
    if cache_is_valid():

        print(
            "Résultats servis depuis le cache."
        )

        return _search_cache["offers"]


    # Une seule recherche complète à la fois.
    async with _search_lock:

        # Pendant que nous attendions le verrou,
        # une autre requête a peut-être rempli le cache.
        if cache_is_valid():

            print(
                "Résultats servis depuis le cache."
            )

            return _search_cache["offers"]


        print(
            "Démarrage d'une nouvelle "
            "recherche Duffel."
        )


        offers = await perform_full_search()


        _search_cache["offers"] = offers

        _search_cache["created_at"] = (
            time.monotonic()
        )


        return offers


# ============================================================
# ROUTE /
# ============================================================

@app.get("/")
async def root():

    return {
        "status": "ok",

        "search_window": {
            "departure": [
                d.isoformat()
                for d in DEPARTURE_DATES
            ],

            "return": [
                d.isoformat()
                for d in RETURN_DATES
            ],
        },

        "thresholds": {
            route["label"]:
                route["max_price_eur"]
            for route in ROUTES
        },

        "cache_seconds":
            CACHE_TTL_SECONDS,
    }


# ============================================================
# ROUTE /search
# ============================================================

@app.get("/search")
async def search():

    try:

        offers = await run_full_search()


    except httpx.TimeoutException as exc:

        raise HTTPException(
            status_code=504,
            detail=(
                "Timeout lors de la "
                "communication avec Duffel: "
                f"{exc}"
            ),
        )


    except httpx.HTTPError as exc:

        raise HTTPException(
            status_code=502,
            detail=(
                "Erreur réseau avec Duffel: "
                f"{exc}"
            ),
        )


    except RuntimeError as exc:

        raise HTTPException(
            status_code=502,
            detail=str(exc),
        )


    by_route = {}


    for route in ROUTES:

        matches = [
            offer
            for offer in offers
            if (
                offer["route_key"]
                == route["key"]
            )
        ]


        by_route[route["key"]] = {
            "label":
                route["label"],

            "threshold":
                route["max_price_eur"],

            "offers":
                matches[:20],
        }


    return {
        "best_offer":
            offers[0]
            if offers
            else None,

        "total_matches":
            len(offers),

        "routes":
            by_route,

        "departure_dates": [
            d.isoformat()
            for d in DEPARTURE_DATES
        ],

        "return_dates": [
            d.isoformat()
            for d in RETURN_DATES
        ],

        "cache_seconds":
            CACHE_TTL_SECONDS,

        "notice":
            "Les prix peuvent changer "
            "ou expirer avant réservation.",
    }