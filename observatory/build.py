"""ダッシュボードの書き出し。

site/private/index.html … 自分用。最新の人気の記事一覧（他の書き手のタイトル・URL）も入る
site/members/index.html … メンバー用。タグごとの集計と自分の記事だけ。他の書き手の情報は入れない
"""

from __future__ import annotations

import datetime as dt
import json
import re

from . import config, db, metrics as m

JST = dt.timezone(dt.timedelta(hours=9))
DENSE_RUNS = 10  # 人気入りの割合を出すのに要る、最低の観測回数


def r1(x):
    return None if x is None else round(x, 1)


def shelf_row(s: dict, field: str, source: str) -> dict:
    return {
        "tag": s["tag"],
        "field": field,
        "source": source,  # series（定時観測）| scan（狙い目タグ探し）
        "page": s["page"],
        "total": s["total"],
        "runs": s["runs"],
        "last_run": s["last_run"],
        "pop_age_days": r1(s["pop_age_days"]),
        "pop_likes": r1(s["pop_likes"]),
        "new_per_day": None if s["new_per_day"] is None else round(s["new_per_day"]),
        "new_depth_h": r1(s["new_depth_h"]),
        "qualified": s["qualified"],
    }


def own_articles(con, audience: str = "private") -> list[dict]:
    """自分の記事と、3つの関門（キーワード・カテゴリ・棚）のどこまで進んだか。"""
    cfg = config.load("own_articles").get("articles", [])
    seen = m.own_appearances(con)
    meta = m.article_categories(con)
    now = dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    out = []
    for a in cfg:
        key = a.get("url_key") or ""
        hint = a.get("title_hint") or ""
        hits = [s for s in seen if (key and key in s["url"]) or
                (not key and hint and hint in (s["title"] or "") and (s["published_at"] or "") >= a["published_at"][:10])]
        apps = [{"run_at": h["run_at"], "surface": h["surface"], "tag": h["tag"], "rank": h["rank"], "kind": h["kind"],
                 "section": h.get("section") or "", "category_name": h.get("text") or ""}
                for h in hits]
        mt = next((v for u, v in meta.items() if key and key in u), None)
        tag_hits = [h for h in hits if h["kind"] != "shelf" and h["surface"] in ("new", "popular")]
        shelf_hits = [h for h in hits if h["kind"] == "shelf"]
        gates = {
            "keyword": sorted({h["tag"] for h in tag_hits}),
            "popular": sorted({h["tag"] for h in tag_hits if h["surface"] == "popular"}),
            "category": mt["category_name"] if mt else None,
            "category_checked_at": mt["checked_at"] if mt else None,
            "shelf": sorted({f"{h.get('text') or h['tag']}「{h.get('section') or ''}」" for h in shelf_hits}),
        }
        hashtags = mt["hashtags"].split("|") if mt and mt["hashtags"] else a.get("tags", [])
        status = "公開前" if a["published_at"] > now else ("載った" if apps else "観測した面には出ていない")
        out.append({
            "id": a["id"], "label": a["label"], "published_at": a["published_at"], "paid": a.get("paid", False),
            "tags": hashtags, "memo": a.get("memo", "") if audience == "private" else "", "status": status,
            "appearances": apps, "gates": gates,
        })
    return out


def payload(con, audience: str) -> dict:
    proj = config.project()
    tags_cfg = config.load("tags")
    field_of = config.field_of_tag()
    top_n, depth_n = proj.get("popular_top_n", 20), proj.get("new_depth_n", 40)

    shelves = []
    scan_tags = set(m.tags_of(con, "scan"))
    for t in sorted(scan_tags):
        shelves.append(shelf_row(m.shelf(con, t, "scan", top_n, depth_n), field_of.get(t, "その他"), "scan"))
    for g in tags_cfg.get("groups", []):
        for t in g["tags"]:
            s = m.shelf(con, t, "series", top_n, depth_n)
            if s["runs"]:
                shelves.append(shelf_row(s, field_of.get(t, g.get("field", g["name"])), "series"))

    entries = []
    for t in m.tags_of(con, "series", "new"):
        s = m.shelf(con, t, "series")
        if s["runs"] < DENSE_RUNS:
            continue
        # 公開から12時間以上追えた記事だけを対象にする
        to = (m.T(s["last_run"]) - dt.timedelta(hours=12)).strftime("%Y-%m-%d %H:%M")
        e = m.entry(con, t, s["first_run"], to)
        if e["n"]:
            e["rate"] = r1(e["rate"] * 100)
            e["lag_h"] = r1(e["lag_h"])
            e["runs"] = s["runs"]
            entries.append(e)

    rhythm_tags = [t for t in m.tags_of(con, "series", "new") if m.shelf(con, t, "series")["runs"] >= DENSE_RUNS]

    data = {
        "audience": audience,
        "title": proj.get(audience, {}).get("title", "noteタグ観測ダッシュボード"),
        "generated_at": dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "coverage": m.coverage(con),
        "gaps": m.gaps(con),
        "shelves": shelves,
        "entries": entries,
        "rhythm": {"tags": rhythm_tags, "hours": m.update_rhythm(con, rhythm_tags)},
        "hypotheses": config.load("hypotheses"),
        "own": own_articles(con, audience) if (audience == "private" or proj.get("members", {}).get("show_own_articles", True)) else [],
        # カテゴリページの棚（カテゴリごとの集計と、棚のキーワードだけ。記事や書き手は入れない）
        "categories": m.shelf_categories(con),
    }
    if audience == "private":
        data["latest_popular"] = latest_popular(con)
    return data


def latest_popular(con, k: int = 10) -> list[dict]:
    """タグごとの、いちばん新しい回の人気上位 k 本（自分用だけ）。"""
    out = []
    for kind in ("series", "scan"):
        for t in m.tags_of(con, kind):
            lists = m.lists_by_run(con, kind, "popular", t)
            if not lists:
                continue
            run = max(lists)
            out.append({
                "tag": t, "kind": kind, "run_at": run,
                "items": [{"rank": r["rank"], "title": r["title"], "url": "https://note.com" + r["url"],
                           "likes": r["likes"], "published_at": r["published_at"],
                           "age_days": r1(m.hours(r["published_at"], run) / 24) if r["published_at"] else None,
                           "own": r["own"]} for r in lists[run][:k]],
            })
    return out


def render(data: dict) -> str:
    tpl = (config.WEB_DIR / "dashboard.html").read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = tpl.replace("__PAYLOAD__", blob).replace("__TITLE__", data["title"])
    return html


def leaks(con, html: str, own_account: str) -> list[str]:
    """メンバー版に、他の書き手の URL・ID・タイトルが入っていないかを調べる。見つかったものを返す。

    カテゴリの棚の言葉（noteが付けたキーワード）と同じ文字のタイトル（例：ドラマの題名だけの記事）は、
    言葉として出しているので数えない。
    """
    found = []
    words = {_clean_word(r[0]) for r in con.execute("SELECT DISTINCT section FROM obs WHERE kind='shelf'")}
    for u in re.findall(r"note\.com/[^\s\"'<>]+", html):
        if f"/{own_account}/" not in u:
            found.append("URL: " + u)
    # カテゴリのパス（lifestyle など）と同じ綴りの書き手IDは、パスとして出ているので数えない
    paths = set()
    for (tag,) in con.execute("SELECT DISTINCT tag FROM obs WHERE kind='shelf'"):
        paths.add(tag)
        paths.update(tag.split("/"))
    authors = [r[0] for r in con.execute(
        "SELECT DISTINCT author FROM obs WHERE own=0 UNION SELECT DISTINCT author FROM article_meta WHERE own=0")]
    for a in authors:
        if len(a) >= 6 and a not in paths and a in html:
            found.append("ID: " + a)
    titles = [r[0] for r in con.execute(
        "SELECT DISTINCT title FROM obs WHERE own=0 AND length(title)>=10 "
        "UNION SELECT DISTINCT title FROM article_meta WHERE own=0 AND length(title)>=10")]
    for t in titles:
        if t.strip() in words:
            continue
        if t[:20] in html:
            found.append("タイトル: " + t[:20])
    return found


