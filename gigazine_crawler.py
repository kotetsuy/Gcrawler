#!/usr/bin/env python3
"""GIGAZINE の新着記事を取り込んで news/YYYY-MM-DD/ に置く。

  uv run --no-sync gigazine_crawler.py               # 通常実行 (1日1回想定)
  uv run --no-sync gigazine_crawler.py --limit 3     # 3件だけ
  uv run --no-sync gigazine_crawler.py --no-shot     # スクショを撮らない
  uv run --no-sync gigazine_crawler.py --dry-run     # 取得予定だけ表示

AIradio とは **ファイル契約のみ** で繋がっている (AIradio/HANDOFF.md §0)。
出力はこの 2 つだけ。

  news/YYYY-MM-DD/NNNN.json  { id, url, title, summary, screenshot, crawled_at }
  news/YYYY-MM-DD/NNNN.png   記事ページのスクリーンショット (表示系の背景用)

HTML スクレイピングではなく RSS を起点にしている。GIGAZINE は RSS を出して
いるので、一覧の取得はそちらの方が壊れにくく、相手にも礼儀正しい。本文は
記事ページから取るが、そこはレイアウト変更で壊れうるので、失敗したら RSS の
description に落として取り込みは続ける。

毎日起動される保証はないので、取り込み時に過去数日ぶんの JSON から既知 URL
集合を作って重複をはじく。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"

DEFAULTS = {
    "rss_url": "https://gigazine.net/news/rss_2.0/",
    "news_dir": "news",
    # 相手のサーバーに負担をかけない。1 リクエストごとにこれだけ空ける
    "request_interval_sec": 1.5,
    # 重複判定にさかのぼる日数。AIradio が読む日数 (既定 3) 以上にしておく
    "dedupe_days": 7,
    # 1 回の実行で取り込む上限
    "limit": 10,
    "user_agent": "Gcrawler/0.1 (personal AI radio; contact: CHANGE_ME@example.com)",
    "llm_base_url": "http://localhost:8080",
    "llm_max_tokens": 512,
    "llm_temperature": 0.5,
    # 要約の文字数。AIradio 側の原稿生成が入力に使う
    "summary_max_chars": 300,
    # LLM に渡す本文の長さ。長すぎると遅く、短すぎると要約が薄くなる
    "body_max_chars": 3000,
    "screenshot_width": 1280,
    "screenshot_height": 960,
}

SUMMARY_SYSTEM = """あなたはニュース記事を要約する編集者です。

# ルール
1. 記事本文に書かれていることだけを使う。推測や補足を足さない
2. 事実関係(何が・誰が・どうなった)を優先し、感想や評価は書かない
3. {max_chars}文字以内の日本語で、続けて読める文章にする
4. 箇条書きや記号は使わない
5. 必ず「。」で終わる完結した文にする

