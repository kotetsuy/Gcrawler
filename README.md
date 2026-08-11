# Gcrawler — a crawler that ingests GIGAZINE news

*日本語版: [READMEJ.md](READMEJ.md)*

A standalone project that feeds news to [AIradio](../AIradio). It picks up new
entries from the RSS feed, pulls the article body from the article page,
summarizes it with llama-server, and drops the result — along with a screenshot —
into `news/YYYY-MM-DD/`.

It is wired to AIradio by a **file contract only**. There is no IPC and no shared
library, so either side can be stopped or rewritten and the other keeps running.

```
news/2026-08-10/0001.json   { id, url, title, summary, screenshot, crawled_at }
news/2026-08-10/0001.png    screenshot of the article page (1280x960)
```

For the design intent, the internals, and what to do when something breaks, see
[TECHNICAL.md](TECHNICAL.md).

## Requirements

- Python 3.12 or later and [uv](https://docs.astral.sh/uv/)
- llama-server (a llama.cpp build) and one GGUF model for summarization.
  The defaults are `~/llama.cpp/build/bin/llama-server` and
  `~/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` (21GB on disk, ~22GB of VRAM)
- Playwright's Chromium, if you want screenshots

llama-server is not mandatory. Ingestion does not stop when it is down, but the
beginning of the article body goes in where the summary should be (see
TECHNICAL.md, "The summary is the beginning of the article body").

## Setup

```bash
git clone <this repository> Gcrawler
cd Gcrawler

uv sync                                 # creates .venv, installs httpx / playwright
uv run playwright install chromium      # not needed if you skip screenshots

cp config.toml.example config.toml      # at minimum, change the contact in the User-Agent
```

`config.toml` is optional — without it the `DEFAULTS` in `gigazine_crawler.py`
are used — but since this hits someone else's server, do put a real contact
address in the User-Agent. Only the keys you write are overridden, so you can
keep just the ones you want to change.

## Usage

### 1. Bring up llama-server

```bash
./start_llm.sh          # foreground (Ctrl-C to stop)
./start_llm.sh --bg     # background; waits until it responds, then returns
```

It will not start a second instance if one already responds. Override the model
or the address with environment variables:

| Environment variable | Default |
| --- | --- |
| `GCRAWLER_LLAMA_SERVER` | `~/llama.cpp/build/bin/llama-server` |
| `GCRAWLER_LLM_MODEL` | `~/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` |
| `GCRAWLER_LLM_HOST` / `GCRAWLER_LLM_PORT` | `127.0.0.1` / `8080` |
| `GCRAWLER_LLM_CTX` | `16384` |
| `GCRAWLER_LLM_LOG` | `llm.log` (only with `--bg`) |

If you change the address, change `llm_base_url` in `config.toml` to match. To
stop it, `kill` the PID that `--bg` prints.

### 2. Ingest

Meant to run once a day, from cron or by hand.

```bash
uv run --no-sync gigazine_crawler.py              # 10 articles by default
uv run --no-sync gigazine_crawler.py --limit 3
uv run --no-sync gigazine_crawler.py --dry-run    # only list what would be fetched
uv run --no-sync gigazine_crawler.py --no-shot    # no screenshots (fast)
```

URLs are checked against the last 7 days of JSON, and articles already ingested
are skipped. Running it several times on the same day continues the sequence
numbers rather than restarting them.

### 3. Redo the summaries (when needed)

Articles ingested while llama-server was down can have their summaries swapped
in afterwards.

```bash
./start_llm.sh --bg
uv run --no-sync resummarize.py --dry-run          # only list the targets
uv run --no-sync resummarize.py --truncated-only   # only the ones cut mid-sentence
uv run --no-sync resummarize.py                    # everything
```

The crawler itself never re-fetches a known URL, so this is the tool for fixing
articles already on disk. It re-fetches the body, but only `summary` is
rewritten — `id` / `url` / `crawled_at` are left alone.

### cron example (7am daily)

```
0 7 * * * cd $HOME/Gcrawler && ./.venv/bin/python gigazine_crawler.py >> crawl.log 2>&1
```

If llama-server is not kept running, either call `start_llm.sh --bg` from cron
first, or accept that the summaries will be body prefixes and fix them later
with `resummarize.py`.

## Configuration (config.toml)

Keys written in `config.toml` override the `DEFAULTS` in `gigazine_crawler.py`.

| Key | Default | Meaning |
| --- | --- | --- |
| `rss_url` | GIGAZINE's RSS 2.0 | where articles come from |
| `news_dir` | `news` | output directory (relative paths resolve against the project) |
| `request_interval_sec` | `1.5` | delay between requests |
| `dedupe_days` | `7` | how many days back the duplicate check looks |
| `limit` | `10` | maximum articles per run |
| `user_agent` | `Gcrawler/0.1 (...)` | put a contact address here |
| `llm_base_url` | `http://localhost:8080` | llama-server |
| `llm_max_tokens` / `llm_temperature` | `512` / `0.5` | summary generation |
| `summary_max_chars` | `300` | summary length |
| `body_max_chars` | `3000` | how much body text is handed to the LLM |
| `screenshot_width` / `screenshot_height` | `1280` / `960` | screenshots |

## Cleanup

Deleting old `news/YYYY-MM-DD/` directories is this project's responsibility.
AIradio only reads the last N days; it never deletes.

```bash
find news -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
```

Keep the retention longer than `dedupe_days` (7 by default). Anything shorter
makes deleted articles look un-ingested again, and they come back in duplicate.

---

## License

[Apache License 2.0](LICENSE). Copyright 2026 Kotetsu Yamamoto.

That covers the crawler's source code. **It says nothing about what the crawler
collects.** Article text, summaries and screenshots downloaded into `news/`
remain the property of [GIGAZINE](https://gigazine.net/) and its authors, and
republishing them may require permission — see [NOTICE](NOTICE). `news/` is
git-ignored, so no article content is in this repository.

Since this program talks to someone else's server, **put a real contact address
in `config.toml`'s `user_agent` before running it.**