def run() -> dict:
    con = db.connect()
    proj = config.project()
    out = {}
    for audience in ("private", "members"):
        html = render(payload(con, audience))
        if audience == "members":
            bad = leaks(con, html, proj["own_account"])
            if bad:
                raise SystemExit("メンバー版に他の書き手の情報が入っています。書き出しを止めました:\n  " + "\n  ".join(bad[:10]))
        path = config.SITE_DIR / audience / "index.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        out[audience] = str(path)
    con.close()
    return out


# ---- 共有アプリ（タグ観測ツール）用のデータ ----
#
# アプリは Claude 上に置き、メンバーにも見せる。入れるのは集計と、自分の記事と、
# 記事番号を塩付きハッシュにした照合表（seen）だけ。ほかの書き手の名前・ID・タイトル・URL は入れない。
# 照合表は、見る人が自分で入れた記事URLを、ブラウザの中で同じハッシュにして引くためのもの。
# 棚の記事ごとの数字（dist）は、スキ・公開からの時間・有料かどうかの3つだけで、誰の記事かは入れない。
#
# 点数の定義は docs/metrics.md「狙い目の点数」。変えるときは、そちらも直す。

APP_SALT = "halsy-obs-1"
SURF_CODE = {"new": "n", "popular": "p", "category": "s"}
APP_VERSION = 2
FULL_SWEEP_SHARE = 0.8   # 見たことのあるカテゴリの8割以上を同じ回に見ていれば「一巡の回」。カテゴリどうしを比べる基準にする
ROLL_DAYS = 8            # 一巡の回がこれより古ければ、直近この日数の巡回（カテゴリごとのいちばん新しい回）で比べる
FEW_ARTICLES = 15        # 棚の記事がこれより少ないカテゴリは「本数が少ない」。点数は出すが、比べる基準には入れない
MIN_FRESH = 3            # スキの集まる速さは、24時間以内の記事がこれだけあるときだけ出す
WORD_MIN_N = 3           # 棚の言葉の点数は、記事がこれだけある棚だけを比べる基準にする
FLAG_Z = 2.5             # 中央値から MAD で測って、これ以上離れていれば「突出」

# (項目, +1 なら大きいほど良い / -1 なら小さいほど良い)
CATEGORY_AXES = {
    "entry": [("likes_q25", -1), ("under10_pct", +1)],
    "turnover": [("fresh24_pct", +1), ("age_med_h", -1)],
    "reader": [("likes_q75", +1), ("speed_med", +1)],
}
CATEGORY_WEIGHTS = {"entry": 0.4, "turnover": 0.3, "reader": 0.3}
TAG_AXES = {
    "entry": [("pop_low", -1), ("pop_q1", -1)],
    "turnover": [("pop_age_days", -1)],
    "competition": [("new_per_day", -1), ("qualified", -1)],
    "reader": [("pop_top1", +1)],
}
TAG_WEIGHTS = {"entry": 0.35, "turnover": 0.25, "competition": 0.2, "reader": 0.2}
WORD_AXES = {
    "entry": [("likes_med", -1), ("fresh_pct", +1)],
    "reader": [("likes_max", +1)],
}
WORD_WEIGHTS = {"entry": 0.7, "reader": 0.3}
# 突出を調べる項目（True はスキや時間なので対数で比べる）
CATEGORY_FLAGS = [("likes_max", True), ("likes_q75", True), ("likes_med", True), ("speed_med", True),
                  ("age_med_h", True), ("paid_pct", False)]
TAG_FLAGS = [("pop_top1", True), ("pop_likes", True), ("new_per_day", True), ("pop_age_days", True)]

SCORE_DEFS = {
    "category": {
        "entry": {"label": "入口の低さ", "parts": ["棚の記事のスキ（少ないほうから4分の1の位置）が少ないほど高い",
                                              "10スキ未満の記事の割合が高いほど高い"]},
        "turnover": {"label": "入れかわり", "parts": ["公開24時間以内の記事の割合が高いほど高い",
                                              "棚の記事の古さ（中央値）が短いほど高い"]},
        "reader": {"label": "読者の厚み", "parts": ["棚の記事のスキ（多いほうから4分の1の位置）が多いほど高い",
                                             "24時間以内の記事が1時間あたりに集めたスキ（中央値）が多いほど高い"]},
        "total": {"label": "狙い目", "parts": ["入口の低さ×0.4 ＋ 入れかわり×0.3 ＋ 読者の厚み×0.3"]},
    },
    "tag": {
        "entry": {"label": "入口の低さ", "parts": ["人気の上位20本のうち、いちばん少ないスキが少ないほど高い",
                                              "人気の上位20本の、少ないほうから4分の1の位置のスキが少ないほど高い"]},
        "turnover": {"label": "入れかわり", "parts": ["人気の上位20本の古さ（中央値）が短いほど高い"]},
        "competition": {"label": "競争の薄さ", "parts": ["1日の新着（目安）が少ないほど高い",
                                                 "人気の上位20本にいる資格者が少ないほど高い（タグ探しの回だけ）"]},
        "reader": {"label": "読者の厚み", "parts": ["人気1位の記事のスキが多いほど高い"]},
        "total": {"label": "狙い目", "parts": ["入口×0.35 ＋ 入れかわり×0.25 ＋ 競争の薄さ×0.2 ＋ 読者×0.2"]},
    },
    "word": {
        "entry": {"label": "入りやすさ", "parts": ["棚の記事のスキ（中央値）が少ないほど高い", "24時間以内の記事の割合が高いほど高い"]},
        "reader": {"label": "伸びしろ", "parts": ["棚でいちばん多いスキが多いほど高い"]},
        "total": {"label": "狙い目", "parts": ["入りやすさ×0.7 ＋ 伸びしろ×0.3（同じ回の言葉どうしで比べる）"]},
    },
    "common": [
        "点数は、比べる相手の中で「下から何%の位置か」を良い向きにそろえて平均したもの（0〜100）。50がちょうど真ん中。",
        "カテゴリは、すべてのカテゴリを同じ時刻に見た回（一巡の回）どうしで比べる。棚のスキは時刻で変わるため。"
        f"一巡の回が{ROLL_DAYS}日より古いときは、直近{ROLL_DAYS}日の巡回（同じ時刻に1回10カテゴリずつ見た回の、カテゴリごとのいちばん新しい回）で比べる。",
        "同じ棚を出している小カテゴリ（例：スポーツと野球）は、比べる相手には1回だけ数える。",
        f"棚の記事が{FEW_ARTICLES}本より少ないカテゴリは、点数は出すが比べる相手には入れない（参考）。",
        f"「突出」は、ほかのカテゴリの中央値から大きく離れた値（中央値からのずれの中央値の約{FLAG_Z}倍以上。スキと時間は対数で比べる）。",
    ],
}


def _hash_article(article_id: str) -> str:
    import hashlib
    return hashlib.sha256((APP_SALT + ":" + article_id).encode("utf-8")).hexdigest()[:16]


def _article_id(url: str) -> str | None:
    mm = re.search(r"/n/(n[0-9a-f]{12})", url or "")
    return mm.group(1) if mm else None


def _clean_word(w: str) -> str:
    return re.sub(r"^/interests/", "", w or "").strip()


def _log(x):
    import math
    return None if x is None else math.log1p(max(0, x))


