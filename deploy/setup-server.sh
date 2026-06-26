#!/bin/bash
# Setup script for Oracle Cloud ARM instance (Ubuntu 22.04+)
# Run as root: sudo bash setup-server.sh

set -e

echo "=== Actualizando sistema ==="
apt-get update && apt-get upgrade -y

echo "=== Instalando dependencias ==="
apt-get install -y \
    python3 python3-pip python3-venv \
    git \
    chromium-browser \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libxshmfence1 libx11-xcb1

echo "=== Creando usuario scraper ==="
useradd -m -s /bin/bash scraper || true

echo "=== Clonando repositorio ==="
cd /home/scraper
if [ -d deals-scraper ]; then
    cd deals-scraper && git pull
else
    # Se copiará manualmente con scp
    mkdir -p deals-scraper
fi

echo "=== Instalando dependencias Python ==="
cd /home/scraper/deals-scraper
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
playwright install chrome

echo "=== Configurando systemd ==="
cp deploy/deals-scraper.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable deals-scraper

echo "=== Abriendo puertos del firewall ==="
# Dashboard (opcional)
iptables -I INPUT -p tcp --dport 8080 -j ACCEPT

chown -R scraper:scraper /home/scraper/deals-scraper

echo ""
echo "=== Setup completo ==="
echo "Copia tu config.yaml y deals.db al servidor."
echo "Luego ejecuta: sudo systemctl start deals-scraper"
echo "Ver logs: journalctl -u deals-scraper -f"
