"""指標の計算。定義は docs/metrics.md に書いてある。数字を変えるときは、そちらも直す。"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
import statistics as st
from collections import defaultdict

QUALIFIED = re.compile(r"医師|Dr\.|ドクター|看護師|薬剤師|管理栄養士|保健師|診断士|税理士|会計士|社労士|弁護士|行政書士|司法書士")


def T(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def hours(a: str, b: str) -> float:
    """a から b までの時間（時間単位）。"""
    return (T(b) - T(a)).total_seconds() / 3600


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def lists_by_run(con: sqlite3.Connection, kind: str, surface: str, tag: str,
                 run_to: str | None = None, run_from: str | None = None) -> dict[str, list[sqlite3.Row]]:
    """回ごとの一覧（順位順）。"""
    q = "SELECT * FROM obs WHERE kind=? AND surface=? AND tag=?"
    args: list = [kind, surface, tag]
    if run_from:
        q += " AND run_at>=?"
        args.append(run_from)
    if run_to:
        q += " AND run_at<=?"
        args.append(run_to)
    q += " ORDER BY run_at, rank"
    out: dict[str, list] = defaultdict(list)
    for r in con.execute(q, args):
        out[r["run_at"]].append(r)
    return out


def shelf(con, tag: str, kind: str = "series", top_n: int = 20, depth_n: int = 40,
          run_to: str | None = None, run_from: str | None = None, strict_depth: bool = False) -> dict:
    """タグの棚の性格。人気の古さとスキ、新着の深さ（n本目の古さ）と1日の新着の目安。

    回ごとに出して、回をまたいだ中央値をとる。
    strict_depth=True のときは、新着が depth_n 本に満たない回を数えない。
    """
    pop = lists_by_run(con, kind, "popular", tag, run_to, run_from)
    new = lists_by_run(con, kind, "new", tag, run_to, run_from)
    ages, likes, quals = [], [], []
    for run, rows in pop.items():
        top = rows[:top_n]
        a = [hours(r["published_at"], run) / 24 for r in top if r["published_at"]]
        k = [r["likes"] for r in top if r["likes"] is not None]
        if a:
            ages.append(st.median(a))
        if k:
            likes.append(st.median(k))
        if kind == "scan":
            quals.append(sum(1 for r in top if QUALIFIED.search(r["text"] or "")))
    depth, per_day = [], []
    for run, rows in new.items():
        rows = [r for r in rows if r["published_at"]]
        if not rows or (strict_depth and len(rows) < depth_n):
            continue
        n = min(depth_n, len(rows))
        h = hours(rows[n - 1]["published_at"], run)
        if h <= 0:
            continue
        depth.append(h)
        per_day.append(n / h * 24)
    latest_rows = [r for rows in list(pop.values())[-1:] for r in rows]
    page = next((r["page"] for r in latest_rows if r["page"]), "")
    total = next((r["total"] for r in latest_rows if r["total"] is not None), None)
    runs = sorted(set(pop) | set(new))
    return {
        "tag": tag,
        "kind": kind,
        "runs": len(runs),
        "first_run": runs[0] if runs else None,
        "last_run": runs[-1] if runs else None,
        "page": page,
        "total": total,
        "pop_age_days": med(ages),
        "pop_likes": med(likes),
        "new_depth_h": med(depth),
        "new_per_day": med(per_day),
        "qualified": (round(st.mean(quals), 1) if quals else None),
    }


def entry(con, tag: str, pub_from: str, pub_to: str, kind: str = "series") -> dict:
    """新着から人気へ入った割合。

    対象は、新着で見かけた記事のうち、公開が [pub_from, pub_to) のもの。
    そのうち、どこかの回で人気に出たものを「入った」と数える。
    same_update は、新着に初めて出た回と同じ回で人気にも出た本数。
    """
    first_new: dict[str, tuple[str, str]] = {}
    for r in con.execute(
            "SELECT url, run_at, published_at FROM obs WHERE kind=? AND surface='new' AND tag=? "
            "AND published_at>=? AND published_at<? ORDER BY run_at", (kind, tag, pub_from, pub_to)):
        first_new.setdefault(r["url"], (r["run_at"], r["published_at"]))
    first_pop: dict[str, str] = {}
    for r in con.execute("SELECT url, run_at FROM obs WHERE kind=? AND surface='popular' AND tag=? ORDER BY run_at",
                         (kind, tag)):
        first_pop.setdefault(r["url"], r["run_at"])
    entered = [u for u in first_new if u in first_pop]
    same = [u for u in entered if first_pop[u] <= first_new[u][0]]
    lag = [hours(first_new[u][1], first_pop[u]) for u in entered]
    n = len(first_new)
    return {
        "tag": tag,
        "pub_from": pub_from,
        "pub_to": pub_to,
        "n": n,
        "entered": len(entered),
        "rate": (len(entered) / n if n else None),
        "same_update": len(same),
        "lag_h": med(lag),
    }


def unique_new(con, tag: str, pub_from: str, pub_to: str, kind: str = "series") -> int:
    """新着で見かけた記事のうち、公開が [pub_from, pub_to) の本数（重なりなし）。"""
    return con.execute(
        "SELECT COUNT(DISTINCT url) FROM obs WHERE kind=? AND surface='new' AND tag=? "
        "AND published_at>=? AND published_at<?", (kind, tag, pub_from, pub_to)).fetchone()[0]


def update_rhythm(con, tags: list[str], kind: str = "series", top_k: int = 10, max_gap_h: float = 1.5) -> list[dict]:
    """新着の上位 top_k 本の顔ぶれが、前の回から変わったかを、時刻（時台）ごとに数える。

    前の回との間が max_gap_h 時間以内の組だけを使う（1時間おきに見ていた期間が対象）。
    """
    per_tag = {t: {run: [r["url"] for r in rows[:top_k]] for run, rows in lists_by_run(con, kind, "new", t).items()}
               for t in tags}
    runs = sorted({run for d in per_tag.values() for run in d})
    buckets = {h: {"hour": h, "pairs": 0, "changed": 0} for h in range(24)}
    for a, b in zip(runs, runs[1:]):
        if hours(a, b) > max_gap_h:
            continue
        compared = [(d[a], d[b]) for d in per_tag.values() if a in d and b in d]
        if not compared:
            continue
        hb = T(b).hour
        buckets[hb]["pairs"] += 1
        if any(set(x) != set(y) for x, y in compared):
            buckets[hb]["changed"] += 1
    return list(buckets.values())


def own_appearances(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT kind, run_at, surface, tag, section, text, rank, url, title, published_at FROM obs WHERE own=1 "
        "ORDER BY run_at, tag")]


# ---- カテゴリページの棚（kind=shelf） ----

def _q(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))]


def _pct(n, d):
    return round(100 * n / d) if d else None


def has_article_id(url: str) -> bool:
    """記事番号（/n/n…）のある URL か。11/1朝のカテゴリ一巡は記事番号を取っておらず、
    URL の代わりに「/書き手/x/（書き手とタイトルから作った印）」が入っている。"""
    return "/n/" in (url or "")


def kept_urls(cur_rows, prev_rows) -> tuple[set, str]:
    """今回の記事のうち、前の回にもあった記事（今回の URL の集合）と、照らし合わせ方。

    どちらの回にも記事番号があれば URL で、片方にでもなければ書き手で照らし合わせる
    （同じ棚に同じ書き手の別の記事が出ることは少ないが、あれば多めに数える）。
    """
    cur = {r["url"]: r for r in cur_rows}
    if all(has_article_id(u) for u in cur) and all(has_article_id(r["url"]) for r in prev_rows):
        purls = {r["url"] for r in prev_rows}
        return {u for u in cur if u in purls}, "url"
    pauth = {r["author"] for r in prev_rows}
    return {u for u, r in cur.items() if r["author"] in pauth}, "author"


# ---- 順位と点数（共有アプリ）。定義は docs/metrics.md「狙い目の点数」 ----

def pct_rank(values, x) -> float | None:
    """x が values の中で、下から何%の位置か（0〜100）。同じ値がいくつもあるときは、その真ん中の位置。

    values に x と同じ値がないとき（記事の診断で入れた数など）も、あいだの位置を返す。
    """
    vs = [v for v in values if v is not None]
    if x is None or len(vs) < 2:
        return None
    less = sum(1 for v in vs if v < x)
    eq = sum(1 for v in vs if v == x)
    pos = less + (eq - 1) / 2 if eq else less - 0.5
    return round(max(0.0, min(100.0, 100 * pos / (len(vs) - 1))), 1)


def robust_z(values, x) -> float | None:
    """中央値と、中央値からのずれの中央値（MAD）で測った、x の外れ具合。|z|≥2.5 を「突出」とする。"""
    vs = [v for v in values if v is not None]
    if x is None or len(vs) < 8:
        return None
    mid = st.median(vs)
    mad = st.median([abs(v - mid) for v in vs])
    if mad == 0:
        return None
    return round((x - mid) / (1.4826 * mad), 2)


def add_scores(rows: list[dict], axes: dict, weights: dict, ref: list[dict] | None = None) -> dict:
    """rows に、軸ごとの点数（0〜100）と総合点、順位を書き足す。

    axes    … {軸: [(項目, +1 なら大きいほど良い / -1 なら小さいほど良い), ...]}
    weights … {軸: 総合点での重み}
    ref     … 分布の基準にする行（同じ棚を2回数えないように、重複を除いたもの）。省略すると rows 全部。

    軸の点数は、項目ごとの「下から何%の位置」（良い向きにそろえたもの）の平均。
    値がない項目は飛ばし、残りで平均する。総合点は、点数のある軸だけの重みつき平均。
    順位は、基準の行の中で自分より点数が高い行の数＋1（同点は同じ順位）。
    返り値は、点数ごとの「何行の中の順位か」（基準の行のうち点数のある行の数）。
    """
    ref = rows if ref is None else ref
    for r in rows:
        r["scores"] = {}
        for ax, comps in axes.items():
            ps = []
            for f, d in comps:
                p = pct_rank([x.get(f) for x in ref], r.get(f))
                if p is not None:
                    ps.append(p if d > 0 else 100 - p)
            r["scores"][ax] = round(st.mean(ps)) if ps else None
        got = [(w, r["scores"][a]) for a, w in weights.items() if r["scores"].get(a) is not None]
        r["scores"]["total"] = round(sum(w * v for w, v in got) / sum(w for w, _ in got)) if got else None
    keys = list(axes) + ["total"]
    for r in rows:
        r["ranks"] = {}
        for k in keys:
            v = r["scores"].get(k)
            if v is None:
                r["ranks"][k] = None
                continue
            r["ranks"][k] = 1 + sum(1 for x in ref if (x.get("scores") or {}).get(k) is not None and x["scores"][k] > v)
    return {k: sum(1 for x in ref if (x.get("scores") or {}).get(k) is not None) for k in keys}


def shelf_categories(con) -> list[dict]:
    """カテゴリページの棚の入口。カテゴリごとに、いちばん新しい回を集計し、その前の回と比べる。

    同じ記事が2つの棚に出ることがあるので、カテゴリ全体の数字は記事の重なりを除いて数える。
    公開からの時間は、カードの「◯時間前・◯日前」からの目安（1日を超えると日単位で粗くなる）。
    """
    runs_by_cat: dict[str, list[str]] = defaultdict(list)
    for r in con.execute("SELECT tag, run_at FROM obs WHERE kind='shelf' GROUP BY tag, run_at ORDER BY tag, run_at"):
        runs_by_cat[r["tag"]].append(r["run_at"])
    out = []
    for cat, runs in runs_by_cat.items():
        last = runs[-1]
        prev = runs[-2] if len(runs) > 1 else None
        rows = con.execute("SELECT * FROM obs WHERE kind='shelf' AND tag=? AND run_at=? ORDER BY id",
                           (cat, last)).fetchall()
        if not rows:
            continue
        uniq = {}
        sections: dict[str, list] = {}
        for r in rows:
            uniq.setdefault(r["url"], r)
            sections.setdefault(r["section"], []).append(r)

        def age(r):
            return hours(r["published_at"], last) if r["published_at"] else None

        arts = list(uniq.values())
        likes = [r["likes"] for r in arts]
        ages = [age(r) for r in arts]
        sec_out = []
        for kw, rs in sections.items():
            lk = [r["likes"] for r in rs]
            sec_out.append({
                "word": kw, "n": len(rs), "likes_min": _q(lk, 0), "likes_med": _q(lk, .5), "likes_max": _q(lk, 1),
                "age_med_h": _q([age(r) for r in rs], .5), "paid": sum(1 for r in rs if r["price"]),
                "under10": sum(1 for x in lk if x is not None and x < 10),
            })
        item = {
            "category": cat, "name": rows[0]["text"] or cat, "runs": len(runs), "last_run": last, "prev_run": prev,
            "articles": len(arts), "writers": len({r["author"] for r in arts}),
            "likes_min": _q(likes, 0), "likes_q1": _q(likes, .25), "likes_med": _q(likes, .5), "likes_max": _q(likes, 1),
            "under10_pct": _pct(sum(1 for x in likes if x is not None and x < 10), len(arts)),
            "fresh24_pct": _pct(sum(1 for a in ages if a is not None and a < 24), len(arts)),
            "age_med_h": _q(ages, .5),
            "paid_pct": _pct(sum(1 for r in arts if r["price"]), len(arts)),
            "sections": sec_out,
            "kept_pct": None, "hours_since_prev": None, "new_words": [], "gone_words": [],
        }
        if prev:
            prow = con.execute("SELECT url, author, section FROM obs WHERE kind='shelf' AND tag=? AND run_at=?",
                               (cat, prev)).fetchall()
            pwords = {r["section"] for r in prow}
            item["kept_pct"] = _pct(len(kept_urls(arts, prow)[0]), len(uniq))
            item["hours_since_prev"] = round(hours(prev, last), 1)
            item["new_words"] = [w for w in sections if w not in pwords]
            item["gone_words"] = sorted(pwords - set(sections))
        out.append(item)
    return sorted(out, key=lambda x: (x["likes_med"] if x["likes_med"] is not None else 9999, x["category"]))


def article_categories(con) -> dict[str, dict]:
    """記事ごとの、いちばん新しく見た分類（パンくず）とハッシュタグ。"""
    out = {}
    for r in con.execute("SELECT * FROM article_meta ORDER BY checked_at"):
        out[r["url"]] = dict(r)
    return out


def coverage(con) -> dict:
    """観測した範囲。shelf の tags はカテゴリの数。"""
    out = {}
    for r in con.execute("SELECT kind, COUNT(DISTINCT run_at) n, MIN(run_at) a, MAX(run_at) b, "
                         "COUNT(DISTINCT tag) tags FROM obs GROUP BY kind"):
        out[r["kind"]] = {"runs": r["n"], "from": r["a"], "to": r["b"], "tags": r["tags"]}
    return out


def tags_of(con, kind: str, surface: str = "popular") -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT tag FROM obs WHERE kind=? AND surface=? GROUP BY tag ORDER BY MIN(run_at), tag", (kind, surface))]


def gaps(con, kind: str = "series", min_gap_h: float = 6) -> list[dict]:
    """観測が min_gap_h 時間以上空いたところ。"""
    runs = [r[0] for r in con.execute("SELECT DISTINCT run_at FROM obs WHERE kind=? ORDER BY run_at", (kind,))]
    return [{"from": a, "to": b, "hours": round(hours(a, b), 1)} for a, b in zip(runs, runs[1:])
            if hours(a, b) >= min_gap_h]


# ---- 一次情報の点：本文から合図を数える（定義は config/scoring.json、docs/metrics.md） ----

_DATE_PATTERNS = [
    (re.compile(r"(20\d\d)年(\d{1,2})月(?:(\d{1,2})日)?"), "ymd"),
    (re.compile(r"(20\d\d)[-/](\d{1,2})[-/](\d{1,2})"), "ymd"),
    (re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日"), "md"),
]


def _sentences(text: str) -> list[str]:
    text = text.replace("\r\n", "\n")
    out = []
    for para in re.split(r"\n\s*\n|\n", text):
        for s in re.split(r"(?<=[。！？!?])", para):
            s = s.strip()
            if s:
                out.append(s)
    return out


def _dates_in(text: str, ref: dt.date | None) -> list[dt.date]:
    found = []
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                if kind == "ymd":
                    y, mo = int(m.group(1)), int(m.group(2))
                    d = int(m.group(3)) if m.group(3) else 1
                else:
                    mo, d = int(m.group(1)), int(m.group(2))
                    y = (ref or dt.date.today()).year
                    if ref and dt.date(y, mo, d) > ref:   # 公開日より後の月日は前の年
                        y -= 1
                found.append(dt.date(y, mo, d))
            except ValueError:
                continue
    return found


def score_text(text: str, rubric: dict, published: str | None = None) -> dict:
    """本文（無料部分）から、10項目ごとの合図の数と、7W1H の有無を出す。

    返すのは合図の数と「ありそう」の目安だけ。点数の判定は人か Claude がする。本文は返さない。
    """
    sents = _sentences(text)
    body = "\n".join(sents)
    chars = len(re.sub(r"\s", "", text))
    ref = None
    if published:
        try:
            ref = dt.date.fromisoformat(published[:10].replace("/", "-"))
        except ValueError:
            ref = None
    items = []
    for it in rubric.get("items", []):
        rxs = [re.compile(p, re.M) for p in it["patterns"]]
        hit_sents = [s for s in sents if any(r.search(s) for r in rxs)]
        n = len(hit_sents)
        if it["id"] == "s10":
            ds = _dates_in(body, ref)
            base = ref or dt.date.today()
            recent = [d for d in ds if 0 <= (base - d).days <= 92]
            items.append({"id": it["id"], "key": it["key"], "n": len(ds), "likely": bool(recent),
                          "detail": (f"いちばん新しい日付 {max(ds).isoformat()}" if ds else "日付なし")
                                    + ("" if ref else "（公開日が無いので今日から数えた）")})
            continue
        if it["id"] == "s9":
            ext = n
            linked = len([s for s in hit_sents if re.search(r"https?://|20\d\d年|\d{1,2}月\d{1,2}日|によると|によれば", s)])
            items.append({"id": it["id"], "key": it["key"], "n": ext, "likely": ext == 0 or linked >= 2 or linked * 2 >= ext,
                          "detail": "外の情報の言い回しなし" if ext == 0 else f"外の情報らしい文 {ext}、名前・日付・リンク付き {linked}"})
            continue
        items.append({"id": it["id"], "key": it["key"], "n": n, "likely": n >= it.get("min", 1),
                      "detail": f"合図のある文 {n}（目安 {it.get('min', 1)}以上）"})
    w = []
    for q in rubric.get("w7h1", []):
        rxs = [re.compile(p, re.M) for p in q["patterns"]]
        w.append({"key": q["key"], "found": any(r.search(s) for s in sents for r in rxs)})
    numbers = len(re.findall(r"\d[\d,.]*\s*(?:回|件|本|人|名|字|文字|分|時間|日|週|円|万円|%|％|割|倍|位)", body))
    # 書き方の加点の合図（判定は人か Claude）
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    heads = [ln for ln in lines if (ln.startswith("#") or (len(ln) <= 30 and not re.search(r"[。！？!?、,]$", ln) and not ln.startswith(("・", "-", "*")))) and len(ln) >= 4]
    bullets = [ln for ln in lines if re.match(r"^([・\-*•]|[0-9０-９]+[.．、）)]|[①②③④⑤⑥⑦⑧⑨])", ln)]
    avg_len = round(sum(len(x) for x in sents) / len(sents), 1) if sents else 0
    figures = len(re.findall(r"(図|表|スクショ|スクリーンショット|画像|グラフ)[0-9０-９]?[:：）)]|（(図|表|写真)[0-9０-９]*）", body))
    first3 = [x for x in sents if not re.match(r"^(皆様|みなさん|こんにちは|おはよう|こんばんは|本日は)", x)][:3]
    w_all = all(x["found"] for x in w)
    bonus = {
        "7W1H": {"found": sum(1 for x in w if x["found"]), "of": len(w), "likely": w_all,
                 "detail": f"自動で分かる{len(w)}つのうち {sum(1 for x in w if x['found'])}（「何を」は人が判断）"},
        "書き出し": {"lead_chars": sum(len(x) for x in first3), "likely": None,
                 "detail": "あいさつの後の3文がタイトルの問いに答えているかは、人か Claude が判断"},
        "独自性": {"tables": sum(1 for ln in lines if ln.count("|") >= 2 or ln.count("｜") >= 2), "likely": None,
                "detail": "自分で作った表・比べ・名前付けがあるかは、人か Claude が判断"},
        "読みやすさ": {"headings": len(heads), "bullets": len(bullets), "avg_sentence": avg_len, "figures": figures,
                  "likely": sum([len(heads) >= 2, len(bullets) >= 3, avg_len <= 50, figures >= 1]) >= 2,
                  "detail": f"見出しらしい行 {len(heads)}・箇条書き {len(bullets)}・1文の平均 {avg_len}字・図表の印 {figures}"},
    }
    return {
        "bonus": bonus,
        "chars": chars, "sentences": len(sents), "numbers_with_unit": numbers,
        "dates": len(_dates_in(body, ref)), "links": len(re.findall(r"https?://", body)),
        "items": items, "likely_total": sum(1 for i in items if i["likely"]), "w7h1": w,
        "note": "合図の数は目安。点数は10項目を人か Claude が判定する。本文は保存していない",
    }