def _shelf_stats(rows, prev_rows=None) -> dict:
    """カテゴリの棚1回分の集計。prev_rows（前の回の行）があれば、残った記事と入れかわった言葉も出す。"""
    uniq: dict = {}
    words: dict = {}
    for r in rows:
        uniq.setdefault(r["url"], r)
        words.setdefault(r["section"], []).append(r)
    arts = list(uniq.values())

    def age(r):
        return m.hours(r["published_at"], r["observed_at"]) if r["published_at"] else None

    lk = [r["likes"] for r in arts]
    ages = [age(r) for r in arts]
    fresh = [r for r in arts if age(r) is not None and age(r) < 24]
    speeds = [r["likes"] / max(age(r), 1) for r in fresh if r["likes"] is not None]
    pub_hours = [0] * 24   # 24時間以内の記事が、何時台に公開されたか
    age_hours = [0] * 25   # 公開から0〜23時間、24時間以上
    for r in arts:
        a = age(r)
        if a is None:
            continue
        age_hours[min(24, int(a))] += 1
        if a < 24:
            pub_hours[m.T(r["published_at"]).hour] += 1
    kept, how = (m.kept_urls(arts, prev_rows) if prev_rows is not None else (None, None))
    new_arts = [r for r in arts if kept is not None and r["url"] not in kept]
    prev_words = {r["section"] for r in prev_rows} if prev_rows is not None else None
    wl = []
    for w, rs in words.items():
        wk = [r["likes"] for r in rs]
        wf = [r for r in rs if age(r) is not None and age(r) < 24]
        wl.append({
            "word": _clean_word(w), "n": len(rs),
            "likes_min": m._q(wk, 0), "likes_med": m._q(wk, .5), "likes_max": m._q(wk, 1),
            "under10": sum(1 for x in wk if x is not None and x < 10),
            "fresh_n": len(wf), "fresh_pct": m._pct(len(wf), len(rs)),
            "age_med_h": r1(m._q([age(r) for r in rs], .5)),
            "paid": sum(1 for r in rs if r["price"]),
            "is_new": None if prev_words is None else (w not in prev_words),
            "new_n": None if kept is None else sum(1 for r in rs if r["url"] not in kept),
        })
    fl = [r["likes"] for r in fresh]
    return {
        "run": rows[0]["run_at"], "n": len(arts), "writers": len({r["author"] for r in arts}),
        "likes_min": m._q(lk, 0), "likes_q25": m._q(lk, .25), "likes_med": m._q(lk, .5),
        "likes_q75": m._q(lk, .75), "likes_max": m._q(lk, 1),
        "under10_pct": m._pct(sum(1 for x in lk if x is not None and x < 10), len(arts)),
        "fresh24_pct": m._pct(len(fresh), len(arts)),
        "age_med_h": r1(m._q(ages, .5)),
        "paid_pct": m._pct(sum(1 for r in arts if r["price"]), len(arts)),
        "fresh_n": len(fresh), "fresh_likes_med": m._q(fl, .5), "fresh_likes_min": m._q(fl, 0),
        "fresh_age_med_h": r1(m._q([age(r) for r in fresh], .5)),
        "speed_med": round(m._q(speeds, .5), 2) if len(speeds) >= MIN_FRESH else None,
        "pub_hours": pub_hours, "age_hours": age_hours,
        "dist": sorted(([r["likes"], r1(age(r)), 1 if r["price"] else 0] for r in arts if r["likes"] is not None),
                       key=lambda x: (x[0], x[1] if x[1] is not None else 0)),
        "kept_pct": None if kept is None else m._pct(len(kept), len(arts)),
        "kept_by": how,
        "new_n": None if kept is None else len(new_arts),
        "new_likes_med": m._q([r["likes"] for r in new_arts], .5) if new_arts else None,
        "new_age_med_h": r1(m._q([age(r) for r in new_arts], .5)) if new_arts else None,
        "new_words": [] if prev_words is None else [_clean_word(w) for w in words if w not in prev_words],
        "gone_words": [] if prev_words is None else sorted(_clean_word(w) for w in prev_words - set(words)),
        "words": wl,
    }


CATEGORY_FIELDS = ["n", "writers", "likes_min", "likes_q25", "likes_med", "likes_q75", "likes_max", "under10_pct",
                   "fresh24_pct", "age_med_h", "paid_pct", "fresh_n", "fresh_likes_med", "fresh_likes_min",
                   "fresh_age_med_h", "speed_med"]
HISTORY_FIELDS = ["run", "n", "likes_q25", "likes_med", "likes_q75", "under10_pct", "fresh24_pct", "age_med_h",
                  "fresh_likes_med", "fresh_age_med_h", "kept_pct", "kept_by", "new_n", "new_likes_med", "new_age_med_h"]


def _best_window(hours: list[int], width: int = 3) -> dict | None:
    """24時間のうち、記事がいちばん多く入る連続 width 時間（日をまたいでよい）。"""
    total = sum(hours)
    if not total:
        return None
    best = max(range(24), key=lambda h: (sum(hours[(h + i) % 24] for i in range(width)), -h))
    n = sum(hours[(best + i) % 24] for i in range(width))
    return {"from": best, "to": (best + width) % 24, "n": n, "total": total, "share": m._pct(n, total)}


def _flags(row: dict, ref: list[dict], fields) -> list[dict]:
    out = []
    for f, use_log in fields:
        tr = _log if use_log else (lambda v: v)
        z = m.robust_z([tr(x.get(f)) for x in ref], tr(row.get(f)))
        if z is not None and abs(z) >= FLAG_Z:
            out.append({"field": f, "z": z})
    return out


def _category_badges(c: dict) -> list[str]:
    b = []
    if c.get("same_as"):
        b.append("dup")
    if (c["n"] or 0) < FEW_ARTICLES:
        b.append("few")
    if not c["base_is_sweep"]:
        b.append("not_sweep")
    if (c["under10_pct"] or 0) >= 50:
        b.append("low_bar")
    if (c["fresh24_pct"] or 0) >= 70:
        b.append("turnover")
    if c["fresh24_pct"] is not None and c["fresh24_pct"] <= 20:
        b.append("held")
    if (c["paid_pct"] or 0) >= 15:
        b.append("paid")
    return b


