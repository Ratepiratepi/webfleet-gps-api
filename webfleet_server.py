#!/usr/bin/env python3
"""
Webfleet GPS API Server - Version Production VPS
Serveur HTTP sécurisé avec authentification API Key
"""

import asyncio
import json
import os
import sys
import threading
import logging
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import hashlib
import secrets

# Configuration logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/app/data/webfleet.log') if os.path.exists('/app/data') else logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

try:
    from playwright.async_api import async_playwright
except ImportError:
    logger.error("Playwright non installé")
    sys.exit(1)

# =============================================================================
# CONFIGURATION (via variables d'environnement)
# =============================================================================
API_PORT = int(os.environ.get("API_PORT", "8080"))
API_KEY = os.environ.get("API_KEY", "")  # Clé d'authentification
CACHE_DURATION = int(os.environ.get("CACHE_DURATION", "60"))
WEBFLEET_USERNAME = os.environ.get("WEBFLEET_USERNAME", "")
WEBFLEET_PASSWORD = os.environ.get("WEBFLEET_PASSWORD", "")
WEBFLEET_ACCOUNT = os.environ.get("WEBFLEET_ACCOUNT", "")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# CACHE
# =============================================================================
class DataCache:
    def __init__(self):
        self.positions = []
        self.last_update = None
        self.lock = threading.Lock()
        self.error = None
        self.login_count = 0
        self.refresh_count = 0

    def update(self, positions):
        with self.lock:
            self.positions = positions
            self.last_update = datetime.now()
            self.error = None
            self.refresh_count += 1

    def set_error(self, error):
        with self.lock:
            self.error = str(error)
            logger.error(f"Cache error: {error}")

    def get(self):
        with self.lock:
            age = None
            if self.last_update:
                age = (datetime.now() - self.last_update).total_seconds()

            return {
                "positions": self.positions,
                "count": len(self.positions),
                "last_update": self.last_update.isoformat() if self.last_update else None,
                "cache_age_seconds": round(age, 1) if age else None,
                "error": self.error
            }

    def stats(self):
        with self.lock:
            return {
                "login_count": self.login_count,
                "refresh_count": self.refresh_count,
                "vehicle_count": len(self.positions),
                "last_update": self.last_update.isoformat() if self.last_update else None,
                "error": self.error
            }


cache = DataCache()


