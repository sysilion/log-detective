#!/usr/bin/env sh
# Runs INSIDE the Sprite: installs the `logdet` wrapper, ingests the sample incident, starts the web service.
set -eu
APP="$HOME/log-detective"
TZNAME="${LOGDET_TZ:-Asia/Seoul}"
ENVV="LOGDET_HOME=$HOME/.logdet,LOGDET_TZ=$TZNAME"
[ -n "${LOGDET_MODEL:-}" ] && ENVV="$ENVV,LOGDET_MODEL=$LOGDET_MODEL"

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/logdet" <<WRAP
#!/usr/bin/env sh
export LOGDET_HOME="\${LOGDET_HOME:-$HOME/.logdet}" LOGDET_TZ="\${LOGDET_TZ:-$TZNAME}"
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
echo "--- ingest sample incident (yesterday, $TZNAME)"
logdet demo --out "$HOME/log-detective/data/sample"

echo "--- (re)starting web service on :8080"
sprite-env services delete logdet >/dev/null 2>&1 || true
sprite-env services create logdet --cmd python3 --args "-m,logdet.cli,serve,--port,8080" \
  --dir "$APP" --env "$ENVV,PYTHONPATH=$APP" --http-port 8080 --no-stream
sprite-env services list
