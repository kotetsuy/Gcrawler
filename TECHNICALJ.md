# Gcrawler 技術メモ

*English: [TECHNICAL.md](TECHNICAL.md)*

使い方は [READMEJ.md](READMEJ.md)。こちらは「なぜこう作ったか」と
「壊れたときにどう直すか」。

## 1. 全体の流れ

```
RSS (gigazine.net/news/rss_2.0/)
  ↓ fetch_rss()          title / url / description
  ↓ known_urls()         過去 7 日ぶんの JSON と突き合わせて既知 URL を落とす
  ↓ fetch_body()         記事ページ → <div id="article"> の地の文 (3000 字まで)
  ↓ summarize()          llama-server /v1/chat/completions → 300 字の要約
  ↓ take_screenshot()    Playwright/Chromium で 1280x960
  ↓ save_article()       news/YYYY-MM-DD/NNNN.{json,png}
```

ファイルは `gigazine_crawler.py` (取り込み)、`resummarize.py` (要約のやり直し)、
`start_llm.sh` (llama-server の起動) の 3 つだけ。

## 2. 設計上の約束

- **RSS 起点**。一覧を HTML から掻き取るより壊れにくく、相手にも礼儀正しい。
  本文だけは記事ページから取るが、そこが壊れたら RSS の description に落として
  取り込み自体は続ける
- **毎日実行される保証は無い前提**。取り込み時に過去 7 日ぶん (`dedupe_days`) の
  JSON から既知 URL 集合を作り、重複記事はスキップする
- **アクセス間隔を空ける** (既定 1.5 秒/リクエスト)。User-Agent に連絡先を書く
- **JSON は一時ファイルに書いてから `replace()` で差し替える**。AIradio が読んで
  いる最中でも壊れたファイルは見えない (`save_article()`、`resummarize.py` とも)
- **AIradio とはファイル契約のみ**。プロセス間通信も共有ライブラリも無く、
  `news/YYYY-MM-DD/NNNN.json` の形だけが約束。片方を止めても書き換えても、
  もう片方は動き続ける

### 止まらないための切り分け

要約もスクショも「無くても取り込みは続ける」側に倒してある。1 記事の失敗で
その日の取り込み全部が飛ぶのが一番困るため。

| 落ちたもの | どうなるか |
| --- | --- |
| 記事ページの取得・本文抽出 | RSS の description を本文の代わりに使う |
| llama-server | 要約の代わりに本文の冒頭が入る (下記 6.1) |
| Playwright / Chromium | `screenshot: null` で JSON だけ書く |
| RSS | ここだけは続行不能。終了コード 1 で止まる |

## 3. 本文抽出 (ArticleExtractor)

標準ライブラリの `html.parser` だけで書いてある。GIGAZINE の記事本文は
`<div id="article">` に入っているので、`id` か `class` に `article` / `content` /
`cntnts` を含む `div` に入ったところから地の文を集め、`script` / `style` /
`nav` / `header` / `footer` / `form` の中身は捨てる。

**壊れやすいのは承知の上**。レイアウトが変われば何も集まらないが、そのときは
空文字を返して RSS の description に落ちるだけで、取り込みは止まらない。
外部の抽出ライブラリを足すより、壊れ方が読める方を選んでいる。

**症状**: 要約がやたら短い、記事の中身と食い違う。
**確認**: `fetch_body(url, load_config())` を直接呼んで戻りが空かどうか。
**対処**: `ArticleExtractor.TARGET` に新しい `id` / `class` を足す。

## 4. 要約 (summarize)

llama-server の OpenAI 互換エンドポイント `/v1/chat/completions` に投げるだけ。
プロンプトは `SUMMARY_SYSTEM` にあり、「本文にあることだけ」「箇条書き禁止」
「必ず『。』で終わる」を指示している。

### Qwen3 系は既定で thinking を吐き、content が空で返る

リクエストに `"chat_template_kwargs": {"enable_thinking": False}` を入れて止めて
いる。これが無いと推論だけ返して `message.content` が空文字になり、要約が
毎回フォールバック (本文の冒頭) に落ちる。**thinking を出すモデルに差し替える
ときは、ここが効くテンプレートかどうかを確認すること。**

### 300 字で切るときは文を壊さない

モデルは指示しても 300 字を超えてくる。単純に `text[:300]` すると
`…MicrosoftはManifes` のように語の途中で切れ、AIradio 側の原稿生成に文の
断片が渡る。`clip_to_sentence()` で最後の「。」まで戻してから返す。

```python
def clip_to_sentence(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    kept, period, _ = head.rpartition("。")
    return kept + period if kept else head
```

