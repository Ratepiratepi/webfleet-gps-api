#!/bin/bash
# =============================================================================
# Webfleet GPS API - Installation VPS avec HTTPS
# =============================================================================
set -e

INSTALL_DIR="/opt/webfleet-api"
DOMAIN=""
EMAIL=""

echo "=================================================="
echo "🚛 WEBFLEET GPS API - Installation Production"
echo "=================================================="
echo ""

# =============================================================================
# 1. Vérifications préliminaires
# =============================================================================
if [ "$EUID" -ne 0 ]; then
    echo "⚠️  Ce script doit être exécuté en root (sudo)"
    exit 1
fi

# =============================================================================
# 2. Installation Docker
# =============================================================================
if ! command -v docker &> /dev/null; then
    echo "📦 Installation de Docker..."
    curl -fsSL https://get.docker.com | sh
fi

if ! docker compose version &> /dev/null; then
    echo "📦 Installation de Docker Compose..."
    apt-get update && apt-get install -y docker-compose-plugin
fi

# =============================================================================
# 3. Configuration
# =============================================================================
echo ""
echo "🔧 CONFIGURATION"
echo "----------------"

# Domaine
read -p "Nom de domaine (ex: webfleet.monsite.com): " DOMAIN
if [ -z "$DOMAIN" ]; then
    echo "❌ Domaine requis!"
    exit 1
fi

# Email pour Let's Encrypt
read -p "Email pour Let's Encrypt: " EMAIL
if [ -z "$EMAIL" ]; then
    echo "❌ Email requis!"
    exit 1
fi

# Créer le dossier
mkdir -p $INSTALL_DIR
cd $INSTALL_DIR

# Credentials Webfleet
if [ ! -f .env ]; then
    echo ""
    echo "🔐 Credentials Webfleet:"
    read -p "WEBFLEET_USERNAME: " WF_USER
    read -sp "WEBFLEET_PASSWORD: " WF_PASS
    echo ""
    read -p "WEBFLEET_ACCOUNT [benwah]: " WF_ACCOUNT
    WF_ACCOUNT=${WF_ACCOUNT:-benwah}

    # Générer API Key
    API_KEY=$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)

    cat > .env << EOF
WEBFLEET_USERNAME=$WF_USER
WEBFLEET_PASSWORD=$WF_PASS
WEBFLEET_ACCOUNT=$WF_ACCOUNT
API_KEY=$API_KEY
CACHE_DURATION=60
DOMAIN=$DOMAIN
EMAIL=$EMAIL
EOF
    chmod 600 .env
    echo "✅ Fichier .env créé"
else
    source .env
fi

# =============================================================================
# 4. Création des fichiers de configuration
# =============================================================================
echo ""
echo "📁 Création des fichiers..."

mkdir -p nginx certbot/conf certbot/www data

# Nginx config initiale (HTTP only pour obtenir le certificat)
cat > nginx/nginx.conf << 'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name DOMAIN_PLACEHOLDER;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 200 'Webfleet API - En attente du certificat SSL';
        add_header Content-Type text/plain;
    }
}
NGINX
sed -i "s/DOMAIN_PLACEHOLDER/$DOMAIN/g" nginx/nginx.conf

# Dockerfile
cat > Dockerfile << 'DOCKERFILE'
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget gnupg libglib2.0-0 libnss3 libnspr4 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libdbus-1-3 libxcb1 \
    libxkbcommon0 libx11-6 libxcomposite1 libxdamage1 libxext6 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir playwright && playwright install chromium

WORKDIR /app
RUN mkdir -p /app/data
COPY webfleet_server.py /app/

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8080/health || exit 1

CMD ["python", "-u", "webfleet_server.py"]
DOCKERFILE

# Docker Compose
cat > docker-compose.yml << 'COMPOSE'
version: '3.8'

