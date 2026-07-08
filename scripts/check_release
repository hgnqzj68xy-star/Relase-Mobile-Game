#!/usr/bin/env python3
"""
check_releases.py
Vérifie tous les 2 jours les jeux "upcoming" dont la date est passée
et met à jour leur statut vers "released" si confirmé sur les stores.
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

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
DATA_FILE   = Path(__file__).parent.parent / "data" / "games.json"
BACKUP_FILE = Path(__file__).parent.parent / "data" / "games.backup.json"

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
    tmp = DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(DATA_FILE)
    log.info(f"Sauvegarde : {len(data['games'])} jeux")

# ── Vérification iOS via iTunes Lookup ───────────────────────────────────────
def check_ios_released(bundle_id: str, app_id: str) -> dict | None:
    """
    Vérifie sur iTunes si le jeu est sorti.
    Retourne les données mises à jour ou None si pas encore sorti.
    """
    # Extraire le vrai app_id depuis l'id du jeu (ios_XXXXXXX)
    itunes_id = app_id.replace("ios_", "")
    if not itunes_id.isdigit():
        return None

    try:
        resp = requests.get(
            "https://itunes.apple.com/lookup",
            params={
                "id":      itunes_id,
                "country": "fr",
                "entity":  "software",
            },
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None

        item = results[0]

        # Vérifier la date de sortie réelle
        release_raw = item.get("releaseDate", "")
        try:
            release_dt = datetime.fromisoformat(release_raw.replace("Z", ""))
        except Exception:
            return None

        now = datetime.utcnow()

        # Le jeu est sorti si la date est dans le passé
        if release_dt <= now:
            return {
                "status":      "released",
                "releaseDate": release_dt.strftime("%Y-%m-%d"),
                "rating":      round(item.get("averageUserRating", 0), 1) or None,
                "price":       item.get("formattedPrice", "Free"),
            }

        return None  # Pas encore sorti

    except Exception as e:
        log.warning(f"  iTunes lookup error pour {itunes_id}: {e}")
        return None

# ── Vérification Android via Google Play ─────────────────────────────────────
def check_android_released(bundle_id: str) -> dict | None:
    """
    Vérifie sur Google Play FR si le jeu est sorti
    (plus de mention pre-register).
    """
    if not bundle_id:
        return None

    url = f"https://play.google.com/store/apps/details?id={bundle_id}&hl=fr&gl=FR"

    try:
        resp = requests.get(url, headers=HEADERS_MOBILE, timeout=20)

        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            log.warning("  429 Rate limit — attente 15s")
            time.sleep(15)
            resp = requests.get(url, headers=HEADERS_MOBILE, timeout=20)

        resp.raise_for_status()
        raw = resp.text

        # App inexistante
        if any(kw in raw for kw in ["Nous n'avons pas pu trouver", "not found"]):
            return None

        # Vérifier si le jeu est toujours en pre-registration
        still_upcoming = any(kw in raw.lower() for kw in [
            "pre-register", "preregister", "pre_register",
            "preregistration", "préinscription",
        ])

        if still_upcoming:
            log.info(f"  -> Toujours en pre-register : {bundle_id}")
            return None

        # Le jeu est sorti : extraire la note si disponible
        rating = None
        for pat in (r'"starRating"\s*:\s*"?([\d.]+)"?', r'(\d\.\d)\s*sur\s*5'):
            m = re.search(pat, raw)
            if m:
                try:
                    rating = round(float(m.group(1)), 1)
                    break
                except Exception:
                    pass

        # Extraire le prix
        price = "Free"
        pm = re.search(r'"price"\s*:\s*"([^"]*)"', raw)
        if pm:
            p = pm.group(1).strip()
            price = "Free" if p in ("0", "", "Free", "Gratuit") else p

        return {
            "status": "released",
            "rating": rating,
            "price":  price,
        }

    except Exception as e:
        log.warning(f"  Google Play check error pour {bundle_id}: {e}")
        return None

# ── Vérification des titres à surveiller ─────────────────────────────────────
def check_new_releases_today(games: list[dict]) -> tuple[list[dict], int]:
    """
    Parcourt tous les jeux :
    1. Les "upcoming" dont la date est passée → vérifie si sorti
    2. Les "released" récents → vérifie la note si elle manque
    Retourne la liste mise à jour et le nombre de changements.
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

        # ── Cas 1 : Jeu upcoming dont la date est passée ──────────────────
        if status == "upcoming" and release_date <= today:
            log.info(f"[CHECK] {game['title']} — date passée, vérification...")

            new_data = None

            # Vérifier selon la source
            if game.get("source") == "itunes" or "ios" in game.get("platform", []):
                new_data = check_ios_released(
                    game.get("bundleId", ""), game.get("id", "")
                )
                time.sleep(0.3)

            if new_data is None and game.get("source") == "gplay" or "android" in game.get("platform", []):
                new_data = check_android_released(game.get("bundleId", ""))
                time.sleep(0.5)

            if new_data:
                log.info(f"  -> SORTI ! Mise a jour : {game['title']}")
                game["status"]      = "released"
                game["releaseDate"] = new_data.get("releaseDate", game["releaseDate"])
                if new_data.get("rating"):
                    game["rating"]  = new_data["rating"]
                if new_data.get("price"):
                    game["price"]   = new_data["price"]
                changes += 1
            else:
                # Toujours pas sorti — on garde upcoming mais on log
                log.info(f"  -> Pas encore confirme sorti : {game['title']}")

        # ── Cas 2 : Jeu released récent sans note → tenter de récupérer ──
        elif (
            status == "released"
            and game.get("rating") is None
            and release_date >= (today - timedelta(days=14))
        ):
            log.info(f"[NOTE] {game['title']} — récupération note...")

            if "ios" in game.get("platform", []):
                note_data = check_ios_released(
                    game.get("bundleId", ""), game.get("id", "")
                )
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
    log.info("=== Check Releases ===")
    log.info(f"Date : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    data  = load_data()
    games = data.get("games", [])

    if not games:
        log.warning("Aucun jeu en base — rien a verifier")
        return

    # Stats initiales
    upcoming_before = sum(1 for g in games if g.get("status") == "upcoming")
    log.info(f"Jeux en base       : {len(games)}")
    log.info(f"Upcoming a checker : {upcoming_before}")

    backup_data()

    # Vérification
    updated_games, changes = check_new_releases_today(games)

    # Stats finales
    upcoming_after  = sum(1 for g in updated_games if g.get("status") == "upcoming")
    released_after  = sum(1 for g in updated_games if g.get("status") == "released")

    log.info("=" * 40)
    log.info(f"Changements        : {changes}")
    log.info(f"Upcoming -> Released : {upcoming_before - upcoming_after}")
    log.info(f"Total upcoming     : {upcoming_after}")
    log.info(f"Total released     : {released_after}")
    log.info("=" * 40)

    if changes > 0:
        data["games"] = updated_games
        save_data(data)
        log.info(f"JSON mis a jour avec {changes} changements")
    else:
        log.info("Aucun changement — JSON non modifie")

    elapsed = time.time() - start
    log.info(f"Termine en {elapsed:.1f}s")

if __name__ == "__main__":
    main()
