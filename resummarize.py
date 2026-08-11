#!/usr/bin/env python3
"""取り込み済みの記事の summary を取り直す。

  uv run --no-sync resummarize.py                    # 全部
  uv run --no-sync resummarize.py --truncated-only   # 文の途中で切れたものだけ
  uv run --no-sync resummarize.py --dry-run          # 対象を見るだけ

llama-server が落ちている間に取り込むと、summary が本文の冒頭で代用される
(gigazine_crawler.summarize のフォールバック)。クローラー本体は既知 URL を
再取得しないので、後から直すにはこちらを使う。

本文は記事ページから取り直す。JSON のうち書き換えるのは summary だけで、
id / url / title / screenshot / crawled_at は取り込み時のまま残す
(AIradio 側から見て記事の同一性が変わらないようにする)。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from gigazine_crawler import (
    PROJECT_ROOT,
    clip_to_sentence,
    fetch_body,
    load_config,
    summarize,
)


def is_truncated(record: dict) -> bool:
    """要約が文の途中で終わっている (＝切り出しに失敗している)。"""
    return not record["summary"].endswith("。")


def main() -> int:
    conf = load_config()
    ap = argparse.ArgumentParser(description="取り込み済み記事の要約を取り直す")
    ap.add_argument("--dry-run", action="store_true", help="対象を表示するだけ")
    ap.add_argument(
        "--truncated-only",
        action="store_true",
        help="末尾が「。」でないものだけ",
    )
    ap.add_argument("--news-dir", default=conf["news_dir"], help="対象ディレクトリ")
    args = ap.parse_args()

    news_dir = Path(args.news_dir)
    if not news_dir.is_absolute():
        news_dir = PROJECT_ROOT / news_dir

    targets: list[tuple[Path, dict]] = []
    for path in sorted(news_dir.glob("*/*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"読めないので飛ばします {path}: {e}", file=sys.stderr)
            continue
        if not all(k in record for k in ("url", "title", "summary")):
            print(f"項目が足りないので飛ばします {path}", file=sys.stderr)
            continue
        if args.truncated_only and not is_truncated(record):
            continue
        targets.append((path, record))

    print(f"対象: {len(targets)} 件")
    if args.dry_run:
        for path, record in targets:
            print(f"  {path.relative_to(news_dir)}  {record['title'][:40]}")
        return 0

    max_chars = conf["summary_max_chars"]
    interval = float(conf["request_interval_sec"])
    updated = failed = 0

    for path, record in targets:
        print(f"\n{path.relative_to(news_dir)}  {record['title'][:50]}")
        body = fetch_body(record["url"], conf)
        time.sleep(interval)
        if not body:
            print("  本文が取れないので飛ばします", file=sys.stderr)
            failed += 1
            continue

        summary = summarize(record["title"], body, conf)
        if summary == clip_to_sentence(body, max_chars):
            # summarize() のフォールバックと同じ値。LLM が返らなかったので、
            # 上書きしても今より良くならない
            print("  要約できなかったので飛ばします", file=sys.stderr)
            failed += 1
            continue

        before = record["summary"]
        record["summary"] = summary
        # 本体と同じく書き切ってから差し替える。AIradio が読んでいる最中でも
        # 壊れたファイルは見えない
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
        updated += 1
        print(f"  旧({len(before)}字): {before[:60]}...")
        print(f"  新({len(summary)}字): {summary}")

    print(f"\n完了: 更新 {updated} 件 / 失敗 {failed} 件")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