def app_categories(con) -> dict:
    """カテゴリの棚。カテゴリどうしは一巡の回で比べ、そのあとの回は前の回からの変化として出す。"""
    runs_by_cat: dict[str, list[str]] = {}
    for r in con.execute("SELECT tag, run_at FROM obs WHERE kind='shelf' GROUP BY tag, run_at ORDER BY tag, run_at"):
        runs_by_cat.setdefault(r["tag"], []).append(r["run_at"])
    if not runs_by_cat:
        return {"items": [], "base_run": None, "snapshots": [], "word_rank_of": {}, "overall": None}
    per_run: dict[str, int] = {}
    for runs in runs_by_cat.values():
        for run in runs:
            per_run[run] = per_run.get(run, 0) + 1
    full = [run for run, k in per_run.items() if k >= FULL_SWEEP_SHARE * len(runs_by_cat)]
    base_run = max(full) if full else None
    # 一巡の回が古くなったら、直近 ROLL_DAYS 日の巡回（カテゴリごとのいちばん新しい回）を比べる土台にする
    newest = max(per_run)
    roll = None
    if base_run is None or m.hours(base_run, newest) > ROLL_DAYS * 24:
        recent = {}
        for cat, runs in runs_by_cat.items():
            rr = [r for r in runs if m.hours(r, newest) <= ROLL_DAYS * 24]
            if rr:
                recent[cat] = max(rr)
        if len(recent) >= FULL_SWEEP_SHARE * len(runs_by_cat):
            roll = recent
            base_run = max(recent.values())
    base_from = min(roll.values()) if roll else base_run

    cats = []
    for cat, runs in runs_by_cat.items():
        prev = None
        hist = []
        name = cat
        for run in runs:
            rows = con.execute("SELECT * FROM obs WHERE kind='shelf' AND tag=? AND run_at=? ORDER BY id",
                               (cat, run)).fetchall()
            if not rows:
                continue
            name = rows[0]["text"] or name
            hist.append(_shelf_stats(rows, prev))
            prev = rows
        if not hist:
            continue
        want = roll.get(cat) if roll else base_run
        base = next((s for s in hist if s["run"] == want), hist[-1])
        latest = hist[-1]
        short, _, parent = name.partition(" | ")
        c = {"path": cat, "name": name, "short": short, "parent": parent or None, "depth": cat.count("/") + 1,
             "base_is_sweep": want is not None and base["run"] == want, "same_as": None,
             "base": base, "latest": latest if latest["run"] != base["run"] else None,
             "history": [{k: s[k] for k in HISTORY_FIELDS} for s in hist]}
        c.update({f: base[f] for f in CATEGORY_FIELDS})
        cats.append(c)

    # 同じ棚を出しているカテゴリ（大カテと小カテで同じページになっているもの）
    sig: dict = {}
    for c in sorted(cats, key=lambda x: (x["depth"], x["path"])):
        key = (c["base"]["run"], tuple(sorted((d[0], -1 if d[1] is None else d[1]) for d in c["base"]["dist"])),
               tuple(sorted(w["word"] for w in c["base"]["words"])))
        if key in sig:
            c["same_as"] = sig[key]
        else:
            sig[key] = c["short"]

    ref = [c for c in cats if not c["same_as"] and c["n"] >= FEW_ARTICLES and c["base_is_sweep"]]
    rank_of = m.add_scores(cats, CATEGORY_AXES, CATEGORY_WEIGHTS, ref)
    ref_ids = {id(c) for c in ref}
    for c in cats:
        c["in_ref"] = id(c) in ref_ids
        c["flags"] = _flags(c, ref, CATEGORY_FLAGS)
        c["badges"] = _category_badges(c)
        hours = [a + b for a, b in zip(c["base"]["pub_hours"], (c["latest"] or {}).get("pub_hours", [0] * 24))]
        c["publish_window"] = _best_window(hours)

    # 棚の言葉：同じ回の言葉どうしで比べる
    word_rank_of = {}
    by_run: dict[str, list[dict]] = {}
    for c in cats:
        if c["same_as"]:
            continue
        for s in [c["base"]] + ([c["latest"]] if c["latest"] else []):
            by_run.setdefault(s["run"], []).extend(s["words"])
    for run, ws in by_run.items():
        wref = [w for w in ws if w["n"] >= WORD_MIN_N]
        word_rank_of[run] = m.add_scores(ws, WORD_AXES, WORD_WEIGHTS, wref)
    for c in cats:   # 同じ棚のカテゴリには、元のカテゴリの言葉の点数を写す
        if c["same_as"]:
            src = next(x for x in cats if x["short"] == c["same_as"] and not x["same_as"])
            for s, t in ((c["base"], src["base"]), (c["latest"], src["latest"])):
                if s and t:
                    for w in s["words"]:
                        tw = next((x for x in t["words"] if x["word"] == w["word"]), None)
                        if tw:
                            w["scores"], w["ranks"] = tw["scores"], tw["ranks"]

    # 回ごとの、棚の新しい記事の公開時刻と古さ（5カテゴリ以上見た回だけ）
    snapshots = []
    for run in sorted(per_run):
        ss = [s for c in cats if not c["same_as"] for s in [c["base"]] + ([c["latest"]] if c["latest"] else [])
              if s["run"] == run]
        if len(ss) < 5:
            continue
        snapshots.append({
            "run": run, "categories": len(ss), "articles": sum(s["n"] for s in ss),
            "fresh_n": sum(s["fresh_n"] for s in ss),
            "pub_hours": [sum(s["pub_hours"][h] for s in ss) for h in range(24)],
            "age_hours": [sum(s["age_hours"][h] for s in ss) for h in range(25)],
            "likes_med": m.med([s["likes_med"] for s in ss]),
        })
    supply = _supply_hours(con)
    tot = sum(supply["hours"]) if supply else 0
    for snap in snapshots:
        n = sum(snap["pub_hours"])
        # 公開した時台ごとの「棚での多さ ÷ 出ている記事の多さ」。1より大きいほど、その時台の記事が棚に多い
        snap["ratio"] = [None if not tot or not n or supply["hours"][h] / tot < SUPPLY_MIN_SHARE
                         else round((snap["pub_hours"][h] / n) / (supply["hours"][h] / tot), 2) for h in range(24)]
        young = [h for h in range(24) if snap["age_hours"][h] >= 3]
        snap["youngest_h"] = young[0] if young else None
    pairs = [(c["path"], c["base"]["run"]) for c in cats if c["base_is_sweep"]]
    return {"items": cats, "base_run": base_run, "base_from": base_from, "base_kind": "rolling" if roll else "sweep",
            "rank_of": rank_of, "word_rank_of": word_rank_of,
            "snapshots": snapshots, "supply": supply, "overall": _overall(con, base_run, pairs if roll else None)}


SUPPLY_MIN_SHARE = 0.02  # 出ている記事がこれより少ない時台は、棚での多さを出さない（数本の違いで大きく振れるため）
SUPPLY_MIN_DEPTH_H = 20  # 新着の40本目が20時間以上前のタグだけで、記事が出る時刻を数える（多いタグは観測した時刻の前に偏るため）


def _supply_hours(con) -> dict | None:
    """記事が何時台に出ているか。新着を1日以上さかのぼれるタグ（1日の新着が少ないタグ）だけで数える。"""
    tags = [t for t in m.tags_of(con, "series", "new")
            if (m.shelf(con, t, "series")["new_depth_h"] or 0) >= SUPPLY_MIN_DEPTH_H]
    runs = [r[0] for r in con.execute("SELECT DISTINCT run_at FROM obs WHERE kind='series' ORDER BY run_at")]
    if not tags or len(runs) < 2:
        return None
    to = (m.T(runs[-1]) - dt.timedelta(hours=12)).strftime("%Y-%m-%d %H:%M")
    hours = [0] * 24
    seen = set()
    q = ("SELECT DISTINCT url, published_at FROM obs WHERE kind='series' AND surface='new' AND tag IN (%s) "
         "AND published_at>=? AND published_at<?" % ",".join("?" * len(tags)))
    for r in con.execute(q, (*tags, runs[0], to)):
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        hours[m.T(r["published_at"]).hour] += 1
    return {"tags": tags, "from": runs[0], "to": to, "articles": len(seen), "hours": hours}


def _overall(con, run: str | None, pairs: list | None = None) -> dict | None:
    """一巡の回（または巡回の、カテゴリと回の組 pairs）の、棚全体の数字（同じ記事は1本と数える）。"""
    if not run:
        return None
    if pairs:
        rows = [r for cat, rr in pairs for r in
                con.execute("SELECT * FROM obs WHERE kind='shelf' AND tag=? AND run_at=?", (cat, rr)).fetchall()]
    else:
        rows = con.execute("SELECT * FROM obs WHERE kind='shelf' AND run_at=?", (run,)).fetchall()
    uniq = {}
    for r in rows:
        uniq.setdefault(r["url"], r)
    arts = list(uniq.values())
    lk = [r["likes"] for r in arts]
    ages = [m.hours(r["published_at"], r["observed_at"]) if r["published_at"] else None for r in arts]
    bins = [(0, 0), (1, 4), (5, 9), (10, 19), (20, 49), (50, 99), (100, 199), (200, None)]
    hist = [{"lo": lo, "hi": hi, "n": sum(1 for x in lk if x is not None and x >= lo and (hi is None or x <= hi))}
            for lo, hi in bins]
    return {
        "run": run, "cards": len(rows), "articles": len(arts), "writers": len({r["author"] for r in arts}),
        "categories": len({r["tag"] for r in rows}),
        "likes_q25": m._q(lk, .25), "likes_med": m._q(lk, .5), "likes_q75": m._q(lk, .75),
        "under10_pct": m._pct(sum(1 for x in lk if x is not None and x < 10), len(arts)),
        "fresh24_pct": m._pct(sum(1 for a in ages if a is not None and a < 24), len(arts)),
        "paid_pct": m._pct(sum(1 for r in arts if r["price"]), len(arts)),
        "likes_hist": hist,
    }


