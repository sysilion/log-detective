#!/usr/bin/env sh
# Deploy log-detective to a Fly.io Sprite from your laptop.
#   prerequisites: `sprite login` done; Pinecone + Ollama Cloud connectors added in the Fly dashboard
#   usage: SPRITE=log-detective ./scripts/sprite-deploy.sh
set -eu
SPRITE="${SPRITE:-log-detective}"
cd "$(dirname "$0")/.."

if ! sprite list 2>/dev/null | grep -qw "$SPRITE"; then
  echo "==> creating sprite $SPRITE"
  sprite create "$SPRITE"
fi

echo "==> uploading code (git HEAD) to $SPRITE:~/log-detective"
git archive --format=tar HEAD | sprite exec -s "$SPRITE" --stdin -- \
  sh -c 'rm -rf "$HOME/log-detective" && mkdir -p "$HOME/log-detective" && tar -x -C "$HOME/log-detective"'

echo "==> setting up inside the sprite"
PASS="LOGDET_TZ=${LOGDET_TZ:-Asia/Seoul},LOGDET_MODEL=${LOGDET_MODEL:-}"
for v in LOGDET_PINECONE_NAMESPACE LOGDET_SKIP_DEMO LOGDET_INGEST_TOKEN LOGDET_TRIGGER_TOKEN LOGDET_PUBLIC_URL; do
  eval "val=\${$v:-}"
  if [ -n "$val" ]; then PASS="$PASS,$v=$val"; fi
done
sprite exec -s "$SPRITE" --env "$PASS" -- \
  sh -c 'cd "$HOME/log-detective" && sh scripts/sprite-setup.sh'

echo "==> sprite URL"
sprite info -s "$SPRITE" | grep -E "URL|Auth" || true
echo "(the URL is org-only by default; for a public demo run: sprite config update -s $SPRITE --url-auth public)"
