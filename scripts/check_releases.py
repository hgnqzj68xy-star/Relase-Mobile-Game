#!/usr/bin/env python3
"""
check_releases.py v2
- Vérifie les jeux "upcoming" dont la date est passée
- Force la mise à jour du statut via iTunes + Google Play
- Purge les jeux trop anciens (> 90j) ou en doublon
"""

import json, os, time, re, logging, shutil
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    os.system("pip install requests --break-system-packages -q")
    import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    os.system("pip install beautifulsoup4 --break-system-packages -q")
    from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DATA_FILE   = Path(__file__).parent.parent / "data" / "games.json"
BACKUP_FILE = Path(__file__).parent.parent / "data" / "games.backup.json"
MAX_GAMES   = 300   # seuil d'alerte si trop de jeux en base
KEEP_DAYS   = 90    # purger les jeux sortis depuis plus de 90j

HEADERS_MOBILE = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
    ),
    "Accept-Language":           "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding":           "gzip, deflate, br",
    "Connection":                "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Cache-Control":             "max-age=0",
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"lastUpdated": "", "games": []}

def backup_data():
    if DATA_FILE.exists():
        shutil.copy2(DATA_FILE, BACKUP_FILE)
        log.info(f"Backup cree : {BACKUP_FILE}")

def save_data(data):
    data["lastUpdated"] = datetime.utcnow().isoformat() + "Z"
    data["totalGames"]  = len(data.get("games", []))
    tmp = DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(DATA_FILE)
    log.info(f"Sauvegarde : {len(data['games'])} jeux")

# ── Purge des doublons et anciens jeux ───────────────────────────────────────
def purge_games(games: list[dict]) -> list[dict]:
    """
    1. Supprime les jeux released depuis plus de KEEP_DAYS jours
    2. Déduplique par titre normalisé (garde le plus récent)
    3. Alerte si trop de jeux en base
    """
    now    = datetime.utcnow()
    cutoff = now - timedelta(days=KEEP_DAYS)
    today  = now.date()

    # Purge temporelle : on garde les upcoming et les released récents
    kept = []
    purged_count = 0
    for g in games:
        try:
            dt = datetime.strptime(g["releaseDate"], "%Y-%m-%d")
        except Exception:
            kept.append(g)
            continue

        status = g.get("status", "released")

        # Toujours garder les upcoming
        if status == "upcoming":
            # Sauf si la date est passée de plus de 30j sans être confirmée
            if dt.date() < (today - timedelta(days=30)):
                log.warning(f"  Upcoming trop ancien purgé : {g['title']} ({g['releaseDate']})")
                purged_count += 1
                continue
            kept.append(g)
        else:
            # Released : purger si trop ancien
            if dt < cutoff:
                purged_count += 1
                continue
            kept.append(g)

    log.info(f"Purge temporelle : {purged_count} jeux supprimés")

    # Déduplication par titre normalisé
    by_title = {}
    for g in kept:
        key = re.sub(r'\s+', ' ', g.get("title", "").strip().lower())
        if key not in by_title:
            by_title[key] = g
        else:
            # Garder celui avec le plus d'infos
            existing = by_title[key]
            if len(g.get("platform", [])) > len(existing.get("platform", [])):
                by_title[key] = g
            elif g.get("rating") and not existing.get("rating"):
                by_title[key] = g

    deduped = list(by_title.values())
    dedup_removed = len(kept) - len(deduped)
    if dedup_removed > 0:
        log.info(f"Déduplication : {dedup_removed} doublons supprimés")

    if len(deduped) > MAX_GAMES:
        log.warning(f"ALERTE : {len(deduped)} jeux en base (seuil={MAX_GAMES})")

    return deduped

# ── Vérification iOS via iTunes Lookup ───────────────────────────────────────
def check_ios_released(game: dict) -> dict | None:
    """
    Vérifie sur le store iTunes FR si le jeu est sorti.
    Supporte les IDs numériques et les bundleIds.
    """
    # Extraire l'ID numérique iTunes
    game_id   = game.get("id", "")
    bundle_id = game.get("bundleId", "")

    # L'id est de la forme "ios_1234567890"
    itunes_id = re.sub(r'^ios_', '', game_id)

    # Si l'id n'est pas numérique, essayer via bundleId
    lookup_param = {}
    if itunes_id.isdigit():
        lookup_param = {"id": itunes_id}
    elif bundle_id:
        lookup_param = {"bundleId": bundle_id}
    else:
        log.warning(f"  Impossible de vérifier iOS : pas d'ID valide pour {game.get('title')}")
        return None

    try:
        resp = requests.get(
            "https://itunes.apple.com/lookup",
            params={**lookup_param, "country": "fr", "entity": "software"},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])

        if not results:
            log.info(f"  iTunes : aucun résultat pour {game.get('title')}")
            return None

        item       = results[0]
        release_raw = item.get("releaseDate", "")

        try:
            release_dt = datetime.fromisoformat(release_raw.replace("Z", ""))
        except Exception:
            return None

        # Sorti si date dans le passé
        if release_dt.date() <= datetime.utcnow().date():
            rating = item.get("averageUserRating", 0)
            return {
                "status":      "released",
                "releaseDate": release_dt.strftime("%Y-%m-%d"),
                "rating":      round(rating, 1) if rating else None,
                "price":       "Free" if item.get("price", 0) == 0 else f"{item.get('price', 0):.2f}€",
            }

        return None  # Pas encore sorti

    except Exception as e:
        log.warning(f"  iTunes lookup error pour {game.get('title')}: {e}")
        return None

