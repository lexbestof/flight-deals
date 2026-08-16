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
# SECTIONS AFFICHÉES SUR LE SITE
# ============================================================

ROUTES = [
    {
        "key": "paris",
        "label": "Paris ↔ Kinshasa",
        "origin": "PAR",
        "destination": "FIH",
        "min_price_eur": 0.0,
        "max_price_eur": 750.0,
    },

    {
        "key": "brussels",
        "label": "Bruxelles ↔ Kinshasa",
        "origin": "BRU",
        "destination": "FIH",
        "min_price_eur": 0.0,
        "max_price_eur": 650.0,
    },

    {
        "key": "paris_650_1000",
        "label": "Paris ↔ Kinshasa",
        "origin": "PAR",
        "destination": "FIH",
        "min_price_eur": 650.0,
        "max_price_eur": 1000.0,
    },

    {
        "key": "paris_1001_2000",
        "label": "Paris ↔ Kinshasa",
        "origin": "PAR",
        "destination": "FIH",
        "min_price_eur": 1001.0,
        "max_price_eur": 2000.0,
    },
]


# ============================================================
# TRAJETS RÉELLEMENT INTERROGÉS CHEZ DUFFEL
# ============================================================

# Important :
#
# Même si nous avons maintenant 3 sections sur le site,
# il n'y a que 2 trajets différents à rechercher chez Duffel :
#
#   PAR -> FIH
#   BRU -> FIH
#
# Cela évite de rechercher deux fois exactement les mêmes
# vols Paris -> Kinshasa.

SEARCH_ROUTES = [
    {
        "key": "paris_search",
        "origin": "PAR",
        "destination": "FIH",

        # Une seule recherche Paris jusqu'à 2000 €.
        # Les résultats seront ensuite répartis
        # entre les différentes sections Paris.
        "max_price_eur": 2000.0,
    },

    {
        "key": "brussels_search",
        "origin": "BRU",
        "destination": "FIH",
        "max_price_eur": 650.0,
    },
]

# ============================================================
# DATES
# ============================================================

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

    age = (
        time.monotonic()
        - _search_cache["created_at"]
    )

    return age < CACHE_TTL_SECONDS


# ============================================================
# FORMATAGE DES SEGMENTS DE VOL
# ============================================================

