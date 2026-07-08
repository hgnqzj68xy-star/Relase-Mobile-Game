#!/usr/bin/env python3
"""
Mobile Games Release Scraper v3
- iOS     : iTunes Search API + vérification disponibilité France
- Android : corrélation bundleId iOS -> Google Play + vérification géo France
- Cache   : évite de re-scraper les jeux déjà connus
- Logging : structuré avec horodatage
- Safety  : backup JSON avant écrasement, validation avant save
"""

import json, os, time, re, logging, shutil
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

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
DATA_FILE        = Path(__file__).parent.parent / "data" / "games.json"
BACKUP_FILE      = Path(__file__).parent.parent / "data" / "games.backup.json"
LOOKBACK_DAYS    = 60   # 60j passes
LOOKAHEAD_DAYS   = 30   # 30j futur uniquement
ANDROID_WORKERS  = 5
CHECK_FR         = os.environ.get("CHECK_FR", "false").lower() == "true"

IOS_SEARCH_TERMS = [
    # Termes anglais (majorité des jeux mobiles)
    "new game", "rpg", "action game", "puzzle", "strategy",
    "adventure", "simulation", "card game", "casual game", "platformer",
    "new ios game", "mobile rpg", "mobile action", "new release game",
    "new mobile game 2026", "game release 2026", "open world mobile",
    "battle royale mobile", "tower defense", "idle game", "gacha game",
    # Termes français (jeux localisés FR)
    "nouveau jeu", "jeu de role", "jeu de strategie", "jeu de cartes",
    "jeu de puzzle", "jeu d'aventure", "simulation mobile",
]

GENRES = {
    "6014":"Games",     "7001":"Action",       "7002":"Adventure",
    "7003":"Arcade",    "7004":"Board",         "7005":"Card",
    "7006":"Casino",    "7007":"Dice",          "7008":"Educational",
    "7009":"Family",    "7010":"Kids",          "7011":"Music",
    "7012":"Puzzle",    "7013":"Racing",        "7014":"Role Playing",
    "7015":"Simulation","7016":"Sports",        "7017":"Strategy",
    "7018":"Trivia",    "7019":"Word",
}

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

# Mots-clés indiquant une restriction géographique France
GEO_BLOCK_KEYWORDS = [
    "not available in your country",
    "pas disponible dans votre pays",
    "not available for your device",
    "cette application n'est pas compatible",
    "isn't available in your country",
    "not available in france",
    "unavailable in your country",
    "cette appli n'est pas disponible",
]

# ── Helpers ───────────────────────────────────────────────────────────────────
def load_existing():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                data = json.load(f)
            log.info(f"JSON charge : {len(data.get('games', []))} jeux existants")
            return data
        except json.JSONDecodeError as e:
            log.error(f"JSON corrompu : {e} — on repart de zero")
    return {"lastUpdated": "", "games": []}

def backup_existing():
    if DATA_FILE.exists():
        shutil.copy2(DATA_FILE, BACKUP_FILE)
        log.info(f"Backup cree : {BACKUP_FILE}")

def save_data(data):
    games = data.get("games", [])
    required_fields = {"id", "title", "platform", "releaseDate"}
    invalid = [g for g in games if not required_fields.issubset(g.keys())]
    if invalid:
        log.warning(f"{len(invalid)} entrees invalides ignorees")
        games = [g for g in games if required_fields.issubset(g.keys())]

    data["games"]       = games
    data["lastUpdated"] = datetime.utcnow().isoformat() + "Z"
    data["totalGames"]  = len(games)

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(DATA_FILE)
    log.info(f"Sauvegarde : {len(games)} jeux -> {DATA_FILE}")

def ios_artwork_hd(url, size=512):
    if not url:
        return url
    return re.sub(r'\d+x\d+bb\.(jpg|png|webp)', f'{size}x{size}bb.jpg', url)

def normalize_title(title):
    return re.sub(r'\s+', ' ', title.strip().lower())

