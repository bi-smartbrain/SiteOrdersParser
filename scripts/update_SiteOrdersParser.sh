#!/bin/bash
set -e
cd /opt/SiteOrdersParser
git fetch origin main
git reset --hard origin/main
docker compose up -d --build --remove-orphans
docker image prune -f
docker builder prune -f --filter "until=168h"
cp scripts/update_SiteOrdersParser.sh /opt/auto/update_SiteOrdersParser.sh
chmod +x /opt/auto/update_SiteOrdersParser.sh