def app_tags(con) -> dict:
    """タグごとの数字と点数。人気の上位20本のスキの幅（少ないほう4分の1・いちばん少ない・1位）を、最近6回の中央値で出す。

    人気の並びはスキの順ではない（仮説H02）ので、「何スキで何位」は出さない。下のほうの記事はスキが
    表示されないことがあるので、取れたスキだけで数える。
    """
    p = payload(con, "members")
    entry_of = {e["tag"]: e for e in p["entries"]}
    out = []
    for s in p["shelves"]:
        kind = s["source"]
        pop = m.lists_by_run(con, kind, "popular", s["tag"])
        last_runs = sorted(pop)[-6:]

        def top(r):
            return [x["likes"] for x in pop[r][:20] if x["likes"] is not None]

        q1 = m.med([m._q(top(r), .25) for r in last_runs if top(r)])
        low = m.med([min(top(r)) for r in last_runs if top(r)])
        top1 = m.med([pop[r][0]["likes"] for r in last_runs if pop[r] and pop[r][0]["likes"] is not None])
        e = entry_of.get(s["tag"]) if kind == "series" else None
        out.append({**s, "pop_top1": top1, "pop_q1": q1, "pop_low": low,
                    "entry_rate": e["rate"] if e else None, "entry_n": e["n"] if e else None,
                    "entry_lag_h": e["lag_h"] if e else None})
    rank_of = m.add_scores(out, TAG_AXES, TAG_WEIGHTS)
    for t in out:
        t["flags"] = _flags(t, out, TAG_FLAGS)
    return {"items": out, "rank_of": rank_of}


def app_seen(con) -> tuple[dict, list]:
    """記事番号（塩付きハッシュ）→ どこに出たか。places は場所の名前の一覧（タグ名・カテゴリの棚の名前だけ）。"""
    places: list[str] = []
    pidx: dict[str, int] = {}
    seen: dict[str, dict] = {}
    q = ("SELECT kind, surface, tag, section, text, url, MIN(rank) best, MIN(run_at) first, MAX(run_at) last, "
         "COUNT(DISTINCT run_at) times FROM obs WHERE surface IN ('new','popular','category') "
         "GROUP BY kind, surface, tag, section, url")
    for r in con.execute(q):
        aid = _article_id(r["url"])
        if not aid:
            continue
        if r["surface"] == "category":
            cat = r["text"] or r["tag"]
            place = f"{cat.split(' | ')[0]}の棚「{_clean_word(r['section'])}」"
        else:
            place = "#" + r["tag"]
        if place not in pidx:
            pidx[place] = len(places)
            places.append(place)
        key = (SURF_CODE[r["surface"]], pidx[place])
        d = seen.setdefault(_hash_article(aid), {})
        cur = d.get(key)
        if cur is None:
            d[key] = [r["best"], r["first"], r["last"], r["times"]]
        else:
            cur[0] = min(x for x in (cur[0], r["best"]) if x is not None) if (cur[0] or r["best"]) else None
            cur[1] = min(cur[1], r["first"])
            cur[2] = max(cur[2], r["last"])
            cur[3] += r["times"]
    packed = {h: [[k[0], k[1], v[0], v[1], v[2], v[3]] for k, v in d.items()] for h, d in seen.items()}
    return packed, places


def _trend(con) -> list[dict]:
    """定時観測の回ごとの、人気の上位20本の古さ（中央値、日）とスキ（中央値）。"""
    trend = []
    for t in m.tags_of(con, "series"):
        pop = m.lists_by_run(con, "series", "popular", t)
        for run, rows in pop.items():
            top = rows[:20]
            ages = [m.hours(r["published_at"], run) / 24 for r in top if r["published_at"]]
            likes = [r["likes"] for r in top if r["likes"] is not None]
            if ages:
                trend.append({"run_at": run, "tag": t, "pop_age_days": r1(m.med(ages)), "pop_likes": m.med(likes)})
    return trend


def body_scoring_summary() -> dict | None:
    """本文の採点（analysis/gate_scoring_results.json）を、組ごとの割合だけにまとめる。URL とタイトルは入れない。"""
    path = config.ROOT / "analysis" / "gate_scoring_results.json"
    sample = config.ROOT / "analysis" / "gate_scoring_sample.json"
    if not path.exists():
        return None
    res = json.loads(path.read_text(encoding="utf-8")).get("results", [])
    rubric = json.loads(sample.read_text(encoding="utf-8")).get("rubric", {}) if sample.exists() else {}
    groups = {}
    for g in ("listed", "unlisted"):
        xs = [r for r in res if r.get("group") == g]
        if not xs:
            continue
        types: dict[str, int] = {}
        for r in xs:
            types[r.get("type", "その他")] = types.get(r.get("type", "その他"), 0) + 1
        groups[g] = {
            "n": len(xs), "score_mean": round(sum(r["score"] for r in xs) / len(xs), 2),
            "items": [m._pct(sum(r.get(f"s{i}", 0) for r in xs), len(xs)) for i in range(1, 6)],
            "hashtag_has_tag": m._pct(sum(1 for r in xs if r.get("hashtag_has_tag")), len(xs)),
            "topic_matches_tag": m._pct(sum(1 for r in xs if r.get("topic_matches_tag")), len(xs)),
            "paid": m._pct(sum(1 for r in xs if r.get("paid")), len(xs)),
            "types": types,
        }
    return {
        "scored_at": sorted({r.get("scored_at", "") for r in res}),
        "method": sorted({r.get("method", "") for r in res}),
        "tags": sorted({r.get("tag", "") for r in res}),
        "rubric": rubric.get("score_0_5", []),
        "groups": groups,
    }


def sheet_snapshot() -> dict | None:
    """スプレッドシート（Apps Script の観測）の、タグの件数と小カテ（関連キーワード）の写し。

    analysis/sheets_snapshot.json があるときだけ使う（Claude がスプレッドシートから写して置く）。
    中身は件数と言葉だけで、記事や書き手は入っていない。
    """
    path = config.ROOT / "analysis" / "sheets_snapshot.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _opt_config(name: str):
    return config.load(name) if (config.CONFIG_DIR / f"{name}.json").exists() else None


def _opt_json(name: str):
    path = config.ROOT / "analysis" / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def node_compare() -> dict | None:
    """ノード比較（上位と上位外の本文の分け方）の集計。analysis/node_compare.json があるときだけ使う。"""
    path = config.ROOT / "analysis" / "node_compare.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---- スプレッドシートの写しを取り込む（定時タスクの Claude が get_values の結果をファイルに置く） ----

def _sheet_values(path) -> list[list]:
    if not path.exists():
        return []
    d = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(d, list) and d and isinstance(d[0], dict) and "text" in d[0]:   # ツールの結果をそのまま保存したもの
        d = json.loads(d[0]["text"])
    if isinstance(d, dict):
        d = d.get("values", [])
    return [[("" if c is None else str(c)).strip() for c in row] for row in d]


def _num(s):
    s = (s or "").replace(",", "").replace("%", "").strip()
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return None


def _count_in_memo(memo: str):
    mm = re.search(r"件数\s*([\d,]+)", memo or "")
    return int(mm.group(1).replace(",", "")) if mm else None


def _t(s: str) -> str:
    """「2026-11-4 9:05」のような書き方を「2026-11-04 09:05」にそろえる。"""
    mm = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})", s or "")
    return f"{mm.group(1)}-{int(mm.group(2)):02d}-{int(mm.group(3)):02d} {int(mm.group(4)):02d}:{mm.group(5)}" if mm else ""


SHEET_WINDOW_DAYS = 7.2   # 直近7日（毎日18:44ごろの回が、7日前の回まで入るように少し広げる）

# スプレッドシート「note観測ログ」のアプリ用タブ（要点・小カテから、いちばん新しい回・7日前の回・基準調査の回だけを
# 取り出す数式）の列。見出しの書き出しで探すので、列の順番が変わっても読める
YOUTEN_COLS = {"time": "観測時刻", "face": "面", "n": "本数", "bottom": "一番下", "memo": "メモ",
               "likes": "スキ中央値", "age": "公開からの時間", "window": "新着の時間幅", "cat": "カテゴリ"}


def _by_header(values: list[list], cols: dict) -> list[dict]:
    if not values:
        return []
    head = values[0]
    idx = {}
    for k, prefix in cols.items():
        idx[k] = next((i for i, h in enumerate(head) if h.startswith(prefix)), None)
    out = []
    for r in values[1:]:
        d = {k: (r[i] if i is not None and i < len(r) else "") for k, i in idx.items()}
        if _t(d.get("time", "")):
            d["time"] = _t(d["time"])
            out.append(d)
    return out


