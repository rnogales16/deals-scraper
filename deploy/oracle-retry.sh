#!/bin/bash
# Auto-retry script para crear instancia ARM en Oracle Cloud Free Tier
# Reintenta cada 60 segundos hasta conseguir capacidad
# Uso: bash deploy/oracle-retry.sh

set -uo pipefail

# --- Configuración ---
COMPARTMENT="ocid1.tenancy.oc1..aaaaaaaan7y3o5fflj6oxgfpxjqbh56necartzg4n32plv3agxg7lkclcbcq"
AD="nCja:EU-MADRID-1-AD-1"
SUBNET="ocid1.subnet.oc1.eu-madrid-1.aaaaaaaaaosejizebedlihsfslqelbe2ujpeyuu3nlwegnbpjrbs5b2ng36q"
IMAGE="ocid1.image.oc1.eu-madrid-1.aaaaaaaarelc4d2mgm5sbscqrqusg5qsatr7uqofrmhnh5os4pbwdwkmj6oa"
SSH_KEY="$HOME/.ssh/oracle_cloud.pub"
SHAPE="VM.Standard.A1.Flex"
OCPUS=2
MEMORY_GB=12
BOOT_VOLUME_GB=50
DISPLAY_NAME="deals-scraper"
RETRY_INTERVAL=60

echo "=== Oracle Cloud ARM Instance Auto-Retry ==="
echo "Shape: $SHAPE ($OCPUS OCPUs, ${MEMORY_GB}GB RAM)"
echo "Region: eu-madrid-1"
echo "Reintentando cada ${RETRY_INTERVAL}s hasta conseguir capacidad..."
echo "Pulsa Ctrl+C para parar"
echo ""

ATTEMPT=0
while true; do
    ATTEMPT=$((ATTEMPT + 1))
    TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
    echo "[$TIMESTAMP] Intento #$ATTEMPT..."

    RESULT=$(oci compute instance launch \
        --compartment-id "$COMPARTMENT" \
        --availability-domain "$AD" \
        --shape "$SHAPE" \
        --shape-config "{\"ocpus\": $OCPUS, \"memoryInGBs\": $MEMORY_GB}" \
        --subnet-id "$SUBNET" \
        --image-id "$IMAGE" \
        --boot-volume-size-in-gbs "$BOOT_VOLUME_GB" \
        --display-name "$DISPLAY_NAME" \
        --assign-public-ip true \
        --ssh-authorized-keys-file "$SSH_KEY" \
        --metadata "{\"user_data\": \"\"}" \
        2>&1) || true

    if echo "$RESULT" | grep -q '"lifecycle-state"'; then
        echo ""
        echo "=== INSTANCIA CREADA! ==="
        INSTANCE_ID=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])")
        echo "Instance ID: $INSTANCE_ID"
        echo ""
        echo "Esperando IP pública..."
        sleep 30

        IP=$(oci compute instance list-vnics \
            --instance-id "$INSTANCE_ID" \
            --query "data[0].\"public-ip\"" \
            --raw-output 2>/dev/null) || true

        if [ -n "$IP" ] && [ "$IP" != "None" ]; then
            echo "IP pública: $IP"
            echo ""
            echo "=== Conectar con: ==="
            echo "ssh -i ~/.ssh/oracle_cloud ubuntu@$IP"
            echo ""
            echo "=== Desplegar con: ==="
            echo "bash deploy/deploy.sh $IP ~/.ssh/oracle_cloud"
        else
            echo "IP aún no asignada. Comprueba en la consola de Oracle Cloud."
            echo "Instance ID: $INSTANCE_ID"
        fi
        exit 0

    elif echo "$RESULT" | grep -qi "out of.*capacity\|InternalError"; then
        echo "[$TIMESTAMP] Sin capacidad. Reintentando en ${RETRY_INTERVAL}s..."

    elif echo "$RESULT" | grep -q "LimitExceeded"; then
        echo "[$TIMESTAMP] Límite de instancias alcanzado. ¿Ya tienes una instancia creada?"
        echo "$RESULT"
        exit 1

    elif echo "$RESULT" | grep -q "NotAuthorized"; then
        echo "[$TIMESTAMP] Error de autorización. Revisa los permisos."
        echo "$RESULT"
        exit 1

    elif echo "$RESULT" | grep -q "TooManyRequests"; then
        echo "[$TIMESTAMP] Rate limit. Esperando ${RETRY_INTERVAL}s..."

    else
        echo "[$TIMESTAMP] Error inesperado: $(echo "$RESULT" | grep -o '"message": "[^"]*"' || echo "$RESULT" | tail -3)"
        echo "Reintentando en ${RETRY_INTERVAL}s..."
    fi

    sleep "$RETRY_INTERVAL"
done