def segment_summary(
    slice_data: dict[str, Any]
) -> dict[str, Any]:

    segments = (
        slice_data.get("segments")
        or []
    )

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
    Lit l'en-tête ratelimit-reset envoyé par Duffel
    et calcule combien de secondes attendre.
    """

    reset_header = response.headers.get(
        "ratelimit-reset"
    )

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

        now = datetime.now(
            timezone.utc
        )

        seconds = (
            reset_time - now
        ).total_seconds()

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

    En cas de HTTP 429, attend automatiquement
    avant de réessayer.
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

        # Pas de rate limit.
        if response.status_code != 429:
            return response

        # Dernière tentative déjà atteinte.
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

    raise RuntimeError(
        "Impossible de contacter Duffel."
    )


# ============================================================
# RECHERCHE D'UNE COMBINAISON DE DATES
# ============================================================

async def search_one(
    client: httpx.AsyncClient,
    search_route: dict[str, Any],
    departure: date,
    return_date: date,
):

    payload = {
        "data": {

            "slices": [
                {
                    "origin":
                        search_route["origin"],

                    "destination":
                        search_route["destination"],

                    "departure_date":
                        departure.isoformat(),
                },

                {
                    "origin":
                        search_route["destination"],

                    "destination":
                        search_route["origin"],

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

        # Seulement les tarifs en euros.
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


        # On ne garde pas les offres au-dessus
        # du plafond nécessaire à cette recherche.
        if (
            amount
            > search_route["max_price_eur"]
        ):
            continue


        slices = (
            offer.get("slices")
            or []
        )


        good_offers.append(
            {
                "id":
                    offer.get("id"),

                # Ici on conserve le trajet physique.
                "origin":
                    search_route["origin"],

                "destination":
                    search_route["destination"],

                "price":
                    amount,

                "currency":
                    "EUR",

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

                # Permet toujours au frontend d'indiquer
                # s'il s'agit du mode test Duffel.
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


        for search_route in SEARCH_ROUTES:

            for departure in DEPARTURE_DATES:

                for return_date in RETURN_DATES:

                    if return_date <= departure:
                        continue


                    print(
                        "Recherche Duffel : "
                        f"{search_route['origin']} → "
                        f"{search_route['destination']} "
                        f"{departure} / "
                        f"{return_date}"
                    )


                    offers = await search_one(
                        client,
                        search_route,
                        departure,
                        return_date,
                    )


                    all_offers.extend(
                        offers
                    )


                    # Petite pause pour limiter les risques
                    # de HTTP 429.
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

    if cache_is_valid():

        print(
            "Résultats servis depuis le cache."
        )

        return _search_cache["offers"]


    async with _search_lock:


        # Le cache a peut-être été rempli pendant
        # que cette requête attendait le verrou.
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
# FILTRAGE D'UNE SECTION
# ============================================================

def offers_for_route(
    offers: list[dict[str, Any]],
    route: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Prend les résultats Duffel et crée les offres
    correspondant à une section du site.

    Cela permet notamment d'utiliser une seule recherche Paris
    pour alimenter :

    - Paris <= 750 €
    - Paris entre 650 et 1000 €
    """

    matches = []


    for offer in offers:

        # Le trajet doit correspondre.
        if (
            offer["origin"]
            != route["origin"]
        ):
            continue

        if (
            offer["destination"]
            != route["destination"]
        ):
            continue


        price = offer["price"]


        # Limite basse INCLUSE.
        if (
            price
            < route["min_price_eur"]
        ):
            continue


        # Limite haute INCLUSE.
        if (
            price
            > route["max_price_eur"]
        ):
            continue


        # On copie l'offre pour pouvoir ajouter
        # les informations propres à cette section.
        result = offer.copy()

        result["route_key"] = (
            route["key"]
        )

        result["route"] = (
            route["label"]
        )

        result["min_price"] = (
            route["min_price_eur"]
        )

        result["threshold"] = (
            route["max_price_eur"]
        )

        matches.append(
            result
        )


    matches.sort(
        key=lambda offer:
            offer["price"]
    )


    return matches


# ============================================================
# ROUTE /
# ============================================================

@app.get("/")
async def root():

    return {
        "status":
            "ok",

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

        "price_ranges": {

            route["key"]: {
                "label":
                    route["label"],

                "min":
                    route["min_price_eur"],

                "max":
                    route["max_price_eur"],
            }

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

    all_section_offers = []


    for route in ROUTES:

        matches = offers_for_route(
            offers,
            route,
        )

        all_section_offers.extend(
            matches
        )


        by_route[route["key"]] = {

            "label":
                route["label"],

            "min_price":
                route["min_price_eur"],

            "threshold":
                route["max_price_eur"],

            "offers":
                matches[:20],
        }


    # Attention :
    # une même offre Paris peut appartenir à plusieurs sections.
    # Pour la meilleure offre générale, on utilise donc
    # directement les résultats Duffel originaux.
    best_offer = None


    if offers:

        cheapest = offers[0]

        # On détermine une étiquette lisible.
        if cheapest["origin"] == "PAR":
            route_label = "Paris ↔ Kinshasa"

        elif cheapest["origin"] == "BRU":
            route_label = "Bruxelles ↔ Kinshasa"

        else:
            route_label = (
                f"{cheapest['origin']} ↔ "
                f"{cheapest['destination']}"
            )


        best_offer = cheapest.copy()

        best_offer["route"] = (
            route_label
        )


    return {

        "best_offer":
            best_offer,

        "total_matches":
            len(all_section_offers),

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