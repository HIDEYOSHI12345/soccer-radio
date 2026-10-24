#!/usr/bin/env python3
"""サッカーニュース読み上げ デモ (Tier 0: AI要約なし / 完全無料)

RSS取得 → サッカー記事の絞り込み → 重複整理 → ラジオ原稿 → VOICEVOXで音声化 → HTMLプレーヤー

標準ライブラリのみ。追加インストール不要(Python 3.9以上)。
音声化にはVOICEVOXエンジンを起動しておく必要があります(--no-audio なら不要)。

使い方:
  python soccer_radio_demo.py --no-audio       # 原稿とHTMLだけ作る(VOICEVOX不要)
  python soccer_radio_demo.py --list-speakers  # 話者IDの一覧
  python soccer_radio_demo.py                  # 音声まで作る
  python soccer_radio_demo.py --speaker 13     # 話者を変える
"""
import argparse
import html
import io
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import wave
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# ---------------------------------------------------------------- 設定
# url: フィードURL(確認済み) / site: トップページ(RSSを自動検出する。未確認のもの)
# soccer_only: Trueなら絞り込みをしない(サッカー専門サイト)
FEEDS = [
    {"name": "Yahoo!ニュース スポーツ", "url": "https://news.yahoo.co.jp/rss/topics/sports.xml", "soccer_only": False},
    {"name": "ゲキサカ", "site": "https://web.gekisaka.jp/", "soccer_only": True},
    {"name": "サッカーキング", "site": "https://www.soccer-king.jp/", "soccer_only": True},
    {"name": "フットボールチャンネル", "site": "https://www.footballchannel.jp/", "soccer_only": True},
    {"name": "Goal(日本版)", "site": "https://www.goal.com/jp", "soccer_only": True},
    {"name": "JFA", "site": "https://www.jfa.jp/", "soccer_only": True},
    {"name": "Jリーグ", "site": "https://www.jleague.jp/", "soccer_only": True},
    # 英語(見出しだけをClaude APIで日本語にして読み上げる。APIキーがなければ自動で飛ばす)
    {"name": "BBC Sport Football", "url": "http://feeds.bbci.co.uk/sport/football/rss.xml", "soccer_only": True, "lang": "en"},
    {"name": "Sky Sports", "url": "https://www.skysports.com/rss/0,20514,11095,00.xml", "soccer_only": False, "lang": "en"},
    {"name": "The Guardian Football", "url": "https://www.theguardian.com/football/rss", "soccer_only": True, "lang": "en"},
]
EN_PER_FEED = 8  # 英語は1フィードあたり最大何本の見出しを翻訳するか(費用を抑えるため)
EN_KEYWORDS = [
    "football", "soccer", "premier league", "champions league", "europa league", "la liga", "serie a",
    "bundesliga", "ligue 1", "transfer", "world cup", "fa cup", "carabao", "manchester", "liverpool",
    "arsenal", "chelsea", "tottenham", "real madrid", "barcelona", "bayern", "juventus", "psg",
]
EN_EXCLUDE = ["cricket", "rugby", "tennis", "golf", "f1", "formula 1", "boxing", "nfl", "nba", "darts", "snooker"]
CLAUDE_MODEL = "claude-haiku-5-5"

# X(旧Twitter)のアカウント。X_BEARER_TOKEN が設定されているときだけ使う(従量課金: 1件あたり約0.005ドル)
X_ACCOUNTS = [("FabrizioRomano", "")]  # (ユーザー名, 読み上げの頭に付ける呼び名。空なら付けない。例: "ロマーノ氏")
X_FETCH = 10      # 1アカウントから取得する投稿数(5〜100。取得した件数ぶん課金される)
X_MAX_ITEMS = 3   # 1アカウントから読み上げに使う最大件数(Xだけで10件が埋まらないようにする)
X_SKIP_RE = re.compile(r"#ad\b|sponsored|promo code|use code|link in bio|subscribe|giveaway", re.I)

