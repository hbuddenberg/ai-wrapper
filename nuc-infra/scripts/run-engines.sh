#!/bin/bash
# Run / smoke-test the GPU engines via ai-wrapper (CUDA hotswap).
# Usage:
#   ./run-engines.sh                       # smoke-test BOTH engines
#   ./run-engines.sh <model-alias> [prompt]# run one engine with a prompt
# Models: ornith-1.0-9b-heretic-mtp (llama-atomic) | gemma-4-12b-it-heretic (llama-tom)
set +e
cd "$(dirname "$0")/.." || exit 1        # -> nuc-infra/
set -a; . ./.env; set +a                  # source WRAPPER_API_KEY
H="Authorization: Bearer ${WRAPPER_API_KEY:?WRAPPER_API_KEY missing in .env}"
URL="http://localhost:5120/v1/chat/completions"
MODEL="$1"; PROMPT="${2:-Say hello in one short sentence.}"

gpu() { nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader; }
smoke() {
  local m="$1"
  echo ">>> $m   (GPU antes: $(gpu))"
  curl -s -m 180 -o /dev/null -w "    HTTP %{http_code} | %{time_total}s\n" \
    -H "$H" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"$PROMPT\"}],\"max_tokens\":40}" \
    "$URL"
  echo "    GPU despues: $(gpu)"
}

if [ -n "$MODEL" ]; then
  smoke "$MODEL"
else
  echo "== engines disponibles =="
  curl -s -H "$H" http://localhost:5120/v1/models | head -c 400; echo
  echo; smoke ornith-1.0-9b-heretic-mtp      # -> llama-atomic
  echo; smoke gemma-4-12b-it-heretic         # -> llama-tom (hotswap)
fi
