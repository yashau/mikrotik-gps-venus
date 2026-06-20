#!/bin/sh
set -eu

APP_DIR="${APP_DIR:-/data/mikrotik-gps-venus}"

cd "$APP_DIR"
chmod +x scripts/start scripts/stop scripts/restart scripts/status scripts/logs

ln -sf scripts/start start
ln -sf scripts/stop stop
ln -sf scripts/restart restart
ln -sf scripts/status status
ln -sf scripts/logs logs

echo "Installed helpers: ./start ./stop ./restart ./status ./logs"