# サッカー以外も混ざるフィード用の絞り込みワード(必要に応じて足してください)
KEYWORDS = [
    "サッカー", "Jリーグ", "J1", "J2", "J3", "日本代表", "なでしこ", "WEリーグ",
    "ワールドカップ", "W杯", "プレミアリーグ", "ラ・リーガ", "ラリーガ", "ブンデスリーガ",
    "セリエA", "リーグ・アン", "チャンピオンズリーグ", "ヨーロッパリーグ", "ACL", "天皇杯", "ルヴァン",
]
EXCLUDE = ["野球", "NPB", "MLB", "バスケ", "ラグビー", "バレー", "大相撲", "ゴルフ", "テニス"]

# 読み上げない見出し(見出し=タイトルにこれらが含まれていたら除く)。足したい言葉があればここに足してください
BLOCK_TITLE = [
    "ライブ", "試合記録",                                   # 試合の実況・記録ページ(読み上げても中身が伝わらない)
    "Jリーグ", "Ｊリーグ", "J1", "J2", "J3", "Ｊ１", "Ｊ２", "Ｊ３", "明治安田",   # Jリーグ関連
    "選手権", "高校サッカー",                                # 高校選手権など
]
# 日本語の記事は、見出しか概要に次の言葉のどれかが入っているものだけを読み上げる(欧州の話題と日本代表)。
# 足りない言葉(クラブ名・海外組の選手名など)は、ここに足してください。--all-topics を付けると、この絞り込みをしません。
KEEP_JA = [
    # 日本代表
    "日本代表", "森保", "サムライブルー",
    # 欧州の大会・リーグ
    "欧州", "ヨーロッパ", "UEFA", "プレミア", "ラ・リーガ", "ラリーガ", "ブンデス", "セリエ", "リーグ・アン", "リーグアン",
    "チャンピオンズリーグ", "カンファレンスリーグ", "FAカップ", "カラバオ", "コパ・デル・レイ", "ドイツ杯", "DFB", "コッパ・イタリア", "ネーションズリーグ",
    # 欧州のクラブ
    "アーセナル", "チェルシー", "リバプール", "リヴァプール", "マンチェスター", "マンC", "マンU", "トッテナム", "ニューカッスル", "アストン・ビラ",
    "ウェストハム", "ブライトン", "クリスタル・パレス", "ブレントフォード", "フラム", "エバートン", "ノッティンガム", "リーズ", "ウォルバーハンプトン",
    "レアル", "バルセロナ", "バルサ", "アトレチコ", "ビジャレアル", "セビージャ", "バレンシア", "ベティス", "マジョルカ", "ヘタフェ", "ジローナ",
    "バイエルン", "ドルトムント", "レバークーゼン", "フランクフルト", "シュツットガルト", "ライプツィヒ", "ボルシア", "フライブルク", "ホッフェンハイム",
    "マインツ", "ウニオン・ベルリン", "ヴォルフスブルク", "ブレーメン", "アウクスブルク",
    "ユベントス", "ミラン", "インテル", "ナポリ", "ローマ", "ラツィオ", "アタランタ", "フィオレンティーナ",
    "パリ・サンジェルマン", "PSG", "モナコ", "マルセイユ", "リヨン", "リール", "スタッド・ランス",
    "セルティック", "レンジャーズ", "フェイエノールト", "アヤックス", "PSV", "スポルティング", "ポルト", "ベンフィカ", "クラブ・ブルッヘ",
    # 欧州でプレーする日本人選手
    "久保建英", "三笘薫", "冨安健洋", "遠藤航", "堂安律", "鎌田大地", "伊藤洋輝", "守田英正", "南野拓実", "上田綺世", "前田大然", "旗手怜央",
    "菅原由勢", "板倉滉", "町野修斗", "中村敬斗", "鈴木彩艶", "浅野拓磨", "佐野海舟", "瀬古歩夢", "渡辺剛", "橋岡大樹", "田中碧", "鈴木唯人",
    "塩貝健人", "佐野航大", "藤田譲瑠チマ", "町田浩樹", "谷川萌々子", "熊谷紗希",
    # 見出しで名字だけが使われることが多い選手(同じ名字の他の選手を拾うことがあります)
    "三笘", "冨安", "堂安", "鎌田", "守田", "南野",
]
# 英語の実況ページ(翻訳する前に除く)
BLOCK_TITLE_EN_RE = re.compile(r"\blive\b|clockwatch|as it happened|minute[- ]by[- ]minute", re.I)