# ── Vérification Android via Google Play ─────────────────────────────────────
def check_android_released(bundle_id: str, title: str) -> dict | None:
    """
    Vérifie sur Google Play FR si le jeu est sorti
    (absence de mention pre-register).
    """
    if not bundle_id:
        return None

    url = f"https://play.google.com/store/apps/details?id={bundle_id}&hl=fr&gl=FR"

    try:
        resp = requests.get(url, headers=HEADERS_MOBILE, timeout=20)

        if resp.status_code == 404:
            log.info(f"  Google Play : app introuvable {bundle_id}")
            return None
        if resp.status_code == 429:
            log.warning("  429 Rate limit — attente 15s")
            time.sleep(15)
            resp = requests.get(url, headers=HEADERS_MOBILE, timeout=20)

        resp.raise_for_status()
        raw = resp.text

        if any(kw in raw for kw in ["Nous n'avons pas pu trouver", "not found"]):
            return None

        # Toujours en pre-registration ?
        still_upcoming = any(kw in raw.lower() for kw in [
            "pre-register", "preregister", "pre_register",
            "preregistration", "préinscription",
        ])

        if still_upcoming:
            log.info(f"  -> Toujours pre-register : {title}")
            return None

        # Sorti ! Extraire note et prix
        rating = None
        for pat in (r'"starRating"\s*:\s*"?([\d.]+)"?', r'(\d\.\d)\s*sur\s*5'):
            m = re.search(pat, raw)
            if m:
                try:
                    rating = round(float(m.group(1)), 1)
                    break
                except Exception:
                    pass

        price = "Free"
        pm = re.search(r'"price"\s*:\s*"([^"]*)"', raw)
        if pm:
            p = pm.group(1).strip()
            price = "Free" if p in ("0", "", "Free", "Gratuit") else p

        return {"status": "released", "rating": rating, "price": price}

    except Exception as e:
        log.warning(f"  Google Play check error pour {title}: {e}")
        return None

# ── Vérification et mise à jour des statuts ───────────────────────────────────
def check_and_update(games: list[dict]) -> tuple[list[dict], int]:
    """
    Pour chaque jeu upcoming dont la date est passée :
    - Vérifie sur iOS et/ou Android si sorti
    - Met à jour le statut

    Pour les released récents sans note :
    - Tente de récupérer la note
    """
    now     = datetime.utcnow()
    today   = now.date()
    changes = 0
    updated = []

    for game in games:
        try:
            release_dt = datetime.strptime(game["releaseDate"], "%Y-%m-%d")
        except Exception:
            updated.append(game)
            continue

        release_date = release_dt.date()
        status       = game.get("status", "released")
        platforms    = game.get("platform", [])
        bundle_id    = game.get("bundleId", "")
        title        = game.get("title", "")

        # ── Cas 1 : Upcoming dont la date est passée ──────────────────────
        if status == "upcoming" and release_date <= today:
            log.info(f"[CHECK] {title} — sortie prévue le {release_date}")
            new_data = None

            # Vérifier iOS en priorité
            if "ios" in platforms:
                new_data = check_ios_released(game)
                time.sleep(0.3)

            # Si pas confirmé iOS, tenter Android
            if new_data is None and "android" in platforms and bundle_id:
                new_data = check_android_released(bundle_id, title)
                time.sleep(0.5)

            if new_data:
                log.info(f"  -> ✅ SORTI : {title}")
                game["status"] = "released"
                if new_data.get("releaseDate"):
                    game["releaseDate"] = new_data["releaseDate"]
                if new_data.get("rating"):
                    game["rating"] = new_data["rating"]
                if new_data.get("price"):
                    game["price"] = new_data["price"]
                changes += 1
            else:
                log.info(f"  -> ⏳ Pas encore confirmé sorti : {title}")

        # ── Cas 2 : Released récent sans note ─────────────────────────────
        elif (
            status == "released"
            and game.get("rating") is None
            and release_date >= (today - timedelta(days=14))
            and "ios" in platforms
        ):
            log.info(f"[NOTE] {title} — récupération note...")
            note_data = check_ios_released(game)
            if note_data and note_data.get("rating"):
                game["rating"] = note_data["rating"]
                log.info(f"  -> Note récupérée : {game['rating']}")
                changes += 1
            time.sleep(0.3)

        updated.append(game)

    return updated, changes

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    start = time.time()
    log.info("=== Check Releases v2 ===")
    log.info(f"Date : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    data  = load_data()
    games = data.get("games", [])

    if not games:
        log.warning("Aucun jeu en base")
        return

    log.info(f"Jeux en base avant purge : {len(games)}")

    backup_data()

    # Purge des anciens et doublons en premier
    games = purge_games(games)
    log.info(f"Jeux après purge : {len(games)}")

    # Stats avant vérification
    upcoming_before = sum(1 for g in games if g.get("status") == "upcoming")
    log.info(f"Upcoming à vérifier : {upcoming_before}")

    # Vérification des statuts
    updated_games, changes = check_and_update(games)

    # Stats finales
    upcoming_after = sum(1 for g in updated_games if g.get("status") == "upcoming")
    released_after = sum(1 for g in updated_games if g.get("status") == "released")
    newly_released = upcoming_before - upcoming_after

    log.info("=" * 40)
    log.info(f"Nouveaux released    : {newly_released}")
    log.info(f"Changements total    : {changes}")
    log.info(f"Upcoming restants    : {upcoming_after}")
    log.info(f"Released total       : {released_after}")
    log.info(f"Total jeux en base   : {len(updated_games)}")
    log.info("=" * 40)

    # Sauvegarder si changements ou si purge a réduit le nombre
    if changes > 0 or len(updated_games) != len(data.get("games", [])):
        data["games"] = updated_games
        save_data(data)
    else:
        log.info("Aucun changement — JSON non modifié")

    elapsed = time.time() - start
    log.info(f"Terminé en {elapsed:.1f}s")

if __name__ == "__main__":
    main()