services:
  webfleet-api:
    build: .
    container_name: webfleet-gps
    restart: always
    expose:
      - "8080"
    environment:
      - WEBFLEET_USERNAME=${WEBFLEET_USERNAME}
      - WEBFLEET_PASSWORD=${WEBFLEET_PASSWORD}
      - WEBFLEET_ACCOUNT=${WEBFLEET_ACCOUNT}
      - API_PORT=8080
      - API_KEY=${API_KEY}
      - CACHE_DURATION=${CACHE_DURATION:-60}
    volumes:
      - ./data:/app/data
    networks:
      - webfleet-network
    deploy:
      resources:
        limits:
          memory: 1G

  nginx:
    image: nginx:alpine
    container_name: webfleet-nginx
    restart: always
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./certbot/conf:/etc/letsencrypt:ro
      - ./certbot/www:/var/www/certbot:ro
    depends_on:
      - webfleet-api
    networks:
      - webfleet-network
    command: "/bin/sh -c 'while :; do sleep 6h & wait $${!}; nginx -s reload; done & nginx -g \"daemon off;\"'"

  certbot:
    image: certbot/certbot
    container_name: webfleet-certbot
    volumes:
      - ./certbot/conf:/etc/letsencrypt
      - ./certbot/www:/var/www/certbot
    entrypoint: "/bin/sh -c 'trap exit TERM; while :; do certbot renew; sleep 12h & wait $${!}; done;'"

networks:
  webfleet-network:
    driver: bridge
COMPOSE

# =============================================================================
# 5. Obtention du certificat SSL
# =============================================================================
echo ""
echo "🔒 Obtention du certificat SSL..."

# Démarrer nginx temporairement
docker compose up -d nginx

# Attendre que nginx soit prêt
sleep 5

# Obtenir le certificat
docker compose run --rm certbot certonly \
    --webroot \
    --webroot-path=/var/www/certbot \
    --email $EMAIL \
    --agree-tos \
    --no-eff-email \
    -d $DOMAIN

# Arrêter nginx
docker compose down

# =============================================================================
# 6. Configuration Nginx finale avec HTTPS
# =============================================================================
echo ""
echo "📝 Configuration HTTPS..."

cat > nginx/nginx.conf << NGINX
# Rate limiting: 1 requête/seconde par IP (burst de 5 pour les pics)
limit_req_zone \$binary_remote_addr zone=api_limit:10m rate=1r/s;

upstream webfleet_api {
    server webfleet-api:8080;
    keepalive 32;
}

# HTTP -> HTTPS redirect
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

# HTTPS
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name $DOMAIN;

    # SSL
    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:50m;

    # Security headers
    add_header Strict-Transport-Security "max-age=63072000" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;

    # Health (no auth, no rate limit)
    location /health {
        proxy_pass http://webfleet_api;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }

    # API
    location / {
        limit_req zone=api_limit burst=5 nodelay;

        proxy_pass http://webfleet_api;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Connection "";

        proxy_connect_timeout 30s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
    }
}
NGINX

# =============================================================================
# 7. Démarrage final
# =============================================================================
echo ""
echo "🚀 Démarrage des services..."

docker compose up -d --build

# Attendre que tout soit prêt
echo "⏳ Attente du démarrage (60s)..."
sleep 60

# Test
echo ""
echo "🧪 Test de l'API..."
curl -s https://$DOMAIN/health | head -c 200

echo ""
echo ""
echo "=================================================="
echo "✅ INSTALLATION TERMINÉE!"
echo "=================================================="
echo ""
echo "🌐 URL: https://$DOMAIN"
echo "🔑 API Key: $API_KEY"
echo ""
echo "📋 Endpoints:"
echo "   GET /health              - Health check (sans auth)"
echo "   GET /positions           - Toutes les positions"
echo "   GET /positions/moving    - Véhicules en mouvement"
echo "   GET /positions/stopped   - Véhicules à l'arrêt"
echo ""
echo "🔧 Pour n8n:"
echo "   URL: https://$DOMAIN/positions"
echo "   Header: Authorization: Bearer $API_KEY"
echo ""
echo "📊 Commandes utiles:"
echo "   cd $INSTALL_DIR"
echo "   docker compose logs -f          # Logs"
echo "   docker compose restart          # Redémarrer"
echo "   docker compose down && docker compose up -d  # Reset"
echo ""