UA = "Mozilla/5.0 (compatible; SoccerRadioDemo/0.1; personal use)"
SIMILARITY = 0.4  # タイトルの似ている度合い(0〜1)。これ以上なら同じニュースとして1本にまとめる


# ---------------------------------------------------------------- 取得
INSECURE = False  # --insecure を付けたときだけ True(RSS取得のみ。証明書の検証を省略する)


def _ssl_contexts():
    """通常の検証 → certifi(入っていれば) → --insecure のときだけ検証なし、の順に試す"""
    yield None
    try:
        import certifi
        yield ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    if INSECURE:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        yield ctx


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last = None
    for ctx in _ssl_contexts():
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLError):
                last = e
                continue
            raise
    raise RuntimeError(
        f"証明書の検証に失敗しました({last.reason})。 `pip install certifi` を実行して再実行するか、"
        "それでもだめなら --insecure を付けてください"
    )


# トップページにRSSの記述がないサイト向けに、よくあるURLも試す
COMMON_PATHS = ["/feed/", "/feed", "/rss/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml", "/news/feed/"]


def looks_like_feed(data):
    head = data[:3000].lower()
    return b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head


def discover_feed(site):
    """① トップページの <link rel="alternate" type="application/rss+xml"> → ② よくあるURLを順に試す"""
    try:
        page = http_get(site).decode("utf-8", errors="ignore")
    except RuntimeError:
        raise  # 証明書エラーはここで知らせる
    except Exception:
        page = ""
    for m in re.finditer(r"<link\b[^>]*>", page, re.I):
        tag = m.group(0)
        if re.search(r'type=["\']application/(rss|atom)\+xml', tag, re.I):
            h = re.search(r'href=["\']([^"\']+)', tag, re.I)
            if h:
                return urllib.parse.urljoin(site, html.unescape(h.group(1)))
    u = urllib.parse.urlparse(site)
    root = f"{u.scheme}://{u.netloc}"
    cands = [root + p for p in COMMON_PATHS]
    if u.path.strip("/"):
        cands += [site.rstrip("/") + p for p in COMMON_PATHS]
    for c in cands:
        try:
            if looks_like_feed(http_get(c, timeout=8)):
                return c
        except RuntimeError:
            raise
        except Exception:
            continue
    return None


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(el, *names):
    for n in names:
        for c in el:
            if local(c.tag) == n and (c.text or "").strip():
                return c.text
    return ""


def child_link(el):
    for c in el:
        if local(c.tag) == "link":
            if c.get("href"):
                return c.get("href")
            if (c.text or "").strip():
                return c.text.strip()
    return ""


def strip_tags(t):
    t = html.unescape(re.sub(r"<[^>]+>", "", t or ""))
    return re.sub(r"\s+", " ", t).strip()


def parse_date(s):
    if not s:
        return None
    d = None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except Exception:
            return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def parse_feed(data, source):
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title = strip_tags(child_text(el, "title"))
        if not title:
            continue
        items.append({
            "title": title,
            "link": child_link(el),
            "summary": strip_tags(child_text(el, "description", "summary", "content")),
            "date": parse_date(child_text(el, "pubDate", "date", "updated", "published")),
            "source": source,
        })
    return items


# ---------------------------------------------------------------- X(旧Twitter)
def x_get(token, path, params):
    url = os.environ.get("X_BASE_URL", "https://api.x.com") + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "User-Agent": "soccer-radio"})
    last = None
    for ctx in _verified_contexts():
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")[:200]
            hint = {401: "トークンが正しくありません", 402: "クレジット(残高)が足りません", 403: "権限がありません",
                    429: "回数制限に達しました"}.get(e.code, "")
            raise RuntimeError(f"X APIがエラーを返しました(HTTP {e.code} {hint}): {detail}")
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLError):
                last = e
                continue
            raise
    raise RuntimeError(f"証明書の検証に失敗しました({last.reason})")


