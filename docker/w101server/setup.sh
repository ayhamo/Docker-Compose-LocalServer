#!/bin/bash
set -e

# Error handler function
trap 'echo -e "\n\033[0;31m[ERROR] Script failed at line $LINENO. Check output above.\033[0m"; exit 1' ERR

echo "=== STARTING AUTOMATED WIZARD101 SERVER SETUP ==="

# Define fixed server IP
SERVER_IP="192.168.0.214"

# 1. Create directory structure
mkdir -p Imlight/repo Imlight/data

# 2. Clone or Update Imlight repository safely
if [ ! -d "Imlight/repo/.git" ]; then
    echo "--> Cloning Imlight repository..."
    git clone --recursive https://github.com/Revive101/Imlight.git Imlight/repo
else
    echo "--> Updating existing Imlight repository..."
    cd Imlight/repo
    git pull
    git submodule update --init --recursive
    cd ../..
fi

# 3. Copy Dockerfile backup into repository context
if [ -f "Imlight-Dockerfile" ]; then
    echo "--> Copying Imlight-Dockerfile to Imlight/repo/Dockerfile..."
    cp Imlight-Dockerfile Imlight/repo/Dockerfile
else
    echo -e "\n\033[0;31m[ERROR] Imlight-Dockerfile not found in $(pwd)!\033[0m"
    echo "Please save your Dockerfile as 'Imlight-Dockerfile' in $(pwd) and re-run."
    exit 1
fi

# 4. Copy manual JSON type dump file to Imcodec
JSON_DEST="Imlight/repo/submodule/Imcodec/src/Imcodec.ObjectProperty/GeneratorInput"
mkdir -p "$JSON_DEST"

if ls *.json 1> /dev/null 2>&1; then
    echo "--> Copying type dump JSON file to Imcodec..."
    cp *.json "$JSON_DEST/"
else
    echo -e "\n\033[0;31m[ERROR] No .json type dump file found in current directory ($(pwd))!\033[0m"
    echo "Please place your wiztype .json file in $(pwd) next to setup.sh and run this script again."
    exit 1
fi

# 5. Fetch latest patch revision & public patcher details
AURORIUM_URL="https://patcher.phill030.de"
echo "--> Fetching revision from ${AURORIUM_URL}..."
LATEST_REV=$(curl -sf "${AURORIUM_URL}/latest") || { echo "Failed to reach patcher URL"; exit 1; }
LATEST_REV_STRIPPED=$(echo "$LATEST_REV" | sed 's/V_//')

echo "--> Latest Game Revision: ${LATEST_REV}"

# 6. Download Root.wad from public patch server
ROOT_WAD_URL="${AURORIUM_URL}/${LATEST_REV}/Data/GameData/Root.wad"
if [ ! -f "Root.wad" ]; then
    echo "--> Downloading Root.wad..."
    curl -Lsf -o Root.wad "$ROOT_WAD_URL" || { echo "Failed to download Root.wad"; exit 1; }
fi

# 7. Extract Message XML files using python/wizwad container
echo "--> Unpacking Message XML files from Root.wad..."
docker run --rm -v "$(pwd)":/app -w /app python:3.11 bash -c "
    pip install --quiet wizwad && \
    wizwad extract /app/Root.wad /app/unpacked_root
"

# 8. Copy extracted XML messages into Imcodec GeneratorInput
MSG_DEST="Imlight/repo/submodule/Imcodec/src/Imcodec.MessageLayer/GeneratorInput"
mkdir -p "$MSG_DEST"
find unpacked_root -type f -name "*Messages*.xml" -exec cp {} "$MSG_DEST/" \;
echo "--> Moved XML message files to GeneratorInput."

# Cleanup temporary unpacking files
rm -rf unpacked_root Root.wad

# 9. Write docker-compose.yml
cat << 'EOF' > docker-compose.yml
services:
  imlight:
    build:
      context: ./Imlight/repo
      dockerfile: Dockerfile
    container_name: w101-imlight
    volumes:
      - ./Imlight/data:/app
    tmpfs:
      - /app/logs:size=256m
    restart: unless-stopped
    ports:
      - "12000:12000"   # Login Server
      - "12333:12333"   # Game Server (Centaur realm)
      - "12500:12500"   # Patch Server
    healthcheck:
      test: ["CMD-SHELL", "bash -c ':< /dev/tcp/127.0.0.1/12000'"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
EOF

# 10. Build and start container
echo "--> Building and starting Imlight Docker container..."
docker compose up -d --build

# 11. Configure Imlight.ini automatically after first initialization
echo "--> Waiting for initial config generation..."
sleep 10
INI_FILE="./Imlight/data/Config/Imlight.ini"

if [ -f "$INI_FILE" ]; then
    echo "--> Updating Imlight.ini with IP, Patcher URL, and Realm settings..."
    
    # Server IP, Patch Server, and Revision
    sed -i "s/^GameServerIP\s*=.*/GameServerIP = ${SERVER_IP}/" "$INI_FILE"
    sed -i "s|^PatchServerInternalUrl\s*=.*|PatchServerInternalUrl = ${AURORIUM_URL}|" "$INI_FILE"
    sed -i "s/^GameRevision\s*=.*/GameRevision = ${LATEST_REV_STRIPPED}/" "$INI_FILE"
    
    # Game Server Realm Configuration
    sed -i "s/^GameServerCount\s*=.*/GameServerCount = 1/" "$INI_FILE"
    sed -i "s/^MaxGameServersAllowed\s*=.*/MaxGameServersAllowed = 2/" "$INI_FILE"
    sed -i "s/^RealmNames\s*=.*/RealmNames = Pegasus/" "$INI_FILE"
    
    echo "--> Restarting container to apply Imlight.ini settings..."
    docker compose restart imlight
else
    echo "--> [WARNING] Imlight.ini not found yet. Container will use standard defaults until restarted."
fi

# 12. Clean up Docker build cache to free up disk space
echo "--> Pruning Docker build cache..."
docker builder prune -a -f

# 13. Delete Repo as building is done
rm -rf Imlight/repo

echo "=== SETUP COMPLETE ==="
