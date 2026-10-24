#!/usr/bin/env python3
"""out/ の出力(wav と meta.json)から、公開用のサイト(mp3・Podcastフィード・index.html)を作る。

標準ライブラリと ffmpeg だけで動きます。GitHub Actions から呼ばれる想定です。

必要な環境変数:
  FEED_TOKEN         推測されにくい文字列(英数字・ハイフン・アンダースコア、16文字以上)。公開URLの一部になる
  GITHUB_REPOSITORY  「ユーザー名/リポジトリ名」(GitHub Actionsが自動で設定)。--base-url で上書き可

使い方:
  python build_site.py --out out --site site
"""
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import wave
import zlib
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

TITLE = "ファブリツィオ・ずんだもん"


def die(msg):
    print(f"エラー: {msg}")
    sys.exit(1)


def make_cover_png(path, size=1400):
    """無地の背景に丸を描いただけの、簡単なカバー画像(PNG)を作る"""
    bg, fg = bytes((24, 110, 62)), bytes((235, 245, 238))
    cx = cy = size // 2
    r = int(size * 0.28)
    rows = []
    for y in range(size):
        dy = y - cy
        if abs(dy) <= r:
            dx = int((r * r - dy * dy) ** 0.5)
            row = bg * (cx - dx) + fg * (2 * dx) + bg * (size - cx - dx)
        else:
            row = bg * size
        rows.append(b"\x00" + row)
    raw = b"".join(rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    Path(path).write_bytes(png)


def hms(seconds):
    s = int(round(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def main():
    ap = argparse.ArgumentParser(description="公開用サイト(mp3・Podcastフィード)を作る")
    ap.add_argument("--out", default="out", help="soccer_radio_demo.py の出力フォルダ")
    ap.add_argument("--site", default="site", help="公開用サイトの出力フォルダ")
    ap.add_argument("--base-url", help="公開サイトのURL(省略時は GITHUB_REPOSITORY から作る)")
    ap.add_argument("--bitrate", default="64k", help="mp3のビットレート")
    args = ap.parse_args()

    # --- 入力の確認
    out = Path(args.out)
    meta_path = out / "meta.json"
    if not meta_path.exists():
        die(f"{meta_path} がありません。ニュースの生成(soccer_radio_demo.py)が完了していません")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not meta.get("audio"):
        die("音声が作られていません(VOICEVOXでの音声化に失敗した可能性があります)。公開は行いません")
    wav_path = out / meta["audio"]
    if not wav_path.exists():
        die(f"{wav_path} がありません")

    token = os.environ.get("FEED_TOKEN", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,}", token):
        die("FEED_TOKEN が未設定、または形式が正しくありません(英数字・ハイフン・アンダースコアで16文字以上)。"
            "GitHubの Settings → Secrets and variables → Actions に FEED_TOKEN を登録してください")

    base = args.base_url
    if not base:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if "/" not in repo:
            die("GITHUB_REPOSITORY が分かりません。--base-url を指定してください")
        owner, name = repo.split("/", 1)
        base = f"https://{owner.lower()}.github.io/{name}/"
    base = base.rstrip("/") + "/"
    root = f"{base}{token}/"

    # --- サイトを作り直す(古い音声は残さず、最新の1本だけ)
    site = Path(args.site)
    if site.exists():
        shutil.rmtree(site)
    dest = site / token
    dest.mkdir(parents=True)

    stamp = meta["stamp"]
    mp3_name = f"news_{stamp}.mp3"
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path), "-vn", "-ac", "1", "-ar", "44100",
         "-codec:a", "libmp3lame", "-b:a", args.bitrate, str(dest / mp3_name)],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not (dest / mp3_name).exists():
        die(f"mp3への変換に失敗しました: {r.stderr[-300:]}")
    size_bytes = (dest / mp3_name).stat().st_size
    with wave.open(str(wav_path)) as w:
        duration = hms(w.getnframes() / w.getframerate())

    # カバー画像: リポジトリ直下に cover.png / cover.jpg があればそれを使い、なければ自動で作った画像を使う。
    # アプリが古い画像を覚えていて更新されないことがあるので、ファイル名に中身の目印(ハッシュ)を入れる
    custom = next((Path(n) for n in ("cover.png", "cover.jpg", "cover.jpeg") if Path(n).is_file()), None)
    if custom:
        data = custom.read_bytes()
        cover_name = f"cover_{hashlib.sha1(data).hexdigest()[:8]}{custom.suffix.lower()}"
        (dest / cover_name).write_bytes(data)
        print(f"カバー画像: {custom} を使います({len(data) // 1024}KB)")
    else:
        cover_name = "cover.png"
        make_cover_png(dest / cover_name)

    # --- Podcastのフィード
    d = datetime.strptime(meta["date"], "%Y-%m-%d")
    ep_title = f"{d.month}月{d.day}日のサッカーニュース"
    lines = []
    for i, h in enumerate(meta["headlines"], 1):
        lines.append(f"{i}. {h['title']}({' / '.join(h['sources'])})")
    lines.append("")
    lines.append(f"音声: {meta['credit']}")
    desc = "\n".join(lines)
    e = html.escape
    pub = format_datetime(datetime.now(timezone.utc))
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>{e(TITLE)}</title>
  <link>{e(root)}</link>
  <atom:link href="{e(root)}feed.xml" rel="self" type="application/rss+xml"/>
  <language>ja</language>
  <description>{e(TITLE)}(個人用。見出しの読み上げ)</description>
  <itunes:author>個人用</itunes:author>
  <itunes:explicit>false</itunes:explicit>
  <itunes:block>Yes</itunes:block>
  <itunes:category text="Sports"/>
  <itunes:image href="{e(root)}{e(cover_name)}"/>
  <item>
    <title>{e(ep_title)}</title>
    <description>{e(desc)}</description>
    <pubDate>{pub}</pubDate>
    <guid isPermaLink="false">soccer-news-{e(stamp)}</guid>
    <enclosure url="{e(root + mp3_name)}" length="{size_bytes}" type="audio/mpeg"/>
    <itunes:duration>{duration}</itunes:duration>
    <itunes:explicit>false</itunes:explicit>
  </item>
</channel>
</rss>
"""
    (dest / "feed.xml").write_text(feed, encoding="utf-8")

    # --- 確認用のページ(検索に載らないようにする)
    page = (out / "index.html").read_text(encoding="utf-8")
    page = page.replace(meta["audio"], mp3_name)
    page = page.replace("<head>", '<head>\n<meta name="robots" content="noindex,nofollow">', 1)
    (dest / "index.html").write_text(page, encoding="utf-8")

    print(f"サイトを作りました: {size_bytes / 1e6:.1f}MB、長さ {duration}、見出し {len(meta['headlines'])}件")
    print(f"フィードのURL: {base}<FEED_TOKEN>/feed.xml  (トークン部分は表示しません)")


if __name__ == "__main__":
    main()