def clean_post(text):
    t = re.sub(r"https?://\S+", " ", text)
    t = re.sub(r"#\w+", " ", t)
    t = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F\u200d]", " ", t)  # 絵文字
    return re.sub(r"\s+", " ", t).strip()


def fetch_x_posts(token, username, label, since, limit=X_FETCH, max_items=X_MAX_ITEMS):
    """アカウントの、本人の元の投稿(リツイート・返信を除く)を、見出し用の形にして返す"""
    u = x_get(token, "/2/users/by/username/" + urllib.parse.quote(username), {})
    uid = (u.get("data") or {}).get("id")
    if not uid:
        raise RuntimeError(f"@{username} のユーザーIDを取得できませんでした: {str(u)[:150]}")
    r = x_get(token, f"/2/users/{uid}/tweets", {
        "max_results": max(5, min(100, limit)),
        "exclude": "retweets,replies",
        "tweet.fields": "created_at",
        "start_time": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    items = []
    for tw in r.get("data") or []:
        text = clean_post(tw.get("text", ""))
        if len(text) < 15 or X_SKIP_RE.search(text):
            continue
        try:
            d = datetime.fromisoformat(tw["created_at"].replace("Z", "+00:00"))
        except Exception:
            d = None
        items.append({
            "title": text, "link": f"https://x.com/{username}/status/{tw['id']}", "summary": "",
            "date": d, "source": f"X @{username}", "x_label": label,
        })
    return items[:max_items]


# ---------------------------------------------------------------- 英語見出しの翻訳(Claude API)
def _verified_contexts():
    """APIキーを送るので、証明書の検証は省略しない(--insecure は使わない)"""
    yield None
    try:
        import certifi
        yield ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass


def call_claude(api_key, model, prompt, max_tokens=8000):
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages",
        data=body, method="POST",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    last = None
    for ctx in _verified_contexts():
        try:
            with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
                raw = r.read()
            try:
                data = json.loads(raw)
            except Exception:
                raise RuntimeError(f"Claude APIの応答を読み取れませんでした: {raw[:200]!r}")
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            if not text.strip():
                kinds = [b.get("type") for b in data.get("content", [])]
                raise RuntimeError(f"Claude APIの応答に文章がありませんでした(stop_reason={data.get('stop_reason')}, 内容={kinds})")
            return text
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")[:300]
            raise RuntimeError(f"Claude APIがエラーを返しました(HTTP {e.code}): {detail}")
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLError):
                last = e
                continue
            raise
    raise RuntimeError(f"証明書の検証に失敗しました({last.reason})。 `pip install certifi` を実行して再実行してください")


def translate_titles(api_key, model, titles):
    """英語の見出しのリストを、日本語の見出しのリストにして返す(件数が合わなければ例外)"""
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    prompt = (
        "次はサッカーニュースの英語の見出しです。ラジオで読み上げる日本語の見出しに訳してください。\n"
        "ルール:\n"
        "- 見出しに書いてあることだけを訳し、書かれていない情報は足さない。\n"
        "- 「〜か」「〜と報じられている」など、噂や不確かさの表現は、日本語でも残す(断定にしない)。\n"
        "- 選手名・クラブ名・監督名は、日本のサッカーメディアで一般的なカタカナ表記にする。\n"
        "- 1本あたり40文字程度までの、読み上げやすい短い文にする。\n"
        f"- 出力は、入力と同じ番号をつけた{len(titles)}行だけ(例: 「1. 訳した見出し」)。説明、前置き、英語の原文は書かない。\n\n"
        + numbered
    )
    text = call_claude(api_key, model, prompt)
    found = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\s*[\.\)．、:]\s*(.+?)\s*$", line)
        if m:
            found[int(m.group(1))] = m.group(2)
    if sorted(found) != list(range(1, len(titles) + 1)):
        raise RuntimeError(f"翻訳結果の件数が合いませんでした(期待{len(titles)}本、取得{len(found)}本)。応答の先頭: {text[:150]!r}")
    return [found[i + 1] for i in range(len(titles))]


