#!/bin/sh
# Switch which database the local backend talks to, then restart it.
#   ./use-env.sh local   -> local mongod + seeded synthetic data
#   ./use-env.sh prod    -> PRODUCTION Mongo (live HR data), scheduler off
set -e
cd "$(dirname "$0")"
case "$1" in
  local) SRC=.env.local-test ;;
  prod)  SRC=.env.production ;;
  *)     echo "usage: $0 local|prod"; exit 1 ;;
esac
[ -f "$SRC" ] || { echo "missing $SRC"; exit 1; }

if [ "$1" = prod ]; then
  # Host/port come from the (gitignored) env file rather than being written
  # here, so this script carries no infrastructure details.
  HOSTPORT=$(sed -n 's#^MONGO_URL=.*@\([^/?]*\).*#\1#p' "$SRC" | head -1)
  HOST=${HOSTPORT%%:*}; PORT=${HOSTPORT##*:}
  [ "$PORT" = "$HOST" ] && PORT=27017
  # Fail fast with a clear message rather than a 30s driver timeout.
  nc -z -w 8 "$HOST" "$PORT" 2>/dev/null || {
    echo "Cannot reach $HOST:$PORT — connect to the office network/VPN, or"
    echo "have the server allow this machine's IP."
    exit 1
  }
  printf 'This points the local server at LIVE HR DATA. Continue? [y/N] '
  read -r a; [ "$a" = y ] || [ "$a" = Y ] || { echo aborted; exit 1; }
fi

cp "$SRC" .env && chmod 600 .env
lsof -nP -iTCP:8001 -sTCP:LISTEN -t 2>/dev/null | xargs -r kill 2>/dev/null || true
sleep 2
echo "now using: $(grep MONGO_DB_NAME .env)"
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8001