def import_sheets(raw_dir) -> dict:
    """スプレッドシートの写しから、analysis/sheets_snapshot.json と analysis/node_compare.json を作る。

    写しは定時タスクの Claude が get_values の結果を JSON で置いたもの（youten・shokate はアプリ用タブ、
    kijun は基準タブ、nodes はノード比較の記事タブ）。入れるのは件数・言葉・中央値などの集計だけ。
    記事の URL・タイトル・書き手は入れない（ノード比較の対応表とメモの列は読まない）。
    """
    from pathlib import Path
    raw = Path(raw_dir)
    out_dir = config.ROOT / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}

    rows = _by_header(_sheet_values(raw / "youten.json"), YOUTEN_COLS)
    shokate = _by_header(_sheet_values(raw / "shokate.json"),
                         {"time": "観測時刻", "tag": "大カテ", "pos": "順番", "word": "小カテ", "count": "件数"})
    kijun = _sheet_values(raw / "kijun.json")[1:]
    if rows:
        last = max(r["time"] for r in rows)
        lo = (m.T(last) - dt.timedelta(days=SHEET_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M")
        win = [r for r in rows if r["time"] >= lo]
        daily = [r for r in win if not r["memo"].startswith("基準")]
        newest_daily = max((r["time"] for r in daily), default=last)
        daily_tags = [r["face"].split(":", 1)[1] for r in win
                      if r["time"] == newest_daily and r["face"].startswith("tag:")]
        tags, unread = [], []
        for tag in dict.fromkeys(daily_tags):
            pts = sorted((r["time"], _count_in_memo(r["memo"]), r["cat"], _num(r["n"]) or 0)
                         for r in win if r["face"] == f"tag:{tag}")
            got = [p for p in pts if p[1] is not None]
            if pts and pts[-1][3] == 0:
                unread.append(tag)
            total = got[-1][1] if got else None
            per_day = None
            if len(got) >= 2 and m.hours(got[0][0], got[-1][0]) >= 20:
                per_day = round((got[-1][1] - got[0][1]) / (m.hours(got[0][0], got[-1][0]) / 24))
            cat = next((p[2] for p in reversed(pts) if p[2]), "")
            ws = [r for r in shokate if r["tag"] == tag and r["time"] >= lo]
            words = []
            if ws:
                lt = max(r["time"] for r in ws)
                first = {}
                for r in sorted(ws, key=lambda r: r["time"]):
                    first.setdefault(r["word"], (r["time"], _num(r["count"])))
                for r in sorted((r for r in ws if r["time"] == lt), key=lambda r: _num(r["pos"]) or 0):
                    cnt = _num(r["count"])
                    t0, c0 = first.get(r["word"], (lt, cnt))
                    h = m.hours(t0, lt)
                    words.append({"word": r["word"], "pos": _num(r["pos"]), "count": cnt,
                                  "per_day": round((cnt - c0) / (h / 24), 1) if cnt is not None and c0 is not None and h >= 20 else None})
            tags.append({"tag": tag, "total": total, "total_per_day": per_day, "category": cat, "words": words})
        snap = {
            "source": "スプレッドシート「note観測ログ（halsy）」の要点・小カテ（Apps Script、毎日18:44ごろ。ログアウト）",
            "from": min(r["time"] for r in win), "to": last, "copied_at": dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
            "note": ("直近7日の写し。小カテは各タグの人気のページに出る関連キーワード。件数は、その言葉のページに出ている記事の件数。"
                     "1日の増え方は、期間の最初の回と最後の回の差を日数で割ったもの。"
                     + (f"いちばん新しい回で記事を読み取れなかったタグ：{'、'.join(unread)}。" if unread else "")),
            "tags": tags,
        }
        base_rows = [r for r in rows if r["memo"].startswith("基準")]
        if base_rows:
            snap["baseline"] = _baseline(base_rows, kijun)
        (out_dir / "sheets_snapshot.json").write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        written["sheets_snapshot"] = len(tags)

    saiten = _sheet_values(raw / "saiten.json")
    if len(saiten) > 1:
        head = saiten[0]
        recs = [dict(zip(head, r + [""] * (len(head) - len(r)))) for r in saiten[1:] if r and r[0]]
        item_cols = [h for h in head if re.match(r"^(10|[1-9]) ", h)]
        bonus_cols = [h for h in head if re.match(r"^加[1-4] ", h)]
        weeks = {}
        for r in recs:
            wk = weeks.setdefault((r.get("週（土曜の日付）", ""), r.get("タグ", "")), [])
            wk.append(r)
        out_weeks = []
        for (week, tag), rs in sorted(weeks.items()):
            totals = [_num(r.get("合計")) for r in rs if _num(r.get("合計")) is not None]
            first5 = [sum((_num(r.get(c)) or 0) for c in item_cols[:5]) for r in rs]
            out_weeks.append({
                "week": week, "tag": tag, "n": len(rs),
                "featured": sum(1 for t in totals if t >= 5),
                "total_med": m.med(totals), "first5_med": m.med(first5),
                "item_rate": {c.split(" ", 1)[1]: round(sum((_num(r.get(c)) or 0) for r in rs) / len(rs), 2) for c in item_cols},
                "bonus_rate": {c.split(" ", 1)[1]: round(sum((_num(r.get(c)) or 0) for r in rs) / len(rs), 2) for c in bonus_cols},
                "bonus_med": m.med([_num(r.get("加点の合計")) for r in rs]),
                "top_rank_med": m.med([_num(r.get("順位")) for r in rs]),
            })
        sc = {"source": "スプレッドシート「note採点（halsy）」の採点タブ（週1回、人気の上位20＋新しく入った記事を10項目で採点）",
              "rubric_version": (_opt_config("scoring") or {}).get("version"), "copied_at": dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
              "weeks": out_weeks,
              "note": "基本の点は10項目のうちいくつあるか。前半5項目は本文採点（80本）と同じ。加点は書き方の4項目。記事のURL・タイトル・書き手は入れていない"}
        (out_dir / "scoring_weeks.json").write_text(json.dumps(sc, ensure_ascii=False, indent=1), encoding="utf-8")
        written["scoring_weeks"] = len(recs)

    nodes = _sheet_values(raw / "nodes.json")
    if len(nodes) > 1:
        head = nodes[0]
        col = {h: i for i, h in enumerate(head)}
        recs = [dict(zip(head, r + [""] * (len(head) - len(r)))) for r in nodes[1:] if r and r[0]]
        groups = {}
        for g in ("上位", "上位外"):
            rs = [r for r in recs if r.get("組") == g]
            if not rs:
                continue
            def medv(k):
                return m.med([_num(r.get(k)) for r in rs])
            groups[g] = {
                "n": len(rs),
                "dist2_pct": medv("距離2の割合"), "primary_pct": medv("一次情報の割合"),
                "free_chars": medv("無料部分の字数"), "nodes": medv("ノード数"), "q0": medv("距離0の問いの数"),
                "likes": medv("スキ（選んだとき）"),
                "paid_n": sum(1 for r in rs if (_num(r.get("価格（円、0は無料）")) or 0) > 0),
                "kinds": dict(sorted({k: sum(1 for r in rs if r.get("本の種類") == k) for k in {r.get("本の種類") for r in rs} if k}.items())),
                "promo": dict(sorted({k: sum(1 for r in rs if r.get("告知・販売の位置") == k) for k in {r.get("告知・販売の位置") for r in rs} if k}.items())),
            }
        tags = sorted({r.get("タグ") for r in recs if r.get("タグ")})
        days = sorted({r.get("分けた日") for r in recs if r.get("分けた日")})
        nc = {"source": "スプレッドシート「ノード比較（halsy）」の記事タブ。本文は保存せず、ノードの数と字数だけを記録したもの",
              "tags": tags, "from": days[0] if days else "", "to": days[-1] if days else "",
              "goal": 10, "groups": groups, "copied_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
              "definitions": {"dist2_pct": "タイトルの問いに答えも信じることも実行も変えない部分（距離2）が、無料部分の字数に占める割合",
                              "primary_pct": "書き手が自分で試した・測った・見た部分の割合",
                              "q0": "タイトルの問いに直接答える、別々の問いの数"}}
        (out_dir / "node_compare.json").write_text(json.dumps(nc, ensure_ascii=False, indent=1), encoding="utf-8")
        written["node_compare"] = len(recs)
    return written


def _baseline(base_rows, kijun) -> dict:
    """基準調査（多くのタグを同じ時刻に1回だけ見た回）の、タグごとの数字。"""
    last = max(r["time"] for r in base_rows)
    rs = [r for r in base_rows if r["time"] == last]
    groups = {r[0]: r[1] for r in kijun if r and len(r) > 1 and r[0]}
    items = []
    for tag in dict.fromkeys(r["face"].split(":", 1)[1] for r in rs if r["face"].startswith("tag:")):
        p = next((r for r in rs if r["face"] == f"tag:{tag}"), None)
        n = next((r for r in rs if r["face"] == f"tagnew:{tag}"), None)
        ok = bool(p and (_num(p["n"]) or 0) > 0)
        new_ok = bool(ok and n and (_num(n["n"]) or 0) > 0)
        items.append({
            "tag": tag, "group": groups.get(tag, ""), "read": ok,
            "total": _count_in_memo(p["memo"]) if p else None,
            "likes_med": _num(p["likes"]) if ok else None, "age_med_h": _num(p["age"]) if ok else None,
            "new_window_h": _num(n["window"]) if new_ok else None,
            "category": (p["cat"] if p else "") or "",
            # 新着の一覧が、人気の一覧と同じ記事で終わっている（同じ並びを読んでいる疑い）
            "suspect": bool(new_ok and p["bottom"] and p["bottom"] == n["bottom"]),
        })
    return {"run": last, "items": items,
            "note": "人気・新着それぞれの上位（50本まで）。スキと古さは人気の上位20本の中央値、新着の幅は新着の一覧がさかのぼる時間。"
                    "「読めず」はページは取れたが記事を読み取れなかったタグ。「疑い」は新着の一覧が人気の一覧と同じ記事で終わっていたタグ"}


# ---- 独立した Web アプリ（Cloudflare Pages に置く版） ----
# site/web/ を作る。この中身だけを GitHub のリポジトリに置き、Cloudflare Pages で公開する。
# 開ける人は Cloudflare Access（メールでのログイン）で絞る。中身は site/app/index.html と同じ集計だけ。

WEB_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="112" fill="#1f6f78"/>
<rect x="104" y="300" width="56" height="108" rx="14" fill="#e1eeee"/>
<rect x="190" y="236" width="56" height="172" rx="14" fill="#e1eeee"/>
<rect x="276" y="268" width="56" height="140" rx="14" fill="#e1eeee"/>
<circle cx="352" cy="176" r="70" fill="none" stroke="#ffffff" stroke-width="30"/>
<line x1="404" y1="228" x2="448" y2="272" stroke="#ffffff" stroke-width="34" stroke-linecap="round"/>
</svg>
"""

WEB_HEADERS = """/*
  X-Robots-Tag: noindex, nofollow
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Referrer-Policy: no-referrer
  Permissions-Policy: camera=(), microphone=(), geolocation=()
/
  Cache-Control: no-cache
/index.html
  Cache-Control: no-cache
"""

WEB_README = """# note狙い目観測（Web アプリ）

このリポジトリの中身は、PC の note-observatory で `py -m observatory export` が作った site/web/ の写しです。
手で直さないでください（次の自動更新で上書きされます）。

- 公開：Cloudflare Pages（このリポジトリにつなぐ。ビルドの命令は空、出力のフォルダは /）
- 開ける人：Cloudflare Access で、登録したメールアドレスだけ
- 中身：タグ・カテゴリの集計と、自分の記事だけ。他の書き手の名前・ID・記事タイトル・URL は入れていない（作るときに検査している）
"""


def _web_manifest() -> str:
    return json.dumps({
        "name": "note狙い目観測", "short_name": "狙い目観測", "lang": "ja",
        "start_url": "./", "scope": "./", "display": "standalone",
        "background_color": "#f3f4f0", "theme_color": "#1f6f78",
        "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"},
                  {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, ensure_ascii=False, indent=1)


def export_webapp() -> str | None:
    """site/app/index.html（データを埋め込んだページ）を、ふつうのブラウザで開ける1枚の HTML にして site/web/ に置く。"""
    src = config.SITE_DIR / "app" / "index.html"
    if not src.exists():
        return None
    frag = src.read_text(encoding="utf-8")
    frag = re.sub(r'<meta name="claude:[^"]*"[^>]*>\n?', "", frag)
    cut = frag.index("</style>") + len("</style>")
    head, body = frag[:cut], frag[cut:]
    page = ("<!doctype html>\n<html lang=\"ja\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">\n"
            "<meta name=\"robots\" content=\"noindex,nofollow\">\n"
            "<meta name=\"theme-color\" content=\"#1f6f78\">\n"
            "<link rel=\"manifest\" href=\"manifest.webmanifest\">\n"
            "<link rel=\"icon\" href=\"icon.svg\" type=\"image/svg+xml\">\n"
            "<link rel=\"apple-touch-icon\" href=\"icon-180.png\">\n"
            + head + "\n</head>\n<body>\n" + body.lstrip("\n") + "\n</body>\n</html>\n")
    out = config.SITE_DIR / "web"
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / "manifest.webmanifest").write_text(_web_manifest(), encoding="utf-8")
    (out / "icon.svg").write_text(WEB_ICON_SVG, encoding="utf-8")
    (out / "_headers").write_text(WEB_HEADERS, encoding="utf-8")
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (out / "README.md").write_text(WEB_README, encoding="utf-8")
    for png in ("icon-180.png", "icon-192.png", "icon-512.png"):   # 一度作った画像を web/webapp/ から写す
        p = config.ROOT / "web" / "webapp" / png
        if p.exists():
            (out / png).write_bytes(p.read_bytes())
    return str(out / "index.html")


def pick_candidates(con, tag: str, top: int = 20, new_n: int = 3, days: int = 7) -> dict:
    """週1回の採点で読む記事を選ぶ。人気の上位 top 本と、約 days 日前の人気の一覧になかった記事を new_n 本まで。

    自分の記事は入れない。本文は読まない（URL と順位を返すだけ）。
    """
    runs = [r[0] for r in con.execute(
        "SELECT DISTINCT run_at FROM obs WHERE kind='series' AND surface='popular' AND tag=? ORDER BY run_at", (tag,))]
    if not runs:
        return {"tag": tag, "run_at": None, "items": []}
    latest = runs[-1]
    target = m.T(latest) - dt.timedelta(days=days)
    prev = min(runs[:-1], key=lambda r: abs((m.T(r) - target).total_seconds()), default=None)
    cur = con.execute("SELECT rank, url, likes, published_at, price FROM obs WHERE kind='series' AND surface='popular' "
                      "AND tag=? AND run_at=? AND own=0 ORDER BY rank", (tag, latest)).fetchall()
    prev_urls = {r[0] for r in con.execute(
        "SELECT url FROM obs WHERE kind='series' AND surface='popular' AND tag=? AND run_at=?", (tag, prev))} if prev else set()
    items = []
    for r in cur:
        is_new = bool(prev) and r["url"] not in prev_urls
        if r["rank"] <= top:
            items.append({"rank": r["rank"], "url": r["url"], "likes": r["likes"], "published": r["published_at"],
                          "price": r["price"] or "", "new": is_new, "why": "上位"})
        elif is_new and sum(1 for x in items if x["why"] == "新しく入った") < new_n:
            items.append({"rank": r["rank"], "url": r["url"], "likes": r["likes"], "published": r["published_at"],
                          "price": r["price"] or "", "new": True, "why": "新しく入った"})
    return {"tag": tag, "run_at": latest, "prev_run": prev, "items": items}


def shelf_plan(con, n: int = 10) -> list[tuple[str, str]]:
    """次に見るカテゴリの棚。いちばん新しい観測が古い順に n 個（カテゴリのパスと名前）。"""
    rows = con.execute("SELECT tag, MAX(run_at) AS last, MAX(text) AS name FROM obs WHERE kind='shelf' "
                       "GROUP BY tag ORDER BY last, tag").fetchall()
    return [(r["tag"], r["name"] or r["tag"]) for r in rows[:n]]


def app_data(con) -> dict:
    p = payload(con, "members")
    seen, places = app_seen(con)
    cats = app_categories(con)
    tags = app_tags(con)
    own_cfg = {x["id"]: x for x in config.load("own_articles").get("articles", [])}
    own = []
    for a in p["own"]:
        apps = a["appearances"]
        best = min(apps, key=lambda x: (x["rank"] or 999)) if apps else None
        own.append({**a, "url_key": own_cfg.get(a["id"], {}).get("url_key", ""),
                    "best": ({"surface": best["surface"], "tag": best["tag"], "rank": best["rank"],
                              "section": best.get("section") or "", "category_name": best.get("category_name") or "",
                              "run_at": best["run_at"]} if best else None)})
    hyp = p["hypotheses"]
    hypotheses = [{
        "id": h["id"], "order": STATUS_ORDER.index(h["status"]) if h["status"] in STATUS_ORDER else 9,
        "status": h["status"], "status_label": STATUS_LABEL.get(h["status"], h["status"]),
        "claim": h["claim"], "evidence": h["evidence"],
    } for h in hyp.get("items", [])]
    return {
        "version": APP_VERSION,
        "generated_at": p["generated_at"],
        "coverage": p["coverage"],
        "gaps": p["gaps"],
        "score_defs": SCORE_DEFS,
        "categories": cats,
        "tags": tags,
        "entries": p["entries"],
        "trend": _trend(con),
        "rhythm": p["rhythm"],
        "hypotheses": {"updated_at": hyp.get("updated_at", ""), "items": hypotheses},
        "own": own,
        "body_scoring": body_scoring_summary(),
        "sheet": sheet_snapshot(),
        "nodes": node_compare(),
        "scoring": _opt_config("scoring"),           # 観測のタスクは config/scoring.json を取り込まない
        "scoring_weeks": _opt_json("scoring_weeks.json"),
        "salt": APP_SALT,
        "places": places,
        "seen": seen,
    }


def export_app() -> tuple[str, int]:
    """共有アプリ用のデータを site/app/data.json に書き出す。照合表の外に他の書き手の情報があれば止める。"""
    con = db.connect()
    data = app_data(con)
    seen = data.pop("seen")
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    bad = leaks(con, text, config.project()["own_account"])
    if bad:
        raise SystemExit("アプリのデータに他の書き手の情報が入っています。書き出しを止めました:\n  " + "\n  ".join(bad[:10]))
    if not all(re.fullmatch(r"[0-9a-f]{16}", k) for k in seen):
        raise SystemExit("照合表の鍵がハッシュの形になっていません。書き出しを止めました")
    data["seen"] = seen
    full = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    out = config.SITE_DIR / "app" / "data.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(full, encoding="utf-8")
    # 公開するページ（データを埋め込んだ1ファイル）。定時タスクがこのファイルをそのまま公開する。
    # 観測のタスクは web/app.html を取り込まないので、そのときはページを作らない
    src = config.ROOT / "web" / "app.html"
    if not src.exists():
        con.close()
        return str(out), len(seen)
    page = src.read_text(encoding="utf-8")
    tag = '<script id="obsData" type="application/json">'
    page = re.sub(re.escape(tag) + r".*?</script>\n?", "", page, flags=re.S)
    embed = tag + full.replace("</", "<\\/") + "</script>\n"
    if "<!--OBS_DATA-->" in page:
        page = page.replace("<!--OBS_DATA-->", embed, 1)
    else:
        i = page.find("<script")
        page = page[:i] + embed + page[i:]
    (out.parent / "index.html").write_text(page, encoding="utf-8")
    con.close()
    return str(out), len(seen)


# ---- Claude のダッシュボード（メンバー向け）用のデータ ----

SOURCE_LABEL = {"series": "定時観測", "scan": "タグ探し"}
PAGE_LABEL = {"tag": "キーワード", "hashtag": "ハッシュタグ", "": "—"}
STATUS_LABEL = {"refuted": "違った", "supported": "合っていた", "partial": "一部合っていた",
                "weak": "弱く合っていた", "testing": "検証中", "untested": "未確認"}
STATUS_ORDER = ["supported", "partial", "weak", "refuted", "testing", "untested"]
SURFACE_LABEL = {"popular": "人気", "new": "新着", "category": "カテゴリの欄", "search": "検索"}


def dash_datasets(con) -> dict[str, list[dict]]:
    p = payload(con, "members")

    shelves = [{
        "tag": s["tag"], "field": s["field"], "source": SOURCE_LABEL.get(s["source"], s["source"]),
        "page": PAGE_LABEL.get(s["page"], s["page"]), "total": s["total"], "runs": s["runs"],
        "last_run": s["last_run"], "pop_age_days": s["pop_age_days"], "pop_likes": s["pop_likes"],
        "new_per_day": s["new_per_day"], "qualified": s["qualified"],
    } for s in p["shelves"]]

    entries = []
    for e in p["entries"]:
        entries.append({
            "tag": e["tag"], "n": e["n"], "entered": e["entered"],
            "rate": round(e["entered"] / e["n"], 4) if e["n"] else None,
            "same_update": e["same_update"], "lag_h": e["lag_h"], "runs": e["runs"],
            "pub_from": e["pub_from"], "pub_to": e["pub_to"],
        })

    rhythm = [{"hour": b["hour"], "pairs": b["pairs"], "changed": b["changed"],
               "share": round(b["changed"] / b["pairs"], 4) if b["pairs"] else None}
              for b in p["rhythm"]["hours"]]

    hyp = p["hypotheses"]
    hypotheses = [{
        "id": h["id"], "order": STATUS_ORDER.index(h["status"]) if h["status"] in STATUS_ORDER else 9,
        "status": h["status"], "status_label": STATUS_LABEL.get(h["status"], h["status"]),
        "claim": h["claim"], "evidence": h["evidence"], "updated_at": hyp.get("updated_at", ""),
    } for h in hyp.get("items", [])]

    own = []
    for a in p["own"]:
        apps = a["appearances"]
        best = min(apps, key=lambda x: (x["rank"] or 999)) if apps else None
        own.append({
            "id": a["id"], "label": a["label"], "published_at": a["published_at"],
            # 「#」を付けずにつなぐ（他の書き手のタイトルが「#タグ名」だけのとき、漏れのチェックが誤って止まるため）
            "paid": "有料" if a["paid"] else "無料", "tags": " ".join(a["tags"]),
            "status": a["status"], "appearances": len(apps),
            "first_seen": apps[0]["run_at"] if apps else None,
            "best": (f"#{best['tag']} {SURFACE_LABEL.get(best['surface'], best['surface'])} {best['rank']}位" if best else None),
        })

    coverage = [{"kind": SOURCE_LABEL.get(k, k), "runs": v["runs"], "from": v["from"], "to": v["to"], "tags": v["tags"]}
                for k, v in p["coverage"].items()]

    trend = _trend(con)

    return {"shelves": shelves, "entries": entries, "rhythm": rhythm, "hypotheses": hypotheses,
            "own": own, "coverage": coverage, "trend": trend}


def export_dash() -> dict:
    """Claude のダッシュボード（メンバー向け）用のデータを site/dash に書き出す。他の書き手の情報が入っていたら止める。"""
    con = db.connect()
    data = dash_datasets(con)
    out_dir = config.SITE_DIR / "dash"
    out_dir.mkdir(parents=True, exist_ok=True)
    own_account = config.project()["own_account"]
    written = {}
    for name, rows in data.items():
        text = json.dumps(rows, ensure_ascii=False, indent=0)
        bad = leaks(con, text, own_account)
        if bad:
            raise SystemExit(f"{name}.json に他の書き手の情報が入っています。書き出しを止めました:\n  " + "\n  ".join(bad[:10]))
        path = out_dir / f"{name}.json"
        path.write_text(text, encoding="utf-8")
        written[name] = (str(path), len(rows))
    stamp = {"generated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds"),
             "files": {k: v[1] for k, v in written.items()}}
    (out_dir / "_manifest.json").write_text(json.dumps(stamp, ensure_ascii=False, indent=1), encoding="utf-8")
    con.close()
    return written