# 出力形式
要約本文のみを出力する。前置きや説明は不要。"""


# ---- 設定 ----------------------------------------------------------------


def load_config() -> dict:
    """config.toml があれば DEFAULTS に上書きして返す。無くても動く。"""
    conf = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            conf.update(tomllib.load(f))
    return conf


# ---- RSS ------------------------------------------------------------------


@dataclass
class FeedItem:
    title: str
    url: str
    description: str


def fetch_rss(conf: dict) -> list[FeedItem]:
    r = httpx.get(
        conf["rss_url"],
        headers={"User-Agent": conf["user_agent"]},
        timeout=30.0,
        follow_redirects=True,
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)

    items: list[FeedItem] = []
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        url = (node.findtext("link") or "").strip()
        desc = strip_tags(node.findtext("description") or "")
        if title and url:
            items.append(FeedItem(title=title, url=url, description=desc))
    return items


# ---- 本文抽出 --------------------------------------------------------------

_WS = re.compile(r"[ \t　]+")
_BLANK_LINES = re.compile(r"\n{2,}")


def strip_tags(html: str) -> str:
    """タグを落として空白を整える (RSS の description 用)。"""
    text = re.sub(r"<[^>]+>", " ", html)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    return _WS.sub(" ", text).strip()


class ArticleExtractor(HTMLParser):
    """<div id="article"> の中の地の文だけを集める。

    GIGAZINE の記事本文はこの div に入っている。見つからなければ何も
    集めないので、呼び出し側は RSS の description に落ちること。
    レイアウト変更で壊れても取り込み自体は止めない、という切り分け。
    """

    TARGET = ("article", "content", "cntnts")
    SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._depth = 0  # 対象 div の内側にいるときの入れ子の深さ
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP_TAGS:
            self._skip += 1
            return
        if self._depth:
            if tag == "div":
                self._depth += 1
            return
        if tag != "div":
            return
        ident = dict(attrs).get("id", "") or ""
        cls = dict(attrs).get("class", "") or ""
        if any(k in ident or k in cls for k in self.TARGET):
            self._depth = 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._depth and tag == "div":
            self._depth -= 1
        if self._depth and tag in ("p", "h2", "h3", "li"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._depth and not self._skip:
            text = _WS.sub(" ", data)
            if text.strip():
                self.parts.append(text)

    def text(self) -> str:
        joined = "".join(self.parts)
        return _BLANK_LINES.sub("\n", joined).strip()


def fetch_body(url: str, conf: dict) -> str:
    """記事本文。取れなければ空文字 (呼び出し側が description に落とす)。"""
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": conf["user_agent"]},
            timeout=30.0,
            follow_redirects=True,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        print(f"  本文の取得に失敗: {e}", file=sys.stderr)
        return ""

    extractor = ArticleExtractor()
    extractor.feed(r.text)
    return extractor.text()[: conf["body_max_chars"]]


# ---- 要約 ------------------------------------------------------------------


def clip_to_sentence(text: str, max_chars: int) -> str:
    """max_chars に収める。切るときは最後の「。」まで戻す。

    AIradio 側の原稿生成に文の断片が渡らないようにする。「。」が一つも
    無ければそのまま切る (要約が無いよりは途中で切れている方がまし)。
    """
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    kept, period, _ = head.rpartition("。")
    return kept + period if kept else head


def summarize(title: str, body: str, conf: dict) -> str:
    """llama-server で要約する。失敗したら本文の頭を切って返す。

    要約が無いと AIradio 側の原稿生成の入力が無くなるので、LLM が落ちていても
    「何かしら summary がある」状態にはしておく。
    """
    max_chars = conf["summary_max_chars"]
    payload = {
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM.format(max_chars=max_chars)},
            {"role": "user", "content": f"タイトル: {title}\n\n本文:\n{body}"},
        ],
        "temperature": conf["llm_temperature"],
        "max_tokens": conf["llm_max_tokens"],
        "stream": False,
        # Qwen3 系は既定で thinking を吐き、content が空で返る
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        r = httpx.post(
            f"{conf['llm_base_url']}/v1/chat/completions", json=payload, timeout=180.0
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        print(f"  要約に失敗、本文の冒頭で代用します: {e}", file=sys.stderr)
        return clip_to_sentence(body, max_chars)

    text = re.sub(r"\s+", " ", text).strip()
    return clip_to_sentence(text or body, max_chars)


# ---- スクリーンショット ----------------------------------------------------


def take_screenshot(url: str, dst: Path, conf: dict) -> bool:
    """Playwright で記事ページを撮る。撮れなければ False。

    表示系の背景にしか使わないので、失敗しても取り込みは続ける
    (screenshot が無い JSON を AIradio 側は許容する)。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "  playwright がありません "
            "(uv pip install playwright && playwright install chromium)",
            file=sys.stderr,
        )
        return False

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(
                viewport={
                    "width": conf["screenshot_width"],
                    "height": conf["screenshot_height"],
                },
                user_agent=conf["user_agent"],
            )
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            # 遅延読み込みの画像が入るまで少し待つ。全画面は撮らない
            # (GIGAZINE の記事は縦に長く、背景にすると縮みすぎる)
            page.wait_for_timeout(2000)
            dst.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(dst), full_page=False)
            browser.close()
    except Exception as e:  # Playwright の例外型は多いのでまとめて拾う
        print(f"  スクリーンショットに失敗: {e}", file=sys.stderr)
        return False
    return True


