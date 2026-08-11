# Gcrawler technical notes

*日本語版: [TECHNICALJ.md](TECHNICALJ.md)*

For usage, see [README.md](README.md). This document covers why it is built this
way, and how to fix it when it breaks.

## 1. The pipeline

```
RSS (gigazine.net/news/rss_2.0/)
  ↓ fetch_rss()          title / url / description
  ↓ known_urls()         drop URLs already present in the last 7 days of JSON
  ↓ fetch_body()         article page → prose inside <div id="article"> (up to 3000 chars)
  ↓ summarize()          llama-server /v1/chat/completions → a 300-character summary
  ↓ take_screenshot()    1280x960 via Playwright/Chromium
  ↓ save_article()       news/YYYY-MM-DD/NNNN.{json,png}
```

There are only three files: `gigazine_crawler.py` (ingestion), `resummarize.py`
(redoing summaries), and `start_llm.sh` (bringing up llama-server).

## 2. Design commitments

- **Start from RSS.** More robust than scraping the index out of HTML, and more
  polite to the other end. Only the body comes from the article page, and when
  that breaks it falls back to the RSS description so ingestion continues
- **Assume it will not run every day.** At ingestion time a set of known URLs is
  built from the last `dedupe_days` (7) days of JSON, and duplicates are skipped
- **Space out the requests** (1.5s per request by default), and put a contact
  address in the User-Agent
- **Write JSON to a temp file, then `replace()` it in.** AIradio never sees a
  half-written file, even if it reads while we write (both in `save_article()`
  and in `resummarize.py`)
- **File contract with AIradio, nothing else.** No IPC, no shared library — the
  shape of `news/YYYY-MM-DD/NNNN.json` is the entire agreement. Either side can
  be stopped or rewritten and the other keeps running

### Degrading instead of stopping

Both summarization and screenshots are on the "ingestion continues without it"
side of the line. The worst outcome would be one article's failure taking down
the whole day's run.

| What went down | What happens |
| --- | --- |
| Article page fetch / body extraction | the RSS description is used as the body |
| llama-server | the beginning of the body goes in as the summary (see 6.1) |
| Playwright / Chromium | the JSON is written with `screenshot: null` |
| RSS | the only unrecoverable one; exits with status 1 |

## 3. Body extraction (ArticleExtractor)

Written against the standard library's `html.parser` alone. GIGAZINE keeps the
article body in `<div id="article">`, so extraction starts once a `div` whose
`id` or `class` contains `article` / `content` / `cntnts` is entered, collecting
prose and discarding the contents of `script` / `style` / `nav` / `header` /
`footer` / `form`.

**This is knowingly fragile.** A layout change means nothing gets collected — but
then it returns an empty string, falls back to the RSS description, and ingestion
does not stop. That is preferred over adding an external extraction library:
the ways it can break stay predictable.

**Symptom**: summaries that are oddly short, or that do not match the article.
**Check**: call `fetch_body(url, load_config())` directly and see if the result
is empty.
**Fix**: add the new `id` / `class` to `ArticleExtractor.TARGET`.

## 4. Summarization (summarize)

Just a POST to llama-server's OpenAI-compatible `/v1/chat/completions`. The
prompt lives in `SUMMARY_SYSTEM` and asks for "only what is in the body", "no
bullet points", and "must end with 。".

### Qwen3-family models emit thinking by default, and return empty content

The request carries `"chat_template_kwargs": {"enable_thinking": False}` to
suppress it. Without that, the model returns only its reasoning, `message.content`
comes back as an empty string, and every summary falls through to the fallback
(the beginning of the body). **When swapping in a model that emits thinking,
verify that its chat template honors this flag.**

### Cutting at 300 characters must not break the sentence

The model exceeds 300 characters no matter how it is instructed. A plain
`text[:300]` cuts mid-word — `…MicrosoftはManifes` — and hands a sentence fragment
to AIradio's script generation. `clip_to_sentence()` walks back to the last 。
before returning.

```python
def clip_to_sentence(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    kept, period, _ = head.rpartition("。")
    return kept + period if kept else head
```

