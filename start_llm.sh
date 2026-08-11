#!/usr/bin/env bash
# 要約用の llama-server を上げる。
#
#   ./start_llm.sh              # フォアグラウンドで起動 (Ctrl-C で停止)
#   ./start_llm.sh --bg         # バックグラウンドで起動し、応答するまで待つ
#   GCRAWLER_LLM_MODEL=... ./start_llm.sh
#
# gigazine_crawler.py / resummarize.py は llm_base_url (既定 localhost:8080) に
# 投げるだけなので、モデルを差し替えたいときはここか環境変数を書き換える。
# サーバーが落ちていても取り込みは止まらないが、summary は本文の冒頭で
# 代用されるので、取り込みの前にこれを上げておくこと。
set -euo pipefail

LLAMA_SERVER=${GCRAWLER_LLAMA_SERVER:-$HOME/llama.cpp/build/bin/llama-server}
MODEL=${GCRAWLER_LLM_MODEL:-$HOME/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf}
HOST=${GCRAWLER_LLM_HOST:-127.0.0.1}
PORT=${GCRAWLER_LLM_PORT:-8080}
# 記事本文 3000 字 + 要約で足りる。増やすと VRAM も増える
CTX=${GCRAWLER_LLM_CTX:-16384}
LOG=${GCRAWLER_LLM_LOG:-$(dirname "$(readlink -f "$0")")/llm.log}

background=0
[[ ${1:-} == --bg ]] && background=1

for path in "$LLAMA_SERVER" "$MODEL"; do
  if [[ ! -f $path ]]; then
    echo "見つかりません: $path" >&2
    exit 1
  fi
done

if curl -sf -m 2 "http://$HOST:$PORT/health" >/dev/null 2>&1; then
  echo "すでに $HOST:$PORT で応答しています。起動しません"
  exit 0
fi

# --jinja: チャットテンプレートを使う。要約は /v1/chat/completions で投げる
# -ngl 999: 全レイヤーを GPU に載せる (gfx1151, 48GB VRAM)
args=(
  -m "$MODEL"
  --host "$HOST" --port "$PORT"
  -c "$CTX" -ngl 999 --jinja
)

if (( background == 0 )); then
  echo "起動します (Ctrl-C で停止): $(basename "$MODEL") → $HOST:$PORT"
  exec "$LLAMA_SERVER" "${args[@]}"
fi

echo "起動します: $(basename "$MODEL") → $HOST:$PORT (ログ: $LOG)"
nohup "$LLAMA_SERVER" "${args[@]}" >"$LOG" 2>&1 &
pid=$!

# モデルのロードに数秒〜数十秒かかる。応答するまで待ってから抜ける
for _ in $(seq 60); do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "起動に失敗しました。ログを見てください: $LOG" >&2
    tail -20 "$LOG" >&2
    exit 1
  fi
  if curl -sf -m 2 "http://$HOST:$PORT/health" >/dev/null 2>&1; then
    echo "準備できました (PID $pid)。停止するには: kill $pid"
    exit 0
  fi
  sleep 2
done

echo "120 秒待っても応答しません (PID $pid)。ログ: $LOG" >&2
exit 1