# ---------------------------------------------------------------- 絞り込み・重複整理
def is_soccer(it):
    t = it["title"] + " " + it["summary"]
    if any(k in t for k in EXCLUDE):
        return False
    return any(k in t for k in KEYWORDS)


def is_europe_or_japan(it):
    t = it["title"] + " " + it["summary"]
    return any(k in t for k in KEEP_JA)


def blocked(it, en=False):
    t = it["title"]
    if en:
        return bool(BLOCK_TITLE_EN_RE.search(t))
    return any(k in t for k in BLOCK_TITLE)


def is_soccer_en(it):
    t = (it["title"] + " " + it["summary"]).lower()
    if any(k in t for k in EN_EXCLUDE):
        return False
    return any(k in t for k in EN_KEYWORDS)


def norm(t):
    return re.sub(r"[\s\W_]+", "", t.lower())


def bigrams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similar(a, b):
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return False
    return len(A & B) / len(A | B) >= SIMILARITY


def cluster(items):
    """似たタイトルを1本にまとめる。報じているメディアが多い順、新しい順に並べる"""
    items = sorted(items, key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    clusters = []
    for it in items:
        key = norm(it["title"])
        for c in clusters:
            if similar(key, c["key"]):
                c["sources"].add(it["source"])
                if not c["summary"] and it["summary"]:
                    c["summary"] = it["summary"]
                break
        else:
            clusters.append({**it, "key": key, "sources": {it["source"]}})
    clusters.sort(key=lambda c: len(c["sources"]), reverse=True)  # 安定ソート: 同数なら新しい順のまま
    return clusters


# ---------------------------------------------------------------- 原稿
def speakable(t):
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[【】\[\]\(\)]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def shorten(t, n=90):
    if len(t) <= n:
        return t
    cut = t[:n]
    p = cut.rfind("。")
    return cut[:p + 1] if p > 20 else cut + "…"


def with_period(t):
    return t if t.endswith(("。", "!", "?", "!", "?", "…")) else t + "。"


def build_script(clusters, now):
    wd = "月火水木金土日"[now.weekday()]
    lines = [f"おはようございます。{now.month}月{now.day}日 {wd}曜日のサッカーニュースです。{len(clusters)}件お伝えします。"]
    for i, c in enumerate(clusters, 1):
        s = f"{i}件目。" + with_period(speakable(c["title"]))
        if c["summary"] and c["summary"][:15] != c["title"][:15]:
            s += with_period(speakable(shorten(c["summary"])))
        if len(c["sources"]) > 1:
            s += f"{len(c['sources'])}つのメディアが報じています。"
        lines.append(s)
    lines.append("以上、サッカーニュースでした。")
    return lines


# ---------------------------------------------------------------- VOICEVOX
def vv_post(base, path, params=None, body=b""):
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def synth(base, text, speaker, speed=1.0):
    q = json.loads(vv_post(base, "/audio_query", {"text": text, "speaker": speaker}))
    q["speedScale"] = speed  # 話す速さ(1.0が標準)
    return vv_post(base, "/synthesis", {"speaker": speaker}, body=json.dumps(q).encode("utf-8"))


def list_speakers(base):
    return json.loads(http_get(base + "/speakers"))


def find_speaker_id(base, name):
    """キャラクター名(例: ずんだもん)から、ノーマルスタイルの話者IDを探す"""
    for sp in list_speakers(base):
        if sp["name"] == name:
            for st in sp["styles"]:
                if st["name"] == "ノーマル":
                    return st["id"]
            return sp["styles"][0]["id"]
    return None


def speaker_name(base, sid):
    try:
        for sp in list_speakers(base):
            for st in sp.get("styles", []):
                if st["id"] == sid:
                    return sp["name"]
    except Exception:
        pass
    return None


def concat_wavs(parts, out_path):
    """parts: [(wavバイト列, 後ろに入れる無音の秒数)]"""
    with wave.open(io.BytesIO(parts[0][0])) as w0:
        params = w0.getparams()
    with wave.open(str(out_path), "wb") as out:
        out.setparams(params)
        for data, gap in parts:
            with wave.open(io.BytesIO(data)) as w:
                out.writeframes(w.readframes(w.getnframes()))
            out.writeframes(b"\x00" * int(params.framerate * gap) * params.nchannels * params.sampwidth)


def load_readings(path):
    """readings.json({"表記": "読み"})を読み込む。ファイルがなければ空"""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  読み方の辞書の読み込みに失敗しました({p}): {e}")
        return {}


def apply_readings(text, readings):
    # 長い表記を先に置き換える(「久保建英」を「久保」より先に処理するため)
    for k in sorted(readings, key=len, reverse=True):
        text = text.replace(k, readings[k])
    return text


def make_audio(base, lines, speaker, out_path, speed=1.0, readings=None):
    parts = []
    for n, line in enumerate(lines, 1):
        sentences = [s for s in re.split(r"(?<=。)", line) if s.strip()]
        for j, s in enumerate(sentences):
            gap = 0.7 if j == len(sentences) - 1 else 0.15
            parts.append((synth(base, apply_readings(s, readings or {}), speaker, speed), gap / speed))
        print(f"  音声化 {n}/{len(lines)}", end="\r")
    print()
    concat_wavs(parts, out_path)


# ---------------------------------------------------------------- HTML
def build_html(clusters, audio_name, credit, now, notes):
    rows = []
    for i, c in enumerate(clusters, 1):
        title = html.escape(c["title"])
        if c["link"]:
            title = f'<a href="{html.escape(c["link"])}" target="_blank" rel="noopener">{title}</a>'
        srcs = " / ".join(sorted(c["sources"]))
        orig = f"<div class='s'>原文: {html.escape(c['title_en'])}</div>" if c.get("title_en") else ""
        rows.append(f"<li><div class='t'>{title}</div>{orig}<div class='s'>{html.escape(srcs)}</div></li>")
    audio = f'<audio controls preload="none" src="{html.escape(audio_name)}"></audio>' if audio_name else "<p class='s'>(音声なしで生成)</p>"
    notes_html = "".join(f"<li>{html.escape(n)}</li>" for n in notes)
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>サッカーニュース {now.month}/{now.day}</title>
<style>
:root {{ --bg:#fff; --fg:#1a1a1a; --sub:#666; --line:#ddd; --link:#0a58ca; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#14161a; --fg:#eceff3; --sub:#9aa3ad; --line:#2c3138; --link:#7db3ff; }} }}
body {{ margin:0; padding:16px; max-width:720px; margin-inline:auto; background:var(--bg); color:var(--fg); font-family:system-ui,"Hiragino Sans","Yu Gothic",sans-serif; line-height:1.6; }}
h1 {{ font-size:1.3rem; }} audio {{ width:100%; margin:8px 0 16px; }}
ol {{ padding-left:1.4em; }} li {{ padding:8px 0; border-bottom:1px solid var(--line); }}
a {{ color:var(--link); text-decoration:none; }} .s {{ color:var(--sub); font-size:.85rem; }}
footer {{ margin-top:24px; color:var(--sub); font-size:.8rem; }}
</style></head><body>
<h1>サッカーニュース {now.month}/{now.day}</h1>
{audio}
<ol>{''.join(rows)}</ol>
<footer>音声: {html.escape(credit)}<ul>{notes_html}</ul></footer>
</body></html>"""


# ---------------------------------------------------------------- メイン
def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="サッカーニュース読み上げ デモ(無料版)")
    ap.add_argument("--out", default="out", help="出力フォルダ")
    ap.add_argument("--hours", type=int, default=36, help="何時間以内の記事を対象にするか")
    ap.add_argument("--max-items", type=int, default=10, help="読み上げる最大件数")
    ap.add_argument("--engine", default="http://127.0.0.1:50021", help="VOICEVOXエンジンのURL")
    ap.add_argument("--speaker", type=int, default=8, help="VOICEVOXの話者ID(--list-speakersで確認)")
    ap.add_argument("--voice", help="キャラクター名で話者を指定(例: --voice ずんだもん)。--speaker より優先")
    ap.add_argument("--speed", type=float, default=1.0, help="話す速さ(1.0が標準。1.2〜1.3で少し速め)")
    ap.add_argument("--readings", default="readings.json", help="読み方の辞書ファイル(JSON)。初期値はカレントフォルダのreadings.json")
    ap.add_argument("--no-audio", action="store_true", help="音声化しない(VOICEVOX不要)")
    ap.add_argument("--list-speakers", action="store_true", help="話者の一覧を表示して終了")
    ap.add_argument("--feeds-json", help="FEEDSを上書きするJSONファイル")
    ap.add_argument("--all-topics", action="store_true", help="日本語記事を「欧州の話題と日本代表」に絞らず、すべて読み上げる")
    ap.add_argument("--no-english", action="store_true", help="英語の見出し(翻訳)を使わない")
    ap.add_argument("--no-rss", action="store_true", help="RSS(日本語・英語)を使わない(Xだけで試すとき用)")
    ap.add_argument("--x-max", type=int, default=X_MAX_ITEMS, help="1アカウントから読み上げに使うXの最大件数")
    ap.add_argument("--no-x", action="store_true", help="X(旧Twitter)の投稿を使わない")
    ap.add_argument("--claude-model", default=CLAUDE_MODEL, help="翻訳に使うClaudeのモデル名")
    ap.add_argument("--insecure", action="store_true", help="RSS取得時の証明書検証を省略する(証明書エラーが直らないときの最後の手段)")
    args = ap.parse_args()
    global INSECURE
    INSECURE = args.insecure

    if args.list_speakers:
        try:
            for sp in list_speakers(args.engine):
                for st in sp.get("styles", []):
                    print(f'{st["id"]:>3}: {sp["name"]}({st["name"]})')
        except Exception as e:
            print(f"VOICEVOXエンジンに接続できません({args.engine}): {e}")
        return

    feeds = FEEDS
    if args.feeds_json:
        feeds = json.loads(Path(args.feeds_json).read_text(encoding="utf-8"))

    now_utc = datetime.now(timezone.utc)
    now = datetime.now()
    since = now_utc - timedelta(hours=args.hours)
    notes, all_items, en_items = [], [], []
    blocked_n = 0
    off_topic_n = 0

    if args.no_rss:
        feeds = []
        print("== RSS取得: --no-rss のため飛ばします ==")
    else:
        print("== RSS取得 ==")
    for f in feeds:
        name = f["name"]
        try:
            url = f.get("url") or discover_feed(f["site"])
            if not url:
                raise RuntimeError("RSSフィードが見つかりません(自動検出・よくあるURLとも不可)")
            items = parse_feed(http_get(url), name)
            n_all = len(items)
            items = [i for i in items if not i["date"] or i["date"] >= since]
            en = f.get("lang") == "en"
            n_before = len(items)
            items = [i for i in items if not blocked(i, en)]
            blocked_n += n_before - len(items)
            if not f.get("soccer_only"):
                items = [i for i in items if (is_soccer_en(i) if en else is_soccer(i))]
            if not en and not args.all_topics:
                n0 = len(items)
                items = [i for i in items if is_europe_or_japan(i)]
                off_topic_n += n0 - len(items)
            if en:
                items = items[:EN_PER_FEED]
                en_items += items
            else:
                all_items += items
            print(f"  OK   {name}: {len(items)}件 (取得{n_all}件)  {url}")
        except Exception as e:
            print(f"  NG   {name}: {e}")
            notes.append(f"{name}: 取得できませんでした")

    print(f"\n除外した見出し(実況ページ・Jリーグ・高校選手権など): {blocked_n}件")
    if not args.all_topics:
        print(f"除いた見出し(欧州・日本代表の話題ではないもの): {off_topic_n}件")

    x_token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if args.no_x or not x_token:
        print("\n== X: " + ("--no-x のため飛ばします" if args.no_x else "X_BEARER_TOKEN が未設定のため飛ばします") + " ==")
    else:
        print("\n== X(旧Twitter)の取得 ==")
        for uname, label in X_ACCOUNTS:
            try:
                posts = fetch_x_posts(x_token, uname, label, since, max_items=args.x_max)
                en_items += posts
                print(f"  OK   @{uname}: {len(posts)}件")
            except Exception as e:
                print(f"  NG   @{uname}: {e}")
                notes.append(f"X @{uname}: 取得できませんでした")

    if en_items:
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if args.no_english:
            print("\n== 英語見出し: --no-english のため飛ばします ==")
        elif not key:
            print("\n== 英語見出し: ANTHROPIC_API_KEY が設定されていないため飛ばします ==")
            notes.append("英語の見出しは、APIキー未設定のため含めていません")
        else:
            print(f"\n== 英語見出しの翻訳(Claude {args.claude_model}): {len(en_items)}本 ==")
            try:
                ja = translate_titles(key, args.claude_model, [i["title"] for i in en_items])
                for i, t in zip(en_items, ja):
                    if i.get("x_label"):
                        t = f"{i['x_label']}によると、" + t
                    i["title_en"], i["title"], i["summary"] = i["title"], t, ""
                all_items += en_items
                print("  翻訳できました")
            except Exception as e:
                print(f"  翻訳に失敗したため、英語の見出しは含めません: {e}")
                notes.append("英語の見出しの翻訳に失敗したため、含めていません")

    if not all_items:
        print("記事が0件でした。--hours を増やすか、フィードの設定を確認してください。")
        return

    clusters = cluster(all_items)[: args.max_items]
    print(f"\n== 重複整理: {len(all_items)}件 → {len(clusters)}件 ==")
    lines = build_script(clusters, now)
    script = "\n".join(lines)
    print(f"原稿: 約{len(script)}文字(読み上げ約{len(script) // 350 + 1}分)")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d_%H%M")  # 同じ日に作り直しても別のエピソード(別のファイル名・別のID)になるよう、時刻まで入れる
    (out / f"news_{stamp}.txt").write_text(script, encoding="utf-8")

    audio_name, credit = None, "なし"
    if not args.no_audio:
        print("\n== 音声化(VOICEVOX) ==")
        speaker = args.speaker
        if args.voice:
            try:
                sid = find_speaker_id(args.engine, args.voice)
            except Exception:
                sid = speaker  # 接続エラーなどは、下の音声化の失敗メッセージで知らせる
            if sid is None:
                print(f"  話者「{args.voice}」が見つかりません。--list-speakers で名前を確認してください")
                return
            speaker = sid
        try:
            audio_name = f"news_{stamp}.wav"
            readings = load_readings(args.readings)
            if readings:
                print(f"  読み方の辞書: {len(readings)}件を適用")
            make_audio(args.engine, lines, speaker, out / audio_name, args.speed, readings)
            credit = "VOICEVOX:" + (speaker_name(args.engine, speaker) or f"話者ID {speaker}")
        except Exception as e:
            print(f"  音声化に失敗しました: {e}")
            print("  VOICEVOXを起動しているか確認してください(--no-audio なら原稿だけ作れます)")
            audio_name = None
            notes.append("音声化に失敗しました")

    (out / "index.html").write_text(build_html(clusters, audio_name, credit, now, notes), encoding="utf-8")
    # クラウド版(build_site.py)が、公開用のフィードを作るときに読むメモ
    meta = {
        "date": now.strftime("%Y-%m-%d"),
        "stamp": stamp,
        "audio": audio_name,
        "credit": credit,
        "headlines": [
            {"title": c["title"], "title_en": c.get("title_en", ""), "link": c["link"], "sources": sorted(c["sources"])}
            for c in clusters
        ],
        "notes": notes,
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n完了: {out / 'index.html'} をブラウザで開いてください")


if __name__ == "__main__":
    main()
