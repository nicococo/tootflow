#!/bin/bash
# TootFlow deployment script
#
# Usage:
#   ./deploy.sh           — standard deploy (uses Docker layer cache)
#   ./deploy.sh --fresh   — full rebuild (use when requirements.txt changed)
#
# Deployment process:
#   1. Run unit tests locally before calling this script
#   2. Run any pending migrations: python -m app.migrations.xxx --prod
#   3. Run this script on the server (or via ssh)
set -e

FRESH=${1:-""}

echo "→ Pulling latest code..."
git pull

# Tag the current commit for easy rollback
VERSION=$(git rev-parse --short HEAD)
echo "→ Deploying commit ${VERSION}..."

echo "→ Building app..."
if [ "$FRESH" = "--fresh" ]; then
  echo "  (full rebuild — no cache)"
  docker compose build --no-cache app
else
  docker compose build app
fi

echo "→ Starting app..."
docker compose up -d app

echo "→ Waiting for health check..."
for i in $(seq 1 30); do
  if docker compose exec app curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo "✓ App is healthy (${VERSION})."
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "✗ Health check timed out. Last 50 log lines:"
    docker compose logs app --tail=50
    echo ""
    echo "To rollback, run:"
    echo "  git checkout <previous-commit>"
    echo "  ./deploy.sh"
    exit 1
  fi
  sleep 2
done

echo "✓ Deploy complete (${VERSION})."
