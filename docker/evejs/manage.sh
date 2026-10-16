#!/bin/bash
# EVE.js Automated Setup & Update Manager

echo "========================================="
echo " EVE.js Automated Setup Manager"
echo "========================================="

# Ask the user for the mode
read -p "Is this a [f]resh install or an [u]pdate? (f/u): " MODE

# Auto-detect the zip file
ZIP_FILE=$(ls *.zip 2>/dev/null | head -n 1)

if [ -z "$ZIP_FILE" ]; then
    echo "[!] No .zip file found in the current directory."
    echo "[!] Please upload the EVE.js release zip here and try again."
    exit 1
fi

echo "[->] Found release archive: $ZIP_FILE"

if [[ "$MODE" == "u" || "$MODE" == "U" ]]; then
    echo "[->] Update Mode Selected"
    echo "[1/10] Stopping current containers cleanly..."
    docker compose down

    echo "[2/10] Backing up existing configuration files..."
    mkdir -p /tmp/evejs_backup
    cp -r config /tmp/evejs_backup/ 2>/dev/null
    cp .env /tmp/evejs_backup/ 2>/dev/null
else
    echo "[->] Fresh Install Mode Selected"
    echo "[1/10] No old containers to stop."
    echo "[2/10] No existing configuration to back up."
fi

echo "[3/10] Extracting $ZIP_FILE and handling nested folders..."
mkdir -p /tmp/evejs_extract
unzip -q -o "$ZIP_FILE" -d /tmp/evejs_extract

EXTRACTED_ROOT=$(find /tmp/evejs_extract -name "compose.yaml" -printf '%h\n' -quit)

if [ -z "$EXTRACTED_ROOT" ]; then
    echo "[!] Error: compose.yaml not found inside the zip. Aborting."
    rm -rf /tmp/evejs_extract
    exit 1
fi

echo "      Moving files from nested directory ($EXTRACTED_ROOT)..."
cp -a "$EXTRACTED_ROOT"/. ./
rm -rf /tmp/evejs_extract

echo "[4/10] Deleting the ZIP file to save space..."
rm "$ZIP_FILE"

if [[ "$MODE" == "u" || "$MODE" == "U" ]]; then
    echo "[5/10] Restoring configurations..."
    cp -r /tmp/evejs_backup/config/* config/ 2>/dev/null
    cp /tmp/evejs_backup/.env .env 2>/dev/null
    rm -rf /tmp/evejs_backup
else
    echo "[5/10] Skipping config restore (Fresh Install)..."
fi

echo "[6/10] Configuring host data directories and permissions..."
# Ensure local data directory exists with correct Node user permissions (UID 1000)
mkdir -p ./data
chown -R 1000:1000 ./data

echo "[7/10] Patching compose.yaml for local storage, tmpfs logs, and custom ports..."
# 1. Replace named volume evejs-data with local ./data bind mount
sed -i 's/evejs-data:\/var\/lib\/evejs/\.\/data:\/var\/lib\/evejs/g' compose.yaml

# 2. Remove evejs-logs volume mount line and append tmpfs block under server service
sed -i '/- evejs-logs:\/app\/server\/logs/d' compose.yaml
sed -i '/server:/a \    tmpfs:\n      - /app/server/logs:rw,noexec,nosuid,size=100m' compose.yaml

# 3. Strip out named volume definitions at the bottom of the file
sed -i '/^volumes:/,$d' compose.yaml

# 4. Replace loopback with wildcard ONLY for port mappings (preserves URLs)
sed -i 's/- "127\.0\.0\.1:/- "0.0.0.0:/g' compose.yaml

# 5. Fix the 443 mapping to 26003 for Nginx bypass
sed -i 's/"0\.0\.0\.0:443:26003"/"0.0.0.0:26003:26003"/g' compose.yaml

# 6. Strip out variables and hardcode explicit ports
sed -i 's/${EVEJS_SERVER_PORT:-26000}/26000/g' compose.yaml
sed -i 's/${EVEJS_IMAGE_SERVER_PORT:-26001}/26001/g' compose.yaml
sed -i 's/${EVEJS_MICROSERVICES_PORT:-26002}/26002/g' compose.yaml
sed -i 's/${EVEJS_REDSHIFT_MONITOR_PORT:-26400}/26400/g' compose.yaml

echo "[8/10] Pruning Docker build cache before building..."
docker builder prune -f

if [[ "$MODE" == "f" || "$MODE" == "F" ]]; then
    echo "[->] Running first-time initializations..."
    docker compose build init
    docker compose run --rm --no-deps market-tools rebuild v1 --preset jita_new_caldari
    docker compose up -d
else
    echo "[->] Rebuilding and starting updated containers..."
    docker compose build
    docker compose up -d
fi

echo "[9/10] Pruning Docker build cache after building..."
docker builder prune -f

echo "[10/10] Waiting for certificates to generate..."
sleep 15 

echo "Packaging certificates into MOVE_CERTS directory..."
mkdir -p MOVE_CERTS/server/certs
mkdir -p MOVE_CERTS/server/src/_secondary/express/certs
cp -r server/certs/* MOVE_CERTS/server/certs/ 2>/dev/null
cp -r server/src/_secondary/express/certs/* MOVE_CERTS/server/src/_secondary/express/certs/ 2>/dev/null

echo ""
echo "======================================================================="
echo "                      SERVER SETUP COMPLETE!                           "
echo "======================================================================="
echo " Next steps for your Windows PC:"
echo ""
echo " 1. COPY CERTIFICATES:"
echo "    Download the 'MOVE_CERTS' folder from this Linux server."
echo "    Drag the 'server' folder inside it into your Windows EVE.js folder."
echo ""
echo " 2. START THE LOCALHOST BRIDGE:"
echo "    Open PowerShell on Windows and run this exact command (leave it open):"
echo "    ssh -L 26000:127.0.0.1:26000 -L 26001:127.0.0.1:26001 -L 26002:127.0.0.1:26002 -L 26003:127.0.0.1:26003 -L 443:127.0.0.1:26003 -L 5222:127.0.0.1:5222 ayham@192.168.0.214 -N"
echo ""
echo " 3. RUN CLIENT SETUP (If fresh install or new certs):"
echo "    Navigate to tools\\ClientSETUP\\ in your Windows EVE.js folder."
echo "    Double-click 'StartClientSetup.bat' and complete the steps."
echo ""
echo " 4. PLAY THE GAME:"
echo "    Run 'Play.bat' from your main Windows EVE.js folder to launch!"
echo "======================================================================="