# =============================================================================
# SCRAPER WEBFLEET
# =============================================================================
class WebfleetScraper:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None
        self.intercepted_data = {"objects": None, "telemetry": None}

    async def start(self):
        logger.info("Démarrage du navigateur headless...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage']  # Pour Docker
        )
        context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="fr-FR"
        )
        self.page = await context.new_page()
        self.page.on("response", self._intercept_response)
        logger.info("Navigateur prêt")

    async def _intercept_response(self, response):
        url = response.url
        try:
            if "/api/objects" in url and "?" not in url:
                self.intercepted_data["objects"] = await response.json()
                logger.debug(f"Intercepté: objects ({len(self.intercepted_data['objects'])} items)")
            elif "/api/latestTelemetry/objects" in url:
                self.intercepted_data["telemetry"] = await response.json()
                logger.debug(f"Intercepté: telemetry ({len(self.intercepted_data['telemetry'])} items)")
        except Exception as e:
            logger.warning(f"Erreur interception: {e}")

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def login(self):
        logger.info("Connexion à Webfleet...")
        cache.login_count += 1

        try:
            await self.page.goto(
                "https://live-wf.webfleet.com/web/map",
                wait_until="networkidle",
                timeout=60000
            )

            # Vérifier si login requis (nouveau portail login.webfleet.com)
            if "login.webfleet.com" in self.page.url or "login" in self.page.url.lower():
                logger.info("Page de login Keycloak détectée, authentification...")
                await self.page.wait_for_load_state("networkidle")
                await asyncio.sleep(3)  # Attendre le chargement complet

                # Nouveau formulaire Keycloak avec sélecteurs spécifiques
                # Utiliser les attributs name ou id pour cibler précisément chaque champ

                # Champ Account (premier champ texte - "Nombre de cuenta de Webfleet")
                account_field = await self.page.query_selector('input[name="account"], input[id="account"], input[autocomplete="username"]:first-of-type')
                if not account_field:
                    # Fallback: premier input text
                    account_field = await self.page.query_selector('input[type="text"]:first-of-type')

                if account_field and WEBFLEET_ACCOUNT:
                    await account_field.click()
                    await account_field.fill("")  # Clear first
                    await account_field.fill(WEBFLEET_ACCOUNT)
                    logger.info(f"Account rempli: {WEBFLEET_ACCOUNT}")
                    await asyncio.sleep(0.5)

                # Champ Username (deuxième champ texte - "Nombre de usuario")
                username_field = await self.page.query_selector('input[name="username"], input[id="username"]')
                if not username_field:
                    # Fallback: tous les inputs text, prendre le deuxième
                    text_inputs = await self.page.query_selector_all('input[type="text"]')
                    if len(text_inputs) >= 2:
                        username_field = text_inputs[1]

                if username_field:
                    await username_field.click()
                    await username_field.fill("")  # Clear first
                    await username_field.fill(WEBFLEET_USERNAME)
                    logger.info(f"Username rempli: {WEBFLEET_USERNAME}")
                    await asyncio.sleep(0.5)

                # Champ Password
                pwd_field = await self.page.query_selector('input[type="password"], input[name="password"], input[id="password"]')
                if pwd_field:
                    await pwd_field.click()
                    await pwd_field.fill("")  # Clear first
                    await pwd_field.fill(WEBFLEET_PASSWORD)
                    logger.info("Password rempli")
                    await asyncio.sleep(0.5)

                # Submit - chercher le bouton
                btn = await self.page.query_selector('button[type="submit"], input[type="submit"], button:has-text("Iniciar"), button:has-text("Login"), button:has-text("Sign")')
                if btn:
                    await btn.click()
                    logger.info("Bouton submit cliqué")

                # Attendre redirection vers l'app
                await self.page.wait_for_url("**/web/**", timeout=60000)
                logger.info("✅ Authentification réussie!")

            await self.page.wait_for_load_state("networkidle")
            await asyncio.sleep(3)
            return True

        except Exception as e:
            logger.error(f"❌ Erreur de connexion: {e}")
            cache.set_error(f"Login failed: {e}")
            return False

    async def refresh(self):
        logger.debug("Refresh de la page...")
        self.intercepted_data = {"objects": None, "telemetry": None}
        try:
            await self.page.reload(wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)
            return True
        except Exception as e:
            logger.error(f"Erreur refresh: {e}")
            return False

    def get_positions(self):
        objects = self.intercepted_data.get("objects", []) or []
        telemetry = self.intercepted_data.get("telemetry", []) or []

        if not objects:
            return []

        telem_map = {t["objectId"]: t for t in telemetry}
        positions = []

        for obj in objects:
            telem = telem_map.get(obj["objectId"], {})
            pos = telem.get("position", obj.get("position", {})) or {}

            positions.append({
                "object_id": obj.get("objectId"),
                "number": obj.get("number", ""),
                "name": obj.get("name", ""),
                "license_plate": obj.get("licensePlate", "").strip(),
                "type": obj.get("type", ""),
                "latitude": pos.get("latitude") if isinstance(pos, dict) else None,
                "longitude": pos.get("longitude") if isinstance(pos, dict) else None,
                "address": (pos.get("location", {}).get("address", "") if isinstance(pos, dict)
                           else obj.get("locationDescription", {}).get("address", "")),
                "speed": telem.get("speed", 0),
                "ignition": telem.get("ignition", "UNKNOWN"),
                "stand_still": telem.get("standStill", False),
                "last_gps_time": (pos.get("time") if isinstance(pos, dict) else None) or obj.get("lastGpsTime", ""),
                "odometer_km": obj.get("odometer", 0) / 100
            })

        return positions


async def background_scraper():
    """Boucle principale du scraper"""
    scraper = WebfleetScraper()
    retry_delay = 30

    while True:
        try:
            await scraper.start()

            if not await scraper.login():
                logger.error(f"Login échoué, retry dans {retry_delay}s...")
                await scraper.close()
                await asyncio.sleep(retry_delay)
                continue

            # Boucle de refresh
            while True:
                try:
                    positions = scraper.get_positions()

                    if positions:
                        cache.update(positions)
                        logger.info(f"✅ {len(positions)} positions mises à jour")

                        # Sauvegarder en fichier
                        with open(DATA_DIR / "positions_latest.json", "w", encoding="utf-8") as f:
                            json.dump(cache.get(), f, indent=2, ensure_ascii=False)
                    else:
                        logger.warning("Pas de données, reconnexion...")
                        if not await scraper.login():
                            break

                    await asyncio.sleep(CACHE_DURATION)

                    if not await scraper.refresh():
                        logger.warning("Refresh échoué, reconnexion...")
                        if not await scraper.login():
                            break

                except Exception as e:
                    logger.error(f"Erreur dans la boucle: {e}")
                    cache.set_error(e)
                    break

        except Exception as e:
            logger.error(f"Erreur fatale scraper: {e}")
            cache.set_error(e)

        finally:
            await scraper.close()

        logger.info(f"Redémarrage dans {retry_delay}s...")
        await asyncio.sleep(retry_delay)