def format_price(price_val):
    try:
        v = float(price_val)
        return "Free" if v == 0 else f"{v:.2f}€"
    except (TypeError, ValueError):
        s = str(price_val).strip()
        return "Free" if s in ("0", "0.0", "", "None", "Free", "Gratuit") else s

def parse_date_flexible(raw):
    if not raw:
        return None
    raw_str = str(raw).strip()

    # Timestamp Unix
    try:
        ts = int(raw_str)
        if ts > 1_000_000_000:
            return datetime.utcfromtimestamp(ts)
    except (ValueError, TypeError):
        pass

    # Format francais : "15 mai 2026"
    mois_fr = {
        "janvier":1,  "fevrier":2,   "mars":3,     "avril":4,
        "mai":5,      "juin":6,      "juillet":7,  "aout":8,
        "septembre":9,"octobre":10,  "novembre":11,"decembre":12,
    }
    raw_lower = raw_str.lower()
    m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', raw_lower)
    if m:
        day_s, month_s, year_s = m.group(1), m.group(2), m.group(3)
        month_s = (month_s
            .replace('\u00e9','e').replace('\u00fb','u')
            .replace('\u00e8','e').replace('\u00fb','u'))
        if month_s in mois_fr:
            try:
                return datetime(int(year_s), mois_fr[month_s], int(day_s))
            except Exception:
                pass

    # Suffixes ordinaux anglais
    raw_str = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', raw_str)

    for fmt in (
        "%B %d, %Y", "%b %d, %Y",
        "%d %B %Y",  "%d %b %Y",
        "%Y-%m-%d",  "%d/%m/%Y",  "%m/%d/%Y",
        "%B %Y",     "%b %Y",
    ):
        try:
            return datetime.strptime(raw_str.strip(), fmt)
        except ValueError:
            pass

    # Trimestre Q1/Q2/Q3/Q4
    q = re.match(r'Q([1-4])\s+(\d{4})', raw_str.strip())
    if q:
        return datetime(int(q.group(2)), (int(q.group(1)) - 1) * 3 + 1, 1)

    # Fallback annee + mois texte anglais
    yr = re.search(r'(\d{4})', raw_str)
    if yr:
        year = int(yr.group(1))
        months_en   = ["january","february","march","april","may","june",
                       "july","august","september","october","november","december"]
        months_abbr = ["jan","feb","mar","apr","may","jun",
                       "jul","aug","sep","oct","nov","dec"]
        rl = raw_str.lower()
        for i, (full, abbr) in enumerate(zip(months_en, months_abbr), 1):
            if full in rl or abbr in rl:
                dm = re.search(r'\b(\d{1,2})\b', raw_str)
                day = int(dm.group(1)) if dm else 1
                try:
                    return datetime(year, i, min(day, 28))
                except Exception:
                    return datetime(year, i, 1)
        return datetime(year, 1, 1)
    return None

def in_window(date_str):
    try:
        dt  = datetime.strptime(date_str, "%Y-%m-%d")
        now = datetime.utcnow()
        return (now - timedelta(days=LOOKBACK_DAYS)) <= dt <= (now + timedelta(days=LOOKAHEAD_DAYS))
    except Exception:
        return False