Text at or under the limit is left untouched. If there is no 。 at all it is cut
bluntly, as before (a summary cut mid-sentence still beats no summary).

**The remaining hole**: if there is exactly one 。 within the limit and it sits
very early, the summary shrinks to that single sentence. This is unlikely in
Japanese prose, so no threshold guard was added. If it matters, add a "fall back
to the blunt cut when the result gets too short" rule.

## 5. Screenshots

Playwright's Chromium, 1280x960, captured 2 seconds after `domcontentloaded`.
`full_page=False` because GIGAZINE articles are very tall, and a full-page capture
shrinks too much to work as a background in the display layer. Playwright raises
many exception types, so they are caught with a single `except Exception`, which
returns `False` and lets ingestion continue.

## 6. Problems and fixes

### 6.1 The summary is the beginning of the article body (llama-server was down)

**Symptom**: `summary` is exactly 300 characters and starts with the article
page's header (date and category), e.g. `2026年08月10日 19時00分メモ…`. The run
printed `要約に失敗、本文の冒頭で代用します: ...` to stderr.

**Cause**: `summarize()` swallows connection failures and returns the beginning
of the body. This is deliberate — without a summary, AIradio's script generation
has no input at all.

**Check**:

```bash
curl -s -m 3 http://localhost:8080/v1/models   # exit 7 when it is down
ss -ltn | grep :8080
```

```bash
# ingested articles whose summary ends mid-sentence
./.venv/bin/python -c "
import json, glob
for p in sorted(glob.glob('news/*/*.json')):
    d = json.load(open(p))
    if not d['summary'].endswith('。'): print(p, len(d['summary']))
"
```

**Fix**: bring up llama-server, then swap in the summaries.

```bash
./start_llm.sh --bg
uv run --no-sync resummarize.py --truncated-only
```

Re-running `gigazine_crawler.py` will not fix it: known URLs are skipped by the
`dedupe_days` duplicate check.

### 6.2 llama-server will not start

`start_llm.sh --bg` waits 120 seconds and, if nothing responds, reports where the
log is (`llm.log`). If the process dies immediately it prints the last 20 lines
of that log on the spot.

- **Model not found**: point `GCRAWLER_LLM_MODEL` at a path that exists
- **Not enough VRAM**: the default model takes about 22GB (Q4_K_XL, ctx 16384).
  Lower `GCRAWLER_LLM_CTX` or swap in a smaller model
- **Port already taken**: a responding server suppresses the second launch, but
  if something else holds 8080, change both `GCRAWLER_LLM_PORT` and
  `llm_base_url` in `config.toml`

### 6.3 The same article is ingested twice

`known_urls()` only looks at the last `dedupe_days` (7) days of directories.
Deleting old `news/YYYY-MM-DD/` on a cycle shorter than that puts articles still
present in the RSS feed back into the "not yet ingested" state. Keep the cleanup
window longer than `dedupe_days`.

The same article landing under two dates is not intended, but `id` (the first 16
hex digits of the URL's SHA-1) is identical, so AIradio can drop the duplicate.

### 6.4 Screenshots fail

`playwright がありません` means `uv run playwright install chromium`. A timeout
(45 seconds) or a page-side problem results in JSON written with
`screenshot: null`. Screenshots are only used as a background in the display
layer, so ingestion still counts as a success. `--no-shot` skips them entirely
and makes runs considerably faster.

### 6.5 RSS cannot be fetched

The one unrecoverable case: it exits with status 1. Check whether `rss_url` has
changed, or whether the User-Agent is being rejected.

## 7. Notes for anyone changing this

- Every return path out of `summarize()` must go through `clip_to_sentence()`.
  There are three (the LLM's output, an empty output, and the body fallback when
  the LLM fails); letting any one of them through raw hands a fragment to AIradio
- `resummarize.py` may only rewrite `summary`. Changing `id` / `url` /
  `crawled_at` makes it a different article as far as AIradio is concerned
- New output fields have to be agreed with AIradio (`AIradio/HANDOFF.md` §0).
  Adding JSON fields is safe; removing or renaming them breaks the contract