# ---- 保存 ------------------------------------------------------------------


def article_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def known_urls(news_dir: Path, days: int) -> set[str]:
    """過去 N 日ぶんの JSON から既知 URL を集める。

    毎日実行される保証がないので、RSS に残っている古い記事を二重に
    取り込まないための保険。
    """
    urls: set[str] = set()
    today = date.today()
    for i in range(days):
        day_dir = news_dir / (today - timedelta(days=i)).isoformat()
        if not day_dir.is_dir():
            continue
        for path in day_dir.glob("*.json"):
            try:
                urls.add(json.loads(path.read_text(encoding="utf-8"))["url"])
            except (OSError, json.JSONDecodeError, KeyError):
                continue
    return urls


def next_index(day_dir: Path) -> int:
    """その日のディレクトリでの次の連番。追記実行に対応する。"""
    used = [
        int(p.stem) for p in day_dir.glob("*.json") if p.stem.isdigit()
    ] if day_dir.is_dir() else []
    return max(used, default=0) + 1


def save_article(
    day_dir: Path, index: int, item: FeedItem, summary: str, shot: bool
) -> Path:
    day_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{index:04d}"
    record = {
        "id": article_id(item.url),
        "url": item.url,
        "title": item.title,
        "summary": summary,
        "screenshot": f"{stem}.png" if shot else None,
        "crawled_at": datetime.now().isoformat(timespec="seconds"),
    }
    path = day_dir / f"{stem}.json"
    # 書き切ってから差し替える。AIradio が読んでいる最中でも壊れて見えない
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


# ---- main ------------------------------------------------------------------


def main() -> int:
    conf = load_config()
    ap = argparse.ArgumentParser(description="GIGAZINE の新着記事を取り込む")
    ap.add_argument("--limit", type=int, default=conf["limit"], help="取り込む件数")
    ap.add_argument("--no-shot", action="store_true", help="スクショを撮らない")
    ap.add_argument("--dry-run", action="store_true", help="取得予定だけ表示する")
    ap.add_argument("--news-dir", default=conf["news_dir"], help="出力先")
    args = ap.parse_args()

    news_dir = Path(args.news_dir)
    if not news_dir.is_absolute():
        news_dir = PROJECT_ROOT / news_dir

    try:
        feed = fetch_rss(conf)
    except (httpx.HTTPError, ET.ParseError) as e:
        print(f"RSS を取得できません: {e}", file=sys.stderr)
        return 1
    print(f"RSS: {len(feed)} 件")

    seen = known_urls(news_dir, conf["dedupe_days"])
    fresh = [item for item in feed if item.url not in seen][: args.limit]
    print(f"既知 {len(seen)} 件 → 新規 {len(fresh)} 件を取り込みます")

    if args.dry_run:
        for item in fresh:
            print(f"  {item.title}\n    {item.url}")
        return 0
    if not fresh:
        return 0

    day_dir = news_dir / date.today().isoformat()
    index = next_index(day_dir)
    interval = float(conf["request_interval_sec"])

    for item in fresh:
        print(f"\n[{index:04d}] {item.title}")
        body = fetch_body(item.url, conf)
        time.sleep(interval)
        if not body:
            # レイアウト変更等。RSS の description で代用する
            body = item.description
        summary = summarize(item.title, body, conf)

        shot = False
        if not args.no_shot:
            shot = take_screenshot(item.url, day_dir / f"{index:04d}.png", conf)
            time.sleep(interval)

        path = save_article(day_dir, index, item, summary, shot)
        print(f"  → {path} ({len(summary)}文字{'・スクショあり' if shot else ''})")
        index += 1

    print(f"\n完了: {day_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
