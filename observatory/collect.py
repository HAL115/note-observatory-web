"""noteのページをブラウザなしで読み、観測の CSV を書く。

GitHub Actions から定時に動かす。ログインしない。/api/ と /search は開かない。
読むのは、タグのキーワードページ（人気・新着）、カテゴリページ（棚）、タグ探しのページだけ。
ページの HTML には Next.js のデータ（self.__next_f.push）が埋め込まれていて、
記事の URL・タイトル・公開時刻・スキ数・価格が、画面を描かなくても取れる。

本文は保存しない。取るのは一覧に出ている項目だけ。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

JST = dt.timezone(dt.timedelta(hours=9))
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
PAUSE = (2.0, 4.5)          # ページとページの間に置く秒数（この幅で揺らす）
MAX_PAGES_PER_RUN = 40      # 1回の実行で開くページの上限（1日の上限 50〜60 の内側）

_FLIGHT = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
_NOTE_KEY = re.compile(r"^n[0-9a-f]{12}$")


class CollectError(Exception):
    pass


# ---------------------------------------------------------------- 取得

def fetch(url: str, timeout: int = 30) -> tuple[str, str]:
    """URL を開いて (最終URL, HTML) を返す。リダイレクトは追う。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ja,en;q=0.8",
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            final = res.geturl()
            raw = res.read()
            charset = res.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        raise CollectError(f"HTTP {e.code} {url}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise CollectError(f"開けない {url}: {e}") from e
    return final, raw.decode(charset, errors="replace")


def pause() -> None:
    time.sleep(random.uniform(*PAUSE))


# ---------------------------------------------------------------- 解析

def flight_text(html: str) -> str:
    """HTML に埋め込まれた Next.js のデータ（JS の文字列）をつなげて返す。"""
    parts = []
    for m in _FLIGHT.finditer(html):
        try:
            parts.append(json.loads('"' + m.group(1) + '"'))
        except json.JSONDecodeError:
            continue
    return "\n".join(parts)


def _json_at(text: str, start: int):
    """text[start] の '{' か '[' から、対応する閉じまでを JSON として読む。"""
    opener = text[start]
    closer = {"{": "}", "[": "]"}[opener]
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise CollectError("JSON が閉じていない")


_ROW = re.compile(r"^([0-9a-f]+):(.*)$")


def _rows(text: str) -> dict[str, str]:
    """flight の行（id:中身）を id ごとに分ける。参照（"$@7c"）の解決に使う。"""
    rows = {}
    for line in text.split("\n"):
        m = _ROW.match(line)
        if m:
            rows[m.group(1)] = m.group(2)
    return rows


def _resolve(v, rows: dict[str, str], depth: int = 0):
    """"$@id" の参照を、その行の JSON に置きかえる（入れ子も）。"""
    if depth > 8:
        return v
    if isinstance(v, str) and v.startswith("$@"):
        raw = rows.get(v[2:])
        if raw is None:
            return v
        try:
            return _resolve(json.loads(raw), rows, depth + 1)
        except json.JSONDecodeError:
            return v
    if isinstance(v, list):
        return [_resolve(x, rows, depth + 1) for x in v]
    if isinstance(v, dict):
        return {k: _resolve(x, rows, depth + 1) for k, x in v.items()}
    return v


def initial_data(html: str) -> dict:
    """ページの initialData のうち、記事の一覧（notes か sections）を持つものを取り出す。

    "initialData" はページに複数あり、値が別の行への参照（"$@7c"）のこともある（2026-11-10 に確認）。
    無ければ CollectError。
    """
    text = flight_text(html)
    rows = _rows(text)
    key = '"initialData":'
    pos = 0
    while True:
        i = text.find(key, pos)
        if i < 0:
            break
        j = i + len(key)
        val = None
        try:
            if j < len(text) and text[j] == '"':
                end = text.index('"', j + 1)
                val = _resolve(text[j + 1:end], rows)
            elif j < len(text) and text[j] in "{[":
                val = _resolve(_json_at(text, j), rows)
        except (CollectError, json.JSONDecodeError, ValueError):
            val = None
        if isinstance(val, dict) and (val.get("notes") or val.get("sections")):
            return val
        pos = j
    raise CollectError("initialData が見つからない（ページの形が変わったか、記事一覧が無い）")


_KEYWORD = re.compile(r'"keyword":\{"name":"((?:[^"\\]|\\.)+)","noteCount":(\d+)\}')


def related_keywords(html: str) -> list[tuple[str, int]]:
    """キーワードページに出ている関連キーワードと、その記事数。タグ探しの「件数」の手がかり。"""
    text = flight_text(html)
    out = []
    seen = set()
    for m in _KEYWORD.finditer(text):
        name = json.loads('"' + m.group(1) + '"')
        if name not in seen:
            seen.add(name)
            out.append((name, int(m.group(2))))
    return out


def page_title(html: str) -> str:
    m = re.search(r"<title>([^<]*)</title>", html)
    return (m.group(1) if m else "").replace("｜note", "").strip()


def _note_row(n: dict) -> dict | None:
    """initialData の1記事を、観測の行に使う形にそろえる。"""
    key = n.get("key") or ""
    user = (n.get("user") or {}).get("urlname") or ""
    if not _NOTE_KEY.match(key) or not user:
        return None
    pub = (n.get("publishAt") or "")[:16].replace("T", " ")
    price = n.get("price") or 0
    return {
        "url": f"/{user}/n/{key}",
        "title": (n.get("name") or "").replace(",", "，").strip(),
        "likes": n.get("likeCount") if isinstance(n.get("likeCount"), int) else "",
        "published": pub,
        "price": f"¥{price:,}" if price else "",
        "price_num": price,
        "author": user,
        "nickname": ((n.get("user") or {}).get("nickname") or "").replace(",", "，"),
    }


def notes_from_tag_page(html: str) -> list[dict]:
    data = initial_data(html)
    out = []
    for n in data.get("notes") or []:
        r = _note_row(n)
        if r:
            out.append(r)
    return out


def sections_from_topic_page(html: str) -> list[tuple[str, list[dict]]]:
    data = initial_data(html)
    out = []
    for s in data.get("sections") or []:
        label = (s.get("labelName") or s.get("name") or "").strip()
        rows = [r for r in (_note_row(n) for n in s.get("notes") or []) if r]
        if label and rows:
            out.append((label, rows))
    return out


# ---------------------------------------------------------------- URL

def tag_url(tag: str, new: bool = False) -> str:
    u = "https://note.com/tag/" + urllib.parse.quote(tag, safe="")
    return u + "?mode=new" if new else u


def hashtag_url(tag: str) -> str:
    return "https://note.com/hashtag/" + urllib.parse.quote(tag, safe="")


def topic_url(path: str) -> str:
    return "https://note.com/topic/" + path.strip("/")


# ---------------------------------------------------------------- 書き出し

def _stamp(t: dt.datetime) -> tuple[str, str]:
    return t.strftime("%Y-%m-%d %H:%M"), t.strftime("%Y-%m-%d_%H%M")


def _write_csv(path: Path, header: list[str], rows: list[list]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


class Status:
    """この回で何を開き、何が取れなかったかの記録（analysis/collect_status.json に足す）。"""

    def __init__(self, kind: str, started: dt.datetime):
        self.kind = kind
        self.started = started.strftime("%Y-%m-%d %H:%M")
        self.pages: list[dict] = []
        self.errors: list[str] = []
        self.files: list[str] = []

    def page(self, label: str, n: int) -> None:
        self.pages.append({"label": label, "items": n})

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def save(self, keep: int = 60) -> Path:
        path = config.ROOT / "analysis" / "collect_status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        hist = []
        if path.exists():
            try:
                hist = json.loads(path.read_text(encoding="utf-8")).get("runs", [])
            except json.JSONDecodeError:
                hist = []
        hist.append({"kind": self.kind, "started": self.started, "finished": dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
                     "pages": len(self.pages), "items": sum(p["items"] for p in self.pages),
                     "errors": self.errors, "files": self.files})
        hist = hist[-keep:]
        path.write_text(json.dumps({"runs": hist}, ensure_ascii=False, indent=1), encoding="utf-8")
        return path


# ---------------------------------------------------------------- 観測

SERIES_HEADER = ["observed_at_jst", "surface", "section", "rank", "url", "likes", "published_jst", "price", "title"]
SHELF_HEADER = ["observed_at_jst", "category", "category_name", "section", "rank", "url", "likes", "age_h", "paid", "title"]
SCAN_HEADER = ["observed_at_jst", "tag", "view", "page", "total", "rank", "url", "likes", "published_jst", "title", "text"]


def series_tags() -> list[str]:
    for g in config.load("tags").get("groups", []):
        if g.get("id") == "series6":
            return list(g["tags"])
    raise CollectError("config/tags.json に series6 が無い")


def collect_series(tags: list[str] | None = None, out_dir: Path | None = None, now: dt.datetime | None = None) -> Path:
    """6タグの人気・新着（12ページ）を1つの CSV に書く。"""
    tags = tags or series_tags()
    now = now or dt.datetime.now(JST)
    observed, stamp = _stamp(now)
    st = Status("series", now)
    rows: list[list] = []
    pages = 0
    for tag in tags:
        for new in (False, True):
            if pages >= MAX_PAGES_PER_RUN:
                st.error("ページ数の上限")
                break
            label = ("tagnew:" if new else "tag:") + tag
            try:
                _, html = fetch(tag_url(tag, new))
                notes = notes_from_tag_page(html)
            except CollectError as e:
                st.error(f"{label}: {e}")
                notes = []
            pages += 1
            st.page(label, len(notes))
            for i, n in enumerate(notes, 1):
                rows.append([observed, label, "", i, n["url"], n["likes"], n["published"], n["price"], n["title"]])
            pause()
    out_dir = out_dir or (config.ROOT / "data" / "raw" / "series")
    path = _write_csv(out_dir / f"note_観測ログ_raw_{stamp}.csv", SERIES_HEADER, rows)
    st.files.append(str(path.relative_to(config.ROOT)) if path.is_relative_to(config.ROOT) else path.name)
    st.save()
    if not rows:
        raise CollectError("1本も取れなかった: " + "; ".join(st.errors))
    return path


def collect_shelves(categories: list[tuple[str, str]], out_dir: Path | None = None, now: dt.datetime | None = None) -> Path:
    """カテゴリページの棚を CSV に書く。categories は (path, name) の並び（shelfplan の結果）。"""
    now = now or dt.datetime.now(JST)
    observed, stamp = _stamp(now)
    st = Status("shelf", now)
    rows: list[list] = []
    for path_, name in categories[:MAX_PAGES_PER_RUN]:
        try:
            _, html = fetch(topic_url(path_))
            secs = sections_from_topic_page(html)
            title = page_title(html) or name
        except CollectError as e:
            st.error(f"{path_}: {e}")
            secs, title = [], name
        st.page(path_, sum(len(r) for _, r in secs))
        for label, notes in secs:
            for i, n in enumerate(notes, 1):
                age = ""
                if n["published"]:
                    try:
                        p = dt.datetime.fromisoformat(n["published"]).replace(tzinfo=JST)
                        age = f"{(now - p).total_seconds() / 3600:.2f}"
                    except ValueError:
                        age = ""
                rows.append([observed, path_, name or title, label, i, n["url"], n["likes"], age,
                             1 if n["price_num"] else 0, n["title"]])
        pause()
    out_dir = out_dir or (config.ROOT / "data" / "raw" / "shelves")
    path = _write_csv(out_dir / f"note_棚_raw_{stamp}.csv", SHELF_HEADER, rows)
    st.files.append(path.name)
    st.save()
    if not rows:
        raise CollectError("1本も取れなかった: " + "; ".join(st.errors))
    return path


def scan_tags(n: int = 15, con=None) -> list[str]:
    """タグ探しで見るタグ。config/tags.json の fields から、まだ見ていない（か、いちばん古い）ものを n 個。"""
    cands = [t for f in config.load("tags").get("fields", []) for t in f.get("tags", [])]
    last: dict[str, str] = {}
    if con is not None:
        for r in con.execute("SELECT tag, MAX(run_at) FROM obs WHERE kind='scan' GROUP BY tag").fetchall():
            last[r[0]] = r[1]
    cands.sort(key=lambda t: last.get(t, ""))
    return cands[:n]


def collect_scans(tags: list[str], out_dir: Path | None = None, now: dt.datetime | None = None) -> Path:
    """タグ探し：タグごとに人気と新着を取る（ハッシュタグページはキーワードページに転送されることがある）。"""
    now = now or dt.datetime.now(JST)
    observed, stamp = _stamp(now)
    st = Status("scan", now)
    rows: list[list] = []
    pages = 0
    for tag in tags:
        if pages + 2 > MAX_PAGES_PER_RUN:
            st.error("ページ数の上限")
            break
        try:
            final, html = fetch(hashtag_url(tag))
            page = "tag" if "/tag/" in final else "hashtag"
            popular = notes_from_tag_page(html)
        except CollectError as e:
            st.error(f"{tag}: {e}")
            page, popular, final = "", [], ""
        pages += 1
        pause()
        fresh: list[dict] = []
        if final:
            try:
                sep = "&" if "?" in final else "?"
                _, html2 = fetch(final.split("#")[0] + sep + "mode=new")
                fresh = notes_from_tag_page(html2)
            except CollectError as e:
                st.error(f"{tag} 新着: {e}")
            pages += 1
            pause()
        st.page(tag, len(popular) + len(fresh))
        for view, notes in (("popular", popular[:20]), ("new", fresh[:50])):
            for i, n in enumerate(notes, 1):
                rows.append([observed, tag, view, page, "", i, n["url"], n["likes"], n["published"], n["title"],
                             (n["title"] + " " + n["nickname"]).strip()[:160]])
    out_dir = out_dir or (config.ROOT / "data" / "raw" / "scans")
    path = _write_csv(out_dir / f"note_タグ探し_raw_{stamp}.csv", SCAN_HEADER, rows)
    st.files.append(path.name)
    st.save()
    if not rows:
        raise CollectError("1本も取れなかった: " + "; ".join(st.errors))
    return path