# ── Vérification disponibilité France iOS ────────────────────────────────────
def is_available_france_ios(app_id: str) -> bool:
    """
    Vérifie via iTunes Lookup API que l'app est disponible sur le store FR.
    Retourne True par défaut en cas d'erreur pour ne pas bloquer le scraping.
    """
    try:
        resp = requests.get(
            "https://itunes.apple.com/lookup",
            params={"id": app_id, "country": "fr", "entity": "software"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        available = data.get("resultCount", 0) > 0
        if not available:
            log.info(f"  -> Non dispo store FR : {app_id}")
        return available
    except Exception:
        return True  # En cas d'erreur réseau, on ne bloque pas

# ── Vérification disponibilité France Android ────────────────────────────────
def is_geo_blocked_france(raw_html: str) -> bool:
    """
    Détecte si une fiche Google Play est géo-restreinte pour la France.
    """
    raw_lower = raw_html.lower()
    return any(kw in raw_lower for kw in GEO_BLOCK_KEYWORDS)

# ── iOS ───────────────────────────────────────────────────────────────────────
def fetch_ios_games() -> list[dict]:
    log.info("=== Scraping iOS (iTunes) ===")
    if CHECK_FR:
        log.info("  Mode CHECK_FR actif : vérification store France activée")

    games_by_bundle: dict[str, dict] = {}
    cutoff  = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)
    skipped_geo = 0

    for term in IOS_SEARCH_TERMS:
        try:
            resp = requests.get(
                "https://itunes.apple.com/search",
                params={
                    "term":    term,
                    "country": "fr",        # store France
                    "media":   "software",
                    "entity":  "software",
                    "genreId": "6014",
                    "limit":   200,
                    # lang retire : inclure jeux FR et EN disponibles en France
                },
                timeout=15,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            log.info(f"  '{term}' -> {len(results)} resultats iTunes")

            for item in results:
                # Date
                try:
                    release_dt = datetime.fromisoformat(
                        item.get("releaseDate", "").replace("Z", "")
                    )
                except Exception:
                    continue
                if release_dt < cutoff:
                    continue

                app_id    = str(item.get("trackId", ""))
                bundle_id = item.get("bundleId", "")
                key       = bundle_id or app_id

                # Vérification URL store France
                store_url = item.get("trackViewUrl", "")
                if store_url:
                    country_match = re.search(
                        r'apps\.apple\.com/([a-z]{2})/', store_url
                    )
                    if country_match:
                        country_in_url = country_match.group(1)
                        if country_in_url not in ("fr", ""):
                            skipped_geo += 1
                            continue

                # Déduplication : garder le plus récent
                existing = games_by_bundle.get(key)
                if existing:
                    try:
                        ex_dt = datetime.strptime(existing["releaseDate"], "%Y-%m-%d")
                        if release_dt <= ex_dt:
                            continue
                    except Exception:
                        pass

                # Vérification iTunes Lookup (optionnelle, activée par CHECK_FR)
                if CHECK_FR:
                    if not is_available_france_ios(app_id):
                        skipped_geo += 1
                        continue
                    time.sleep(0.1)

                genre_label = "Games"
                for gid in item.get("genreIds", []):
                    if gid in GENRES and gid != "6014":
                        genre_label = GENRES[gid]
                        break

                artwork = item.get("artworkUrl100", "")
                rating  = item.get("averageUserRating", 0)

                games_by_bundle[key] = {
                    "id":          f"ios_{app_id}",
                    "title":       item.get("trackName", "").strip(),
                    "platform":    ["ios"],
                    "releaseDate": release_dt.strftime("%Y-%m-%d"),
                    "genre":       genre_label,
                    "developer":   item.get("artistName", "").strip(),
                    "icon":        ios_artwork_hd(artwork, 100),
                    "headerImage": ios_artwork_hd(artwork, 1024),
                    "storeUrl":    store_url,
                    "price":       format_price(item.get("price", 0)),
                    "rating":      round(rating, 1) if rating else None,
                    "bundleId":    bundle_id,
                    "status":      "released",
                    "source":      "itunes",
                    "country":     "fr",
                }

            time.sleep(0.4)

        except Exception as e:
            log.error(f"  iOS error '{term}': {e}")

    games = list(games_by_bundle.values())
    log.info(f"iOS total (deduplique) : {len(games)} jeux ({skipped_geo} non dispo FR ignores)")
    return games

# ── Android via bundleId ──────────────────────────────────────────────────────
def scrape_gplay_page(bundle_id: str) -> dict | None:
    """
    Scrape une fiche Google Play depuis le store France (gl=FR).
    Retourne None si introuvable ou géo-restreinte.
    """
    url  = (
        f"https://play.google.com/store/apps/details"
        f"?id={bundle_id}&hl=fr&gl=FR"   # gl=FR = store France
    )
    resp = None

    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS_MOBILE, timeout=20)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                wait = 15 * (attempt + 1)
                log.warning(f"    429 Rate limit — attente {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                log.warning(f"    {resp.status_code} erreur serveur — retry")
                time.sleep(5)
                continue
            resp.raise_for_status()
            break
        except requests.exceptions.Timeout:
            log.warning(f"    Timeout {bundle_id} (tentative {attempt+1})")
            time.sleep(3)
        except Exception as e:
            log.warning(f"    Erreur {bundle_id} (tentative {attempt+1}): {e}")
            time.sleep(3)

    if resp is None or not resp.ok:
        return None

    raw  = resp.text
    soup = BeautifulSoup(raw, "lxml")

    # App introuvable
    if any(kw in raw for kw in ["Nous n'avons pas pu trouver", "not found"]):
        return None

    # Vérification géo-restriction France
    if is_geo_blocked_france(raw):
        log.info(f"    -> Geo-restreint France : {bundle_id}")
        return None

    # Titre
    title = ""
    og = soup.find("meta", property="og:title")
    if og:
        title = og.get("content", "").split(" - ")[0].strip()
    if not title:
        m = re.search(r'"name"\s*:\s*"([^"]{2,100})"', raw)
        if m:
            title = m.group(1).strip()
    if not title:
        return None

    # Statut pre-registration
    status = "upcoming" if any(
        kw in raw.lower() for kw in
        ["pre-register", "preregister", "pre_register",
         "preregistration", "preinscription", "préinscription"]
    ) else "released"

    # Images
    icon = ""
    m = re.search(r'"(https://play-lh\.googleusercontent\.com/[^"]{20,})"', raw)
    if m:
        icon = m.group(1)
    img_urls   = list(dict.fromkeys(
        re.findall(r'https://play-lh\.googleusercontent\.com/[^\s"\'\\]{20,}', raw)
    ))
    header_img = img_urls[1] if len(img_urls) >= 2 else icon

    # Prix
    price = "Free"
    pm = re.search(r'"price"\s*:\s*"([^"]*)"', raw)
    if pm:
        p = pm.group(1).strip()
        price = "Free" if p in ("0", "", "Free", "Gratuit") else p

    # Note
    rating = None
    for pat in (r'"starRating"\s*:\s*"?([\d.]+)"?', r'(\d\.\d)\s*sur\s*5'):
        m = re.search(pat, raw)
        if m:
            try:
                rating = round(float(m.group(1)), 1)
                break
            except Exception:
                pass

    # Développeur
    developer = ""
    for pat in (
        r'"developerName"\s*:\s*"([^"]+)"',
        r'"author"[^}]*"name"\s*:\s*"([^"]+)"'
    ):
        m = re.search(pat, raw)
        if m:
            developer = m.group(1).strip()
            break

    # Genre
    genre = "Games"
    m = re.search(r'"genre"\s*:\s*"([^"]+)"', raw)
    if m:
        genre = m.group(1)

    return {
        "icon":        icon,
        "headerImage": header_img,
        "price":       price,
        "rating":      rating,
        "developer":   developer,
        "genre":       genre,
        "status":      status,
        "bundleId":    bundle_id,
        "source":      "gplay",
        "storeUrl":    f"https://play.google.com/store/apps/details?id={bundle_id}",
        "country":     "fr",
    }

def fetch_android_from_ios(
    ios_games: list[dict],
    existing_android_ids: set[str],
) -> list[dict]:
    log.info("=== Scraping Android (Google Play FR) ===")

    cutoff   = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)
    to_fetch = []

    for g in ios_games:
        bid = g.get("bundleId", "")
        if not bid:
            continue
        android_id = f"android_{bid.replace('.', '_')}"
        if android_id in existing_android_ids:
            log.info(f"  [CACHE] {g['title']}")
            continue
        to_fetch.append(g)

    log.info(
        f"  {len(to_fetch)} jeux a verifier "
        f"({len(ios_games) - len(to_fetch)} en cache)"
    )

    android_games = []
    seen_ids      = set()

    def worker(ios_game):
        bundle_id = ios_game.get("bundleId", "")
        result    = scrape_gplay_page(bundle_id)
        return ios_game, result

    with ThreadPoolExecutor(max_workers=ANDROID_WORKERS) as executor:
        futures = {executor.submit(worker, g): g for g in to_fetch}
        done    = 0
        for future in as_completed(futures):
            done += 1
            ios_game, gplay_data = future.result()
            title     = ios_game.get("title", "")
            bundle_id = ios_game.get("bundleId", "")
            log.info(f"  [{done}/{len(to_fetch)}] {title}")

            if gplay_data is None:
                log.info(f"    -> Pas disponible Android FR")
                continue

            android_id = f"android_{bundle_id.replace('.', '_')}"
            if android_id in seen_ids:
                continue
            seen_ids.add(android_id)

            # ── Date iOS comme référence (plus fiable que Google Play) ──
            android = {
                "id":          android_id,
                "title":       ios_game["title"],
                "platform":    ["android"],
                "releaseDate": ios_game["releaseDate"],
                "genre":       gplay_data.get("genre") or ios_game.get("genre", "Games"),
                "developer":   gplay_data.get("developer") or ios_game.get("developer", ""),
                "icon":        gplay_data.get("icon") or ios_game.get("icon", ""),
                "headerImage": gplay_data.get("headerImage") or ios_game.get("headerImage", ""),
                "storeUrl":    gplay_data["storeUrl"],
                "price":       gplay_data.get("price", "Free"),
                "rating":      gplay_data.get("rating") or ios_game.get("rating"),
                "bundleId":    bundle_id,
                "status":      gplay_data.get("status", ios_game.get("status", "released")),
                "source":      "gplay",
                "country":     "fr",
            }

            # Fallback icon/header depuis iOS si manquant
            if not android["icon"]:
                android["icon"] = ios_game.get("icon", "")
            if not android["headerImage"] or android["headerImage"] == android["icon"]:
                android["headerImage"] = ios_game.get("headerImage", "")

            status_label = "upcoming" if android["status"] == "upcoming" else "released"
            log.info(f"    -> {status_label} FR ({android['releaseDate']}) {android['price']}")
            android_games.append(android)
            time.sleep(0.5)

    log.info(f"Android FR total : {len(android_games)} jeux trouves")
    return android_games

# ── Merge ─────────────────────────────────────────────────────────────────────
def merge_games(existing: list[dict], *new_lists) -> list[dict]:
    all_games: dict[str, dict] = {g["id"]: g for g in existing}

    for game_list in new_lists:
        for game in game_list:
            eid = game["id"]
            ex  = all_games.get(eid, {})

            if ex.get("headerImage") and not game.get("headerImage"):
                game["headerImage"] = ex["headerImage"]
            if ex.get("status") == "released":
                game["status"] = "released"
            if ex.get("rating") and not game.get("rating"):
                game["rating"] = ex["rating"]

            all_games[eid] = game

    # Fusion iOS + Android par titre normalisé
    source_priority = {"itunes": 0, "gplay": 1}
    by_title: dict[str, list] = {}
    for g in all_games.values():
        key = normalize_title(g.get("title", ""))
        by_title.setdefault(key, []).append(g)

    merged_final: dict[str, dict] = {}
    for group in by_title.values():
        group.sort(key=lambda g: source_priority.get(g.get("source", ""), 9))
        primary = group[0]
        for sec in group[1:]:
            for p in sec.get("platform", []):
                if p not in primary["platform"]:
                    primary["platform"].append(p)
            if not primary.get("icon")        and sec.get("icon"):        primary["icon"]        = sec["icon"]
            if not primary.get("headerImage") and sec.get("headerImage"): primary["headerImage"] = sec["headerImage"]
            if not primary.get("developer")   and sec.get("developer"):   primary["developer"]   = sec["developer"]
            if not primary.get("rating")      and sec.get("rating"):      primary["rating"]      = sec["rating"]
            if "android" in sec.get("platform", []) and sec.get("storeUrl"):
                primary["storeUrlAndroid"] = sec["storeUrl"]
            if "ios" in sec.get("platform", []) and sec.get("storeUrl"):
                primary["storeUrlIos"] = sec["storeUrl"]
        merged_final[primary["id"]] = primary

    # Pruning temporel
    cutoff       = datetime.utcnow() - timedelta(days=90)
    future_limit = datetime.utcnow() + timedelta(days=LOOKAHEAD_DAYS)
    pruned = []
    for game in merged_final.values():
        try:
            dt = datetime.strptime(game["releaseDate"], "%Y-%m-%d")
            if cutoff <= dt <= future_limit:
                pruned.append(game)
        except Exception:
            pruned.append(game)

    pruned.sort(key=lambda g: g["releaseDate"])
    log.info(f"Merge final : {len(pruned)} jeux")
    return pruned

# ── Stats ─────────────────────────────────────────────────────────────────────
def print_stats(merged: list[dict]):
    ios_c      = sum(1 for g in merged if "ios"     in g.get("platform", []))
    android_c  = sum(1 for g in merged if "android" in g.get("platform", []))
    both_c     = sum(1 for g in merged if len(g.get("platform", [])) > 1)
    upcoming_c = sum(1 for g in merged if g.get("status") == "upcoming")
    free_c     = sum(1 for g in merged if g.get("price") == "Free")
    header_c   = sum(1 for g in merged if g.get("headerImage"))
    rated_c    = sum(1 for g in merged if g.get("rating"))
    fr_c       = sum(1 for g in merged if g.get("country") == "fr")

    log.info("=" * 40)
    log.info("RESULTAT FINAL")
    log.info(f"  Total          : {len(merged)}")
    log.info(f"  iOS            : {ios_c}")
    log.info(f"  Android        : {android_c}")
    log.info(f"  Multi-platform : {both_c}")
    log.info(f"  Upcoming       : {upcoming_c}")
    log.info(f"  Gratuits       : {free_c}")
    log.info(f"  Avec image     : {header_c}")
    log.info(f"  Avec note      : {rated_c}")
    log.info(f"  Dispo FR       : {fr_c}")
    log.info("=" * 40)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    start = time.time()
    log.info("Mobile Games Release Scraper v3")
    log.info(f"Fenetre : -{LOOKBACK_DAYS}j / +{LOOKAHEAD_DAYS}j")
    log.info(f"Check disponibilite FR : {'OUI' if CHECK_FR else 'NON (passer CHECK_FR=true pour activer)'}")

    existing_data  = load_existing()
    existing_games = existing_data.get("games", [])
    backup_existing()

    # Cache Android
    existing_android_ids = {
        g["id"] for g in existing_games
        if g.get("source") == "gplay" and in_window(g.get("releaseDate", ""))
    }
    log.info(f"Cache Android : {len(existing_android_ids)} entrees")

    ios_games     = fetch_ios_games()
    android_games = fetch_android_from_ios(ios_games, existing_android_ids)

    # Réinjecter le cache Android existant
    cached_android = [
        g for g in existing_games
        if g.get("source") == "gplay" and g["id"] in existing_android_ids
    ]
    log.info(f"Reinjection cache Android : {len(cached_android)} jeux")

    merged = merge_games(existing_games, ios_games, android_games, cached_android)
    print_stats(merged)
    save_data({"games": merged})

    elapsed = time.time() - start
    log.info(f"Termine en {elapsed:.1f}s")

if __name__ == "__main__":
    main()
