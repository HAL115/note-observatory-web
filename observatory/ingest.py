"""観測の CSV を SQLite に取り込む。

- data/raw（と config/project.json の sources）の下にある CSV を全部見る
- 同じ中身のファイルは2回取り込まない（sha256 で判定）
- 取り込んだあと、近い時刻の観測を「1回の観測（run）」にまとめる
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
import sqlite3
from pathlib import Path

from . import config, db

NOTE_HOST = re.compile(r"^https?://(www\.)?note\.com")
JST = dt.timezone(dt.timedelta(hours=9))


def parse_time(s: str | None) -> str | None:
    """日本時間の 'YYYY-MM-DD HH:MM' にそろえる。Z や +00:00 付きは日本時間に直す。"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z") or re.search(r"[+-]\d\d:?\d\d$", s):
            t = dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(JST)
        else:
            t = dt.datetime.fromisoformat(s[:16].replace(" ", "T"))
    except ValueError:
        return None
    return t.strftime("%Y-%m-%d %H:%M")


def to_int(s) -> int | None:
    s = re.sub(r"[,件\s]", "", str(s or ""))
    return int(s) if s.isdigit() else None


def norm_url(u: str) -> str:
    u = NOTE_HOST.sub("", (u or "").strip()).split("?")[0]
    return u if u.startswith("/") else "/" + u if u else ""


def author_of(url: str) -> str:
    parts = [p for p in url.split("/") if p]
    return parts[0] if parts else ""


SERIES_PREFIX = {"tag": "popular", "tagnew": "new", "topic": "category", "search": "search"}
# カテゴリページの棚に混ざる、noteの案内カード（記事ではない）
NOT_ARTICLE_AUTHORS = {"info"}


def to_float(s) -> float | None:
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def meta_rows_from_csv(reader, own_account: str):
    """記事の分類（パンくず）の CSV を、article_meta の行に直す。"""
    out, skipped = [], 0
    for r in reader:
        url = norm_url(r.get("url", ""))
        checked = parse_time(r.get("checked_at_jst"))
        if not url or not checked:
            skipped += 1
            continue
        author = author_of(url)
        out.append(dict(
            checked_at=checked, url=url, author=author, own=1 if author == own_account else 0,
            category_path=(r.get("category_path") or "").strip(),
            category_name=(r.get("category_name") or "").strip().replace(">", "＞"),
            hashtags=(r.get("hashtags") or "").strip(),
            published_at=parse_time(r.get("published_jst")),
            title=(r.get("title") or "").strip(),
        ))
    return out, skipped


def rows_from_csv(text: str, own_account: str):
    """CSV の中身を、obs テーブルの行（dict）に直す。kind と行のリストを返す。

    kind が article のときだけ、行は article_meta 用（記事の分類）。
    """
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    if "surface" in header:
        kind = "series"
    elif "view" in header and "tag" in header:
        kind = "scan"
    elif "category" in header and "section" in header and "age_h" in header:
        kind = "shelf"
    elif "checked_at_jst" in header and "category_name" in header:
        rows, skipped = meta_rows_from_csv(reader, own_account)
        return "article", rows, skipped
    else:
        raise ValueError(f"知らない形の CSV です（ヘッダー: {header}）")

    out = []
    skipped = 0
    for r in reader:
        url = norm_url(r.get("url", ""))
        observed = parse_time(r.get("observed_at_jst"))
        if not url or not observed:
            skipped += 1
            continue
        if kind == "shelf":
            if author_of(url) in NOT_ARTICLE_AUTHORS:
                skipped += 1
                continue
            age = to_float(r.get("age_h"))
            pub = None
            if age is not None:
                pub = (dt.datetime.fromisoformat(observed) - dt.timedelta(hours=age)).strftime("%Y-%m-%d %H:%M")
            author = author_of(url)
            out.append(dict(
                kind=kind, observed_at=observed, surface="category",
                tag=(r.get("category") or "").strip(), section=(r.get("section") or "").strip(),
                page="", total=None, rank=to_int(r.get("rank")), url=url, author=author,
                own=1 if author == own_account else 0, likes=to_int(r.get("likes")), published_at=pub,
                price="有料" if str(r.get("paid") or "").strip() in ("1", "true", "True") else "",
                title=(r.get("title") or "").strip(), text=(r.get("category_name") or "").strip(),
            ))
            continue
        if kind == "series":
            pre, _, name = (r.get("surface") or "").partition(":")
            surface = SERIES_PREFIX.get(pre)
            if not surface or not name:
                skipped += 1
                continue
            row = dict(surface=surface, tag=name,
                       section=(r.get("section") or r.get("section_or_rank") or "").strip(),
                       page="", total=None)
        else:
            view = (r.get("view") or "").strip()
            surface = {"popular": "popular", "new": "new"}.get(view)
            if not surface:
                skipped += 1
                continue
            row = dict(surface=surface, tag=(r.get("tag") or "").strip(), section="",
                       page=(r.get("page") or "").strip(), total=to_int(r.get("total")))
        author = author_of(url)
        row.update(
            kind=kind,
            observed_at=observed,
            rank=to_int(r.get("rank")),
            url=url,
            author=author,
            own=1 if author == own_account else 0,
            likes=to_int(r.get("likes")),
            published_at=parse_time(r.get("published_jst")),
            price=(r.get("price") or "").strip(),
            title=(r.get("title") or "").strip(),
            text=(r.get("text") or "").strip(),
        )
        out.append(row)
    return kind, out, skipped


