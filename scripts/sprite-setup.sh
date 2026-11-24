#!/usr/bin/env sh
# Runs INSIDE the Sprite: installs the `logdet` wrapper, ingests the sample incident, starts the web service.
set -eu
APP="$HOME/log-detective"
TZNAME="${LOGDET_TZ:-Asia/Seoul}"
# pin the demo clock to "today 09:00" at deploy time, so "yesterday 3am" keeps meaning the sample incident day
DEMO_NOW="${LOGDET_NOW:-$(TZ=$TZNAME date +%Y-%m-%dT09:00)}"
# the demo dataset is fixed in the past: never prune it (belt and braces with the pinned clock)
RETENTION="${LOGDET_RETENTION_DAYS:-0}"
ENVV="LOGDET_HOME=$HOME/.logdet,LOGDET_TZ=$TZNAME,LOGDET_NOW=$DEMO_NOW,LOGDET_RETENTION_DAYS=$RETENTION"
NS="${LOGDET_PINECONE_NAMESPACE:-logs}"
ENVV="$ENVV,LOGDET_PINECONE_NAMESPACE=$NS"
[ -n "${LOGDET_MODEL:-}" ] && ENVV="$ENVV,LOGDET_MODEL=$LOGDET_MODEL"
[ -n "${LOGDET_PUBLIC_URL:-}" ] && ENVV="$ENVV,LOGDET_PUBLIC_URL=$LOGDET_PUBLIC_URL"
# push ingest / run triggers stay disabled unless a token is passed (never on the public demo)
[ -n "${LOGDET_INGEST_TOKEN:-}" ] && ENVV="$ENVV,LOGDET_INGEST_TOKEN=$LOGDET_INGEST_TOKEN"
[ -n "${LOGDET_TRIGGER_TOKEN:-}" ] && ENVV="$ENVV,LOGDET_TRIGGER_TOKEN=$LOGDET_TRIGGER_TOKEN"

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/logdet" <<WRAP
#!/usr/bin/env sh
export LOGDET_HOME="\${LOGDET_HOME:-$HOME/.logdet}" LOGDET_TZ="\${LOGDET_TZ:-$TZNAME}" LOGDET_NOW="\${LOGDET_NOW-$DEMO_NOW}" LOGDET_RETENTION_DAYS="\${LOGDET_RETENTION_DAYS-$RETENTION}" LOGDET_PINECONE_NAMESPACE="\${LOGDET_PINECONE_NAMESPACE-$NS}"
${LOGDET_MODEL:+export LOGDET_MODEL="\${LOGDET_MODEL:-$LOGDET_MODEL}"}
exec env PYTHONPATH="$APP" python3 -m logdet.cli "\$@"
WRAP
chmod +x "$HOME/.local/bin/logdet"
export PATH="$HOME/.local/bin:$PATH"

python3 --version
echo "--- connectors visible to this sprite:"
curl -s https://api.sprites.dev/v1/gateway/list | python3 -c 'import json,sys; d=json.load(sys.stdin); print([c.get("provider") for c in d.get("connections", [])] or "none (check connector access policies)")' || true
echo
echo "--- logdet doctor"
logdet doctor
if [ "${LOGDET_SKIP_DEMO:-0}" != "1" ]; then
  echo "--- ingest sample incident (yesterday, $TZNAME) into namespace '$NS'"
  logdet demo --out "$HOME/log-detective/data/sample"
  echo "--- watch replay over the sample day (fills the Watch / Alerts panel; 1 LLM call per incident)"
  logdet watch --replay --reset || echo "watch replay failed (non-fatal)"
fi

echo "--- (re)starting web service on :8080"
sprite-env services delete logdet >/dev/null 2>&1 || true
sprite-env services create logdet --cmd python3 --args "-m,logdet.cli,serve,--port,8080" \
  --dir "$APP" --env "$ENVV,PYTHONPATH=$APP" --http-port 8080 --no-stream
# print service names/status only: the env may hold tokens
sprite-env services list | python3 -c 'import json,sys; [print(s["name"], s["state"]["status"]) for s in json.load(sys.stdin)]'
