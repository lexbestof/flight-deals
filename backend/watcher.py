import asyncio
import os
from datetime import date
import httpx

from main import run_full_search

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def fmt_trip(offer):
    out = offer.get("outbound") or {}
    ret = offer.get("return") or {}
    airlines = ", ".join(out.get("airlines") or []) or offer.get("owner") or "Compagnie non indiquée"
    return (
        f"✈️ BON PLAN TROUVÉ\n\n"
        f"{offer['route']}\n"
        f"💶 {offer['price']:.2f} € aller-retour\n"
        f"📅 Aller : {offer['departure_date']}\n"
        f"📅 Retour : {offer['return_date']}\n"
        f"🏷️ Plafond : {offer['threshold']:.0f} €\n"
        f"🛫 Compagnie : {airlines}\n"
        f"🔁 Escales aller : {out.get('stops', '?')}\n"
        f"🔁 Escales retour : {ret.get('stops', '?')}\n"
        f"⏳ Offre indiquée jusqu'à : {offer.get('expires_at') or 'non indiqué'}\n\n"
        f"Le tarif peut changer : vérifie-le avant réservation."
    )


async def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
        r.raise_for_status()


async def main():
    if date.today() > date(2026, 8, 20):
        print("La fenêtre de départ est passée : aucune recherche.")
        return

    offers = await run_full_search()

    # Une seule notification par exécution : la meilleure offre disponible.
    # Cela évite de recevoir 10+ messages pour des variantes très proches.
    if not offers:
        print("Aucune offre sous les plafonds.")
        return

    best = offers[0]
    print(f"Meilleure offre: {best['route']} {best['price']:.2f} EUR")
    await send_telegram(fmt_trip(best))


if __name__ == "__main__":
    asyncio.run(main())
