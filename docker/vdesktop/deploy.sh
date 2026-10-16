#!/bin/bash
set -e

# Always target the project directory
PROJECT_DIR="/opt/docker/vdesktop"
cd "$PROJECT_DIR"

# Clear Docker build cache
echo "=== Clearing Docker build cache ==="
docker builder prune -f

# Rebuild without stale layers and bring up container
echo "=== Building and Starting Containers ==="
docker compose build --no-cache
docker compose up -d

echo "=== Deployment Complete ==="
