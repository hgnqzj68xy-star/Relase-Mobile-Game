#!/usr/bin/env python3
"""
Mobile Games Release Scraper v4
- iOS     : iTunes Search API (FR + EN, store France)
- Android : corrélation bundleId iOS -> Google Play FR
- Statut  : upcoming si date future, released si date passée ou aujourd'hui
- Purge   : déduplication stricte, max 300 jeux en base
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
LOOKBACK_DAYS    = 30    # jours passés
LOOKAHEAD_DAYS   = 30    # jours futurs
MAX_GAMES        = 300   # seuil alerte
ANDROID_WORKERS  = 5
CHECK_FR         = os.environ.get("CHECK_FR", "false").lower() == "true"

IOS_SEARCH_TERMS = [
    # Anglais — majorité des jeux mobiles
    "new game", "rpg", "action game", "puzzle", "strategy",
    "adventure", "simulation", "card game", "casual game", "platformer",
    "new ios game", "mobile rpg", "mobile action", "new release game",
    "new mobile game 2026", "open world mobile", "battle royale mobile",
    "tower defense", "idle game", "gacha game",
    "new release 2026", "just released game", "latest game release",
    # Français — jeux localisés
    "nouveau jeu", "jeu de role", "jeu de strategie", "jeu de cartes",
    "jeu de puzzle", "simulation mobile",
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

GEO_BLOCK_KEYWORDS = [
    "not available in your country",
    "pas disponible dans votre pays",
    "cette application n'est pas compatible",
    "isn't available in your country",
    "not available in france",
]

# ── Helpers ───────────────────────────────────────────────────────────────────
def load_existing():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                data = json.load(f)
            log.info(f"JSON charge : {len(data.get('games', []))} jeux")
            return data
        except json.JSONDecodeError as e:
            log.error(f"JSON corrompu : {e}")
    return {"lastUpdated": "", "games": []}

def backup_existing():
    if DATA_FILE.exists():
        shutil.copy2(DATA_FILE, BACKUP_FILE)
        log.info(f"Backup cree")

def save_data(data):
    games = data.get("games", [])
    required = {"id", "title", "platform", "releaseDate"}
    games = [g for g in games if required.issubset(g.keys())]

    data["games"]       = games
    data["lastUpdated"] = datetime.utcnow().isoformat() + "Z"
    data["totalGames"]  = len(games)

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(DATA_FILE)
    log.info(f"Sauvegarde : {len(games)} jeux")

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

    try:
        ts = int(raw_str)
        if ts > 1_000_000_000:
            return datetime.utcfromtimestamp(ts)
    except (ValueError, TypeError):
        pass

    mois_fr = {
        "janvier":1, "fevrier":2, "mars":3, "avril":4,
        "mai":5, "juin":6, "juillet":7, "aout":8,
        "septembre":9, "octobre":10, "novembre":11, "decembre":12,
    }
    raw_lower = raw_str.lower()
    m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', raw_lower)
    if m:
        day_s, month_s, year_s = m.group(1), m.group(2), m.group(3)
        month_s = month_s.replace('é','e').replace('û','u').replace('è','e')
        if month_s in mois_fr:
            try:
                return datetime(int(year_s), mois_fr[month_s], int(day_s))
            except Exception:
                pass

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

    q = re.match(r'Q([1-4])\s+(\d{4})', raw_str.strip())
    if q:
        return datetime(int(q.group(2)), (int(q.group(1)) - 1) * 3 + 1, 1)

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

def compute_status(release_dt: datetime) -> str:
    """
    Calcule le statut en fonction de la date :
    - upcoming : date strictement future (après aujourd'hui minuit UTC)
    - released : date = aujourd'hui ou passée
    """
    today_midnight = datetime.utcnow().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if release_dt > today_midnight:
        return "upcoming"
    return "released"

def in_window(date_str: str) -> bool:
    try:
        dt  = datetime.strptime(date_str, "%Y-%m-%d")
        now = datetime.utcnow()
        return (now - timedelta(days=LOOKBACK_DAYS)) <= dt <= (now + timedelta(days=LOOKAHEAD_DAYS))
    except Exception:
        return False

# ── Vérification dispo France iOS ─────────────────────────────────────────────
def is_available_france_ios(app_id: str) -> bool:
    try:
        resp = requests.get(
            "https://itunes.apple.com/lookup",
            params={"id": app_id, "country": "fr", "entity": "software"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("resultCount", 0) > 0
    except Exception:
        return True

# ── iOS ───────────────────────────────────────────────────────────────────────
def fetch_ios_games() -> list[dict]:
    log.info("=== Scraping iOS (iTunes FR + EN) ===")

    games_by_bundle: dict[str, dict] = {}
    now             = datetime.utcnow()
    today_midnight  = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff          = today_midnight - timedelta(days=LOOKBACK_DAYS)
    future_limit    = today_midnight + timedelta(days=LOOKAHEAD_DAYS)
    skipped_geo     = 0

    for term in IOS_SEARCH_TERMS:
        try:
            resp = requests.get(
                "https://itunes.apple.com/search",
                params={
                    "term":    term,
                    "country": "fr",       # store France
                    "media":   "software",
                    "entity":  "software",
                    "genreId": "6014",
                    "limit":   200,
                    # Pas de filtre lang : inclut FR et EN dispo en France
                },
                timeout=15,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            log.info(f"  '{term}' -> {len(results)} résultats")

            for item in results:
                # Date de sortie
                try:
                    release_dt = datetime.fromisoformat(
                        item.get("releaseDate", "").replace("Z", "")
                    )
                except Exception:
                    continue

                # Fenêtre temporelle : passé LOOKBACK + futur LOOKAHEAD
                if release_dt < cutoff or release_dt > future_limit:
                    continue

                app_id    = str(item.get("trackId", ""))
                bundle_id = item.get("bundleId", "")
                key       = bundle_id or app_id

                # Vérification URL store France
                store_url = item.get("trackViewUrl", "")
                if store_url:
                    cm = re.search(r'apps\.apple\.com/([a-z]{2})/', store_url)
                    if cm and cm.group(1) not in ("fr", ""):
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

                # Vérification lookup FR (optionnelle)
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

                # ── Statut basé sur la date réelle ──
                status = compute_status(release_dt)

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
                    "storeUrlIos": store_url,
                    "price":       format_price(item.get("price", 0)),
                    "rating":      round(rating, 1) if rating else None,
                    "bundleId":    bundle_id,
                    "status":      status,
                    "source":      "itunes",
                    "country":     "fr",
                }

            time.sleep(0.4)

        except Exception as e:
            log.error(f"  iOS error '{term}': {e}")

    games = list(games_by_bundle.values())
    upcoming_count = sum(1 for g in games if g["status"] == "upcoming")
    released_count = sum(1 for g in games if g["status"] == "released")
    log.info(f"iOS total : {len(games)} jeux "
             f"({released_count} released / {upcoming_count} upcoming / "
             f"{skipped_geo} non-FR ignorés)")
    return games

# ── Android via bundleId ──────────────────────────────────────────────────────
def scrape_gplay_page(bundle_id: str, ios_status: str) -> dict | None:
    url  = (
        f"https://play.google.com/store/apps/details"
        f"?id={bundle_id}&hl=fr&gl=FR"
    )
    resp = None

    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS_MOBILE, timeout=20)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                wait = 15 * (attempt + 1)
                log.warning(f"    429 — attente {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                time.sleep(5)
                continue
            resp.raise_for_status()
            break
        except requests.exceptions.Timeout:
            log.warning(f"    Timeout (tentative {attempt+1})")
            time.sleep(3)
        except Exception as e:
            log.warning(f"    Erreur (tentative {attempt+1}): {e}")
            time.sleep(3)

    if resp is None or not resp.ok:
        return None

    raw  = resp.text
    soup = BeautifulSoup(raw, "lxml")

    if any(kw in raw for kw in ["Nous n'avons pas pu trouver", "not found"]):
        return None

    # Géo-restriction France
    if any(kw in raw.lower() for kw in GEO_BLOCK_KEYWORDS):
        log.info(f"    -> Geo-restreint France")
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

    # Statut Google Play : pre-register = upcoming, sinon hériter iOS
    gplay_upcoming = any(kw in raw.lower() for kw in [
        "pre-register", "preregister", "pre_register",
        "preregistration", "préinscription",
    ])
    # On utilise le statut iOS comme référence principale
    # Google Play peut confirmer "upcoming" via pre-register
    status = "upcoming" if (gplay_upcoming or ios_status == "upcoming") else "released"

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
        r'"author"[^}]*"name"\s*:\s*"([^"]+)"',
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
        "storeUrlAndroid": f"https://play.google.com/store/apps/details?id={bundle_id}",
        "country":     "fr",
    }

def fetch_android_from_ios(
    ios_games: list[dict],
    existing_android_ids: set[str],
) -> list[dict]:
    log.info("=== Scraping Android (Google Play FR) ===")

    now            = datetime.utcnow()
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff         = today_midnight - timedelta(days=LOOKBACK_DAYS)
    to_fetch       = []

    for g in ios_games:
        bid = g.get("bundleId", "")
        if not bid:
            continue
        android_id = f"android_{bid.replace('.', '_')}"
        if android_id in existing_android_ids:
            log.info(f"  [CACHE] {g['title']}")
            continue
        to_fetch.append(g)

    log.info(f"  {len(to_fetch)} à vérifier ({len(ios_games) - len(to_fetch)} en cache)")

    android_games = []
    seen_ids      = set()

    def worker(ios_game):
        bundle_id  = ios_game.get("bundleId", "")
        ios_status = ios_game.get("status", "released")
        result     = scrape_gplay_page(bundle_id, ios_status)
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

            # Construire l'entrée Android
            # Date = date iOS (référence fiable)
            # Statut = compute_status sur la date iOS
            ios_release = ios_game.get("releaseDate", "")
            try:
                ios_dt = datetime.strptime(ios_release, "%Y-%m-%d")
                status = compute_status(ios_dt)
            except Exception:
                status = gplay_data.get("status", "released")

            android = {
                "id":            android_id,
                "title":         title,
                "platform":      ["android"],
                "releaseDate":   ios_release,
                "genre":         gplay_data.get("genre") or ios_game.get("genre", "Games"),
                "developer":     gplay_data.get("developer") or ios_game.get("developer", ""),
                "icon":          gplay_data.get("icon") or ios_game.get("icon", ""),
                "headerImage":   gplay_data.get("headerImage") or ios_game.get("headerImage", ""),
                "storeUrl":      gplay_data["storeUrlAndroid"],
                "storeUrlAndroid": gplay_data["storeUrlAndroid"],
                "price":         gplay_data.get("price", "Free"),
                "rating":        gplay_data.get("rating") or ios_game.get("rating"),
                "bundleId":      bundle_id,
                "status":        status,
                "source":        "gplay",
                "country":       "fr",
            }

            if not android["icon"]:
                android["icon"] = ios_game.get("icon", "")
            if not android["headerImage"] or android["headerImage"] == android["icon"]:
                android["headerImage"] = ios_game.get("headerImage", "")

            log.info(f"    -> {status} ({ios_release}) {android['price']}")
            android_games.append(android)
            time.sleep(0.5)

    log.info(f"Android FR total : {len(android_games)} jeux")
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
            if ex.get("rating") and not game.get("rating"):
                game["rating"] = ex["rating"]
            # Ne jamais remettre released -> upcoming
            if ex.get("status") == "released" and game.get("status") == "upcoming":
                game["status"] = "released"

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
            if not primary.get("icon")           and sec.get("icon"):           primary["icon"]           = sec["icon"]
            if not primary.get("headerImage")    and sec.get("headerImage"):    primary["headerImage"]    = sec["headerImage"]
            if not primary.get("developer")      and sec.get("developer"):      primary["developer"]      = sec["developer"]
            if not primary.get("rating")         and sec.get("rating"):         primary["rating"]         = sec["rating"]
            if not primary.get("storeUrlAndroid") and sec.get("storeUrlAndroid"): primary["storeUrlAndroid"] = sec["storeUrlAndroid"]
            if not primary.get("storeUrlIos")    and sec.get("storeUrlIos"):    primary["storeUrlIos"]    = sec["storeUrlIos"]
        merged_final[primary["id"]] = primary

    # Pruning temporel
    now          = datetime.utcnow()
    cutoff       = now - timedelta(days=90)
    future_limit = now + timedelta(days=LOOKAHEAD_DAYS)
    pruned       = []

    for game in merged_final.values():
        try:
            dt = datetime.strptime(game["releaseDate"], "%Y-%m-%d")
            if cutoff <= dt <= future_limit:
                pruned.append(game)
        except Exception:
            pruned.append(game)

    pruned.sort(key=lambda g: g["releaseDate"])
    log.info(f"Merge final : {len(pruned)} jeux")

    if len(pruned) > MAX_GAMES:
        log.warning(f"ALERTE : {len(pruned)} jeux > seuil {MAX_GAMES}")

    return pruned

# ── Stats ─────────────────────────────────────────────────────────────────────
def print_stats(merged: list[dict]):
    ios_c      = sum(1 for g in merged if "ios"     in g.get("platform", []))
    android_c  = sum(1 for g in merged if "android" in g.get("platform", []))
    both_c     = sum(1 for g in merged if len(g.get("platform", [])) > 1)
    upcoming_c = sum(1 for g in merged if g.get("status") == "upcoming")
    released_c = sum(1 for g in merged if g.get("status") == "released")
    free_c     = sum(1 for g in merged if g.get("price") == "Free")

    log.info("=" * 40)
    log.info("RÉSULTAT FINAL")
    log.info(f"  Total          : {len(merged)}")
    log.info(f"  iOS            : {ios_c}")
    log.info(f"  Android        : {android_c}")
    log.info(f"  Multi-platform : {both_c}")
    log.info(f"  Released       : {released_c}")
    log.info(f"  Upcoming       : {upcoming_c}")
    log.info(f"  Gratuits       : {free_c}")
    log.info("=" * 40)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    start = time.time()
    log.info("Mobile Games Release Scraper v4")
    log.info(f"Fenetre : -{LOOKBACK_DAYS}j / +{LOOKAHEAD_DAYS}j")

    existing_data  = load_existing()
    existing_games = existing_data.get("games", [])
    backup_existing()

    existing_android_ids = {
        g["id"] for g in existing_games
        if g.get("source") == "gplay" and in_window(g.get("releaseDate", ""))
    }
    log.info(f"Cache Android : {len(existing_android_ids)} entrées")

    ios_games     = fetch_ios_games()
    android_games = fetch_android_from_ios(ios_games, existing_android_ids)

    cached_android = [
        g for g in existing_games
        if g.get("source") == "gplay" and g["id"] in existing_android_ids
    ]
    log.info(f"Réinjection cache : {len(cached_android)} jeux Android")

    merged = merge_games(existing_games, ios_games, android_games, cached_android)
    print_stats(merged)
    save_data({"games": merged})

    elapsed = time.time() - start
    log.info(f"Terminé en {elapsed:.1f}s")

if __name__ == "__main__":
    main()
