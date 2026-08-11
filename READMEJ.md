# Gcrawler — GIGAZINE のニュースを取り込むクローラー

*English: [README.md](README.md)*

[AIradio](../AIradio) にニュースを供給する独立プロジェクト。RSS で新着を拾い、
記事ページから本文を取り、llama-server で要約して、スクリーンショットと一緒に
`news/YYYY-MM-DD/` に置く。

AIradio とは **ファイル契約だけ** で繋がっている。プロセス間通信も共有ライブラリも
無いので、片方を止めても、書き換えても、もう片方は動き続ける。

```
news/2026-08-10/0001.json   { id, url, title, summary, screenshot, crawled_at }
news/2026-08-10/0001.png    記事ページのスクリーンショット (1280x960)
```

設計の意図・内部の作り・問題が起きたときの対処は [TECHNICALJ.md](TECHNICALJ.md) を見ること。

## 必要なもの

- Python 3.12 以上と [uv](https://docs.astral.sh/uv/)
- 要約用の llama-server (llama.cpp をビルドしたもの) と GGUF モデル 1 つ。
  既定は `~/llama.cpp/build/bin/llama-server` と
  `~/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` (21GB、VRAM 約 22GB)
- スクリーンショットを撮るなら Playwright の Chromium

llama-server は必須ではない。落ちていても取り込みは止まらないが、要約の代わりに
本文の冒頭が入る (TECHNICALJ.md「要約が本文の冒頭になっている」)。

## セットアップ

```bash
git clone <このリポジトリ> Gcrawler
cd Gcrawler

uv sync                                 # .venv を作って httpx / playwright を入れる
uv run playwright install chromium      # スクショを撮らないなら不要

cp config.toml.example config.toml      # User-Agent の連絡先だけは書き換えること
```

`config.toml` は無くても動く (`gigazine_crawler.py` の `DEFAULTS` が使われる) が、
相手のサーバーにアクセスする以上、User-Agent の連絡先は書き換えておくこと。
書いたキーだけが上書きされるので、変えたい項目だけ残せばよい。

## 使い方

### 1. llama-server を上げる

```bash
./start_llm.sh          # フォアグラウンド (Ctrl-C で停止)
./start_llm.sh --bg     # バックグラウンド。応答するまで待って返る
```

すでに応答していれば二重には起動しない。モデルや宛先を変えるなら環境変数で上書きする:

| 環境変数 | 既定 |
| --- | --- |
| `GCRAWLER_LLAMA_SERVER` | `~/llama.cpp/build/bin/llama-server` |
| `GCRAWLER_LLM_MODEL` | `~/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` |
| `GCRAWLER_LLM_HOST` / `GCRAWLER_LLM_PORT` | `127.0.0.1` / `8080` |
| `GCRAWLER_LLM_CTX` | `16384` |
| `GCRAWLER_LLM_LOG` | `llm.log` (`--bg` のときだけ) |

宛先を変えたら `config.toml` の `llm_base_url` も合わせること。停止は
`--bg` が表示する PID に `kill` を送る。

### 2. 取り込む

1 日 1 回の実行を想定している。cron でも手動でもよい。

```bash
uv run --no-sync gigazine_crawler.py              # 既定 10 件
uv run --no-sync gigazine_crawler.py --limit 3
uv run --no-sync gigazine_crawler.py --dry-run    # 取得予定だけ見る
uv run --no-sync gigazine_crawler.py --no-shot    # スクショを撮らない (速い)
```

過去 7 日ぶんの JSON と URL を突き合わせて、取り込み済みの記事は飛ばす。
同じ日に複数回実行しても、連番は続きから振られる。

### 3. 要約をやり直す (必要なとき)

llama-server が落ちている間に取り込んだ記事は、後から要約だけ入れ替える。

```bash
./start_llm.sh --bg
uv run --no-sync resummarize.py --dry-run          # 対象を見るだけ
uv run --no-sync resummarize.py --truncated-only   # 文の途中で切れたものだけ
uv run --no-sync resummarize.py                    # 全部
```

クローラー本体は既知 URL を再取得しないので、取り込み済みの記事を直すのはこちら。
本文は取り直すが、書き換えるのは `summary` だけで `id` / `url` / `crawled_at` は
変えない。

### cron の例 (毎朝 7 時)

```
0 7 * * * cd $HOME/Gcrawler && ./.venv/bin/python gigazine_crawler.py >> crawl.log 2>&1
```

llama-server を常駐させていない場合、cron から `start_llm.sh --bg` を先に呼ぶか、
要約が本文の冒頭になるのを承知で回して後から `resummarize.py` で直す。

## 設定 (config.toml)

`config.toml` に書いたキーが `gigazine_crawler.py` の `DEFAULTS` を上書きする。

| キー | 既定 | 意味 |
| --- | --- | --- |
| `rss_url` | GIGAZINE の RSS 2.0 | 取り込み元 |
| `news_dir` | `news` | 出力先 (相対ならプロジェクト基準) |
| `request_interval_sec` | `1.5` | リクエスト間隔 |
| `dedupe_days` | `7` | 重複判定でさかのぼる日数 |
| `limit` | `10` | 1 回に取り込む上限 |
| `user_agent` | `Gcrawler/0.1 (...)` | 連絡先を書くこと |
| `llm_base_url` | `http://localhost:8080` | llama-server |
| `llm_max_tokens` / `llm_temperature` | `512` / `0.5` | 要約の生成 |
| `summary_max_chars` | `300` | 要約の文字数 |
| `body_max_chars` | `3000` | LLM に渡す本文の長さ |
| `screenshot_width` / `screenshot_height` | `1280` / `960` | スクショ |

## 掃除

古い `news/YYYY-MM-DD/` を消すのはこちらの責務。AIradio は直近 N 日ぶんを
読むだけで、消しには行かない。

```bash
find news -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
```

消す日数は `dedupe_days` (既定 7) より長くしておくこと。短くすると、消えた記事が
「未取得」に戻って二重に取り込まれる。

---

## ライセンス

[Apache License 2.0](LICENSE)。Copyright 2026 Kotetsu Yamamoto。

これはクローラーの**ソースコード**に対するもので、**取ってきたものには何も
及ばない**。`news/` に落ちる記事本文・要約・スクリーンショットの権利は
[GIGAZINE](https://gigazine.net/) と著者にあり、再配布には許諾が要ることがある
([NOTICE](NOTICE) 参照)。`news/` は git 管理外なので、リポジトリに記事の中身は
含まれない。

相手のサーバーにアクセスするプログラムなので、**実行前に `config.toml` の
`user_agent` に連絡の取れるアドレスを書くこと。**
