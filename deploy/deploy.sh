#!/bin/bash
# Deploy script - run from your Mac to push code to the server
# Usage: bash deploy/deploy.sh <server-ip> <ssh-key-path>

SERVER_IP=${1:?"Uso: deploy.sh <server-ip> <ssh-key-path>"}
SSH_KEY=${2:?"Uso: deploy.sh <server-ip> <ssh-key-path>"}

SSH="ssh -i $SSH_KEY ubuntu@$SERVER_IP"
SCP="scp -i $SSH_KEY"

echo "=== Subiendo código al servidor ==="
rsync -avz --exclude='venv' --exclude='__pycache__' --exclude='.git' \
    --exclude='deals.db-shm' --exclude='deals.db-wal' \
    -e "ssh -i $SSH_KEY" \
    /Users/rnogales/Desktop/deals-scraper/ \
    ubuntu@$SERVER_IP:/home/ubuntu/deals-scraper/

echo "=== Ejecutando setup en el servidor ==="
$SSH "cd /home/ubuntu/deals-scraper && sudo bash deploy/setup-server.sh"

echo "=== Arrancando el servicio ==="
$SSH "sudo systemctl restart deals-scraper"

echo ""
echo "=== Desplegado! ==="
echo "Ver logs: ssh -i $SSH_KEY ubuntu@$SERVER_IP 'journalctl -u deals-scraper -f'"