上限以下なら手を触れない。「。」が一つも無ければ従来どおり機械的に切る
(要約が無いよりは途中で切れている方がまし)。

**残っている穴**: 上限内に「。」が 1 つしか無く、それが極端に早い位置にある
場合、要約がその 1 文だけに縮む。日本語の要約文では起きにくいので閾値ガードは
入れていない。気になるなら「短くなりすぎたら元の切り方に戻す」を足す。

## 5. スクリーンショット

Playwright の Chromium で `domcontentloaded` 後 2 秒待って 1280x960 を撮る。
`full_page=False` なのは、GIGAZINE の記事が縦に長く、全画面だと表示系の背景に
したとき縮みすぎるため。Playwright の例外型は多いので `except Exception` で
まとめて拾い、`False` を返して取り込みを続ける。

## 6. 問題と対処

### 6.1 要約が本文の冒頭になっている (llama-server が落ちていた)

**症状**: `summary` がちょうど 300 字で、`2026年08月10日 19時00分メモ…` のように
記事ページのヘッダー (日時・カテゴリ) から始まる。実行時の stderr に
`要約に失敗、本文の冒頭で代用します: ...` が出ている。

**原因**: `summarize()` は接続失敗を握りつぶして本文の冒頭を返す。要約が無いと
AIradio 側の原稿生成の入力が消えるため、意図的にそうしてある。

**確認**:

```bash
curl -s -m 3 http://localhost:9931/v1/models   # 落ちていれば exit 7
ss -ltn | grep :9931
```

```bash
# 取り込み済みのうち、文の途中で終わっているもの
./.venv/bin/python -c "
import json, glob
for p in sorted(glob.glob('news/*/*.json')):
    d = json.load(open(p))
    if not d['summary'].endswith('。'): print(p, len(d['summary']))
"
```

**対処**: llama-server を上げてから要約だけ入れ替える。

```bash
./start_llm.sh --bg
uv run --no-sync resummarize.py --truncated-only
```

`gigazine_crawler.py` を再実行しても直らない。既知 URL は `dedupe_days` の
重複判定で飛ばされるため。

### 6.2 llama-server が上がらない

`start_llm.sh --bg` は 120 秒待って応答が無ければログの場所を出して失敗する
(`llm.log`)。プロセスが即死した場合はその場でログの末尾 20 行を出す。

- **モデルが見つからない**: `GCRAWLER_LLM_MODEL` を実在するパスに
- **VRAM が足りない**: 既定のモデルは約 22GB (Q4_K_XL, ctx 16384) を使う。
  `GCRAWLER_LLM_CTX` を減らすか小さいモデルに差し替える
- **ポートが埋まっている**: 応答すれば二重起動しないが、別物が 9931 を握って
  いる場合は `GCRAWLER_LLM_PORT` と `config.toml` の `llm_base_url` を両方変える

### 6.3 同じ記事が二重に入る

`known_urls()` は過去 `dedupe_days` (既定 7) 日ぶんのディレクトリしか見ない。
古い `news/YYYY-MM-DD/` を 7 日より短い周期で消すと、RSS に残っている記事が
「未取得」に戻る。掃除は `dedupe_days` より長い日数で。

日付をまたいで同じ記事が入るのは仕様どおりではないが、`id` (URL の SHA-1 先頭
16 桁) が同じなので、AIradio 側で落とせる。

### 6.4 スクリーンショットが撮れない

`playwright がありません` なら `uv run playwright install chromium`。タイムアウト
(45 秒) やページ側の都合で失敗した場合は `screenshot: null` の JSON が書かれる。
表示系の背景にしか使わないので、取り込み自体は成功扱い。`--no-shot` を付けると
そもそも撮らず、実行はかなり速くなる。

### 6.5 RSS が取れない

ここだけは続行不能で、終了コード 1 で止まる。`rss_url` が変わっていないか、
User-Agent で弾かれていないかを確認する。

## 7. 触るときの注意

- `summarize()` の戻りは必ず `clip_to_sentence()` を通す。3 つある戻り口
  (LLM の出力・出力が空・LLM 失敗時の本文代用) のどれかを素通しにすると、
  文の途中で切れた要約が AIradio に渡る
- `resummarize.py` が書き換えてよいのは `summary` だけ。`id` / `url` /
  `crawled_at` を変えると、AIradio から見て別の記事になる
- 新しい出力項目を足すときは AIradio 側と合わせる (`AIradio/HANDOFF.md` §0)。
  JSON の項目は増やす分には安全だが、消す・改名するのは契約違反
