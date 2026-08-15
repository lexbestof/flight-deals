# Alerte vols Kinshasa — version avec Telegram

## Recherche configurée

L'application teste automatiquement les 9 combinaisons de dates :

- départ : 18, 19 ou 20 août 2026
- retour : 29, 30 ou 31 août 2026

Pour chacun des deux itinéraires :

- Paris (PAR) ↔ Kinshasa (FIH) : maximum 750 EUR
- Bruxelles (BRU) ↔ Kinshasa (FIH) : maximum 650 EUR

Le prix est celui de l'aller-retour complet pour 1 adulte.

## Architecture

- Frontend statique : Netlify
- Backend Python/FastAPI : Render
- Recherche vols : Duffel API
- Notifications : Telegram Bot API
- Surveillance : Render Cron Job toutes les 6 heures

## 1. Duffel

Crée un compte Duffel et récupère un access token LIVE.
Ajoute-le sur Render :

DUFFEL_ACCESS_TOKEN=...

Le mode test Duffel ne représente pas les vrais prix du marché.

## 2. Telegram

### Créer le bot

1. Dans Telegram, ouvre @BotFather.
2. Envoie `/newbot`.
3. Choisis un nom et un username.
4. Copie le token donné par BotFather.
5. Ouvre ensuite TON nouveau bot et appuie sur Start / envoie `/start`.

### Obtenir ton Chat ID

Après avoir envoyé `/start` au bot, ouvre dans ton navigateur :

https://api.telegram.org/bot<TON_TOKEN>/getUpdates

Dans la réponse JSON, cherche :

"chat":{"id":123456789,...}

Le nombre est ton TELEGRAM_CHAT_ID.

Ne mets jamais le token Telegram dans le frontend Netlify.

## 3. Render

Déploie ce dépôt avec `render.yaml`, ou crée :

### Web Service
- Root directory : backend
- Build : pip install -r requirements.txt
- Start : uvicorn main:app --host 0.0.0.0 --port $PORT

### Cron Job
- Root directory : backend
- Schedule : `0 */6 * * *`
- Build : pip install -r requirements.txt
- Command : python watcher.py

Variables secrètes du Cron :
- DUFFEL_ACCESS_TOKEN
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

Le cron s'arrête logiquement après le 20 août 2026 : watcher.py ne lance plus de recherche après cette date.

## 4. Netlify

Dans frontend/index.html, remplace :

https://REMPLACE-MOI.onrender.com

par l'URL de ton Web Service Render.

Puis déploie la racine du dépôt sur Netlify.
Le publish directory est `frontend`.

## Comportement Telegram

À chaque vérification :
- s'il n'y a aucune offre sous les seuils : aucun message ;
- s'il y en a : Telegram envoie la meilleure offre trouvée.

Attention : sans base de données persistante, une même bonne offre peut être renvoyée lors de plusieurs vérifications.