def run_scraper_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(background_scraper())


# =============================================================================
# SERVEUR HTTP
# =============================================================================
class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logger.info(f"HTTP {args[0]}")

    def check_auth(self):
        """Vérifie l'API Key si configurée"""
        if not API_KEY:
            return True

        # Vérifier header Authorization
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            if secrets.compare_digest(token, API_KEY):
                return True

        # Vérifier query param
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        key = params.get("api_key", [None])[0]
        if key and secrets.compare_digest(key, API_KEY):
            return True

        return False

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        """CORS preflight"""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def do_HEAD(self):
        """Handle HEAD requests (used by Docker healthcheck)"""
        if self.path == "/health":
            data = cache.stats()
            status = 200 if data["last_update"] and not data["error"] else 503
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        # Health check sans auth
        if self.path == "/health":
            data = cache.stats()
            status = "healthy" if data["last_update"] and not data["error"] else "unhealthy"
            self.send_json({"status": status, **data})
            return

        # Vérifier auth pour les autres endpoints
        if not self.check_auth():
            self.send_json({"error": "Unauthorized. Use 'Authorization: Bearer <API_KEY>' header"}, 401)
            return

        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/positions" or path == "/":
            self.send_json(cache.get())

        elif path == "/positions/vehicle":
            plate = params.get("plate", [None])[0]
            number = params.get("number", [None])[0]
            data = cache.get()

            if plate:
                data["positions"] = [p for p in data["positions"]
                                    if plate.upper() in p["license_plate"].upper()]
            elif number:
                data["positions"] = [p for p in data["positions"]
                                    if p["number"] == number]

            data["count"] = len(data["positions"])
            self.send_json(data)

        elif path == "/positions/moving":
            data = cache.get()
            data["positions"] = [p for p in data["positions"]
                                if not p["stand_still"] or p["speed"] > 0]
            data["count"] = len(data["positions"])
            self.send_json(data)

        elif path == "/positions/stopped":
            data = cache.get()
            data["positions"] = [p for p in data["positions"]
                                if p["stand_still"] and p["speed"] == 0]
            data["count"] = len(data["positions"])
            self.send_json(data)

        elif path == "/stats":
            self.send_json(cache.stats())

        else:
            self.send_json({
                "error": "Not found",
                "endpoints": [
                    "GET /health - Health check (no auth)",
                    "GET /positions - All positions",
                    "GET /positions/vehicle?plate=XX - Filter by plate",
                    "GET /positions/vehicle?number=001 - Filter by number",
                    "GET /positions/moving - Moving vehicles only",
                    "GET /positions/stopped - Stopped vehicles only",
                    "GET /stats - Service statistics"
                ]
            }, 404)


# =============================================================================
# MAIN
# =============================================================================
def main():
    # Vérifier credentials
    if not WEBFLEET_USERNAME or not WEBFLEET_PASSWORD:
        logger.error("❌ WEBFLEET_USERNAME et WEBFLEET_PASSWORD requis!")
        sys.exit(1)

    # Générer API key si non définie
    global API_KEY
    if not API_KEY:
        API_KEY = secrets.token_urlsafe(32)
        logger.warning(f"⚠️  API_KEY générée automatiquement: {API_KEY}")
        logger.warning("   Définissez API_KEY en variable d'environnement pour la fixer")

    logger.info("=" * 60)
    logger.info("🚛 WEBFLEET GPS API SERVER - Production")
    logger.info("=" * 60)
    logger.info(f"📡 Port: {API_PORT}")
    logger.info(f"⏱️  Refresh: {CACHE_DURATION}s")
    logger.info(f"🔑 API Key: {API_KEY[:8]}...")
    logger.info("=" * 60)

    # Lancer scraper en background
    scraper_thread = threading.Thread(target=run_scraper_thread, daemon=True)
    scraper_thread.start()

    # Lancer serveur HTTP
    server = HTTPServer(("0.0.0.0", API_PORT), APIHandler)
    logger.info(f"🌐 Serveur démarré sur 0.0.0.0:{API_PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Arrêt...")
        server.shutdown()


if __name__ == "__main__":
    main()
