#!/usr/bin/env bash

URL="https://raw.githubusercontent.com/saganos/yazio_public_api/master/examples/login.js"
CONTENT=$(curl -sL "$URL")

CLIENT_ID=$(echo "$CONTENT" | grep -oP 'client_id",\s*"\K[^"]+')
CLIENT_SECRET=$(echo "$CONTENT" | grep -oP 'client_secret",\s*"\K[^"]+')

echo "=========================================="
echo "YAZIO Client ID:"
echo "$CLIENT_ID"
echo ""
echo "YAZIO Client Secret:"
echo "$CLIENT_SECRET"
echo "=========================================="
