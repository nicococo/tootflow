#!/bin/bash
set -e

echo "→ Pulling latest code..."
git pull

echo "→ Building and restarting app..."
docker compose up --build -d app

echo "→ Waiting for health check..."
for i in $(seq 1 30); do
  if docker compose exec app curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo "✓ App is healthy."
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "✗ Health check timed out. Check logs:"
    docker compose logs app --tail=50
    exit 1
  fi
  sleep 2
done

echo "✓ Deploy complete."