COLS = ["file_id", "kind", "observed_at", "surface", "tag", "section", "page", "total", "rank",
        "url", "author", "own", "likes", "published_at", "price", "title", "text"]


def ingest_file(con: sqlite3.Connection, path: Path, own_account: str) -> tuple[str, int, int]:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if con.execute("SELECT 1 FROM files WHERE sha256=?", (sha,)).fetchone():
        return "skip", 0, 0
    text = raw.decode("utf-8-sig")
    kind, rows, skipped = rows_from_csv(text, own_account)
    cur = con.execute(
        "INSERT INTO files(name, sha256, kind, rows, ingested_at) VALUES (?,?,?,?,?)",
        (path.name, sha, kind, len(rows), dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M")),
    )
    fid = cur.lastrowid
    before = con.total_changes
    if kind == "article":
        cols = ["file_id", "checked_at", "url", "author", "own", "category_path", "category_name",
                "hashtags", "published_at", "title"]
        con.executemany(
            f"INSERT OR IGNORE INTO article_meta({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [[fid if c == "file_id" else r.get(c) for c in cols] for r in rows],
        )
        return kind, con.total_changes - before, skipped
    con.executemany(
        f"INSERT OR IGNORE INTO obs({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
        [[fid if c == "file_id" else r.get(c) for c in COLS] for r in rows],
    )
    added = con.total_changes - before
    return kind, added, skipped


def assign_runs(con: sqlite3.Connection, gap_minutes: int) -> int:
    """観測時刻を並べ、gap_minutes を超えて空いたところで回を区切る。回の時刻は最初の観測時刻。"""
    n_runs = 0
    for (kind,) in con.execute("SELECT DISTINCT kind FROM obs").fetchall():
        times = [r[0] for r in con.execute(
            "SELECT DISTINCT observed_at FROM obs WHERE kind=? ORDER BY observed_at", (kind,))]
        run_of = {}
        start = prev = None
        for t in times:
            cur = dt.datetime.fromisoformat(t)
            if prev is None or (cur - prev).total_seconds() > gap_minutes * 60:
                start = t
                n_runs += 1
            run_of[t] = start
            prev = cur
        con.executemany("UPDATE obs SET run_at=? WHERE kind=? AND observed_at=?",
                        [(run, kind, t) for t, run in run_of.items()])
    return n_runs


def run(rebuild: bool = False, verbose: bool = True) -> dict:
    proj = config.project()
    if rebuild and config.DB_PATH.exists():
        config.DB_PATH.unlink()
    con = db.connect()
    stats = {"files_new": 0, "files_skipped": 0, "rows_added": 0, "rows_skipped": 0, "errors": []}
    paths = []
    for d in config.source_dirs():
        if d.exists():
            paths += sorted(d.rglob("*.csv"))
    for p in paths:
        try:
            kind, added, skipped = ingest_file(con, p, proj["own_account"])
        except (ValueError, UnicodeDecodeError) as e:
            stats["errors"].append(f"{p.name}: {e}")
            continue
        if kind == "skip":
            stats["files_skipped"] += 1
        else:
            stats["files_new"] += 1
            stats["rows_added"] += added
            stats["rows_skipped"] += skipped
            if verbose:
                print(f"  取り込み {p.name}（{kind}、{added}行）")
    stats["runs"] = assign_runs(con, proj.get("run_gap_minutes", 45))
    con.commit()
    con.close()
    return stats
