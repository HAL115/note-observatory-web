"""メンバー版に他の書き手の情報が入らないことを確かめる。

    py -m unittest discover tests
"""

import json
import unittest

from observatory import build, config, db


class MembersPrivacy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = db.connect()
        cls.own = config.project()["own_account"]

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_members_has_no_other_writers(self):
        html = build.render(build.payload(self.con, "members"))
        self.assertEqual(build.leaks(self.con, html, self.own), [])

    def test_members_has_no_latest_popular(self):
        self.assertNotIn("latest_popular", build.payload(self.con, "members"))

    def test_detector_catches_planted_url_and_title(self):
        row = self.con.execute("SELECT url, title FROM obs WHERE own=0 AND length(title)>=10 LIMIT 1").fetchone()
        if row is None:
            self.skipTest("データがない")
        html = build.render(build.payload(self.con, "members"))
        planted = html.replace("</body>", f"<p>https://note.com{row['url']} {row['title']}</p></body>")
        found = build.leaks(self.con, planted, self.own)
        self.assertTrue(any(f.startswith("URL") for f in found))
        self.assertTrue(any(f.startswith("タイトル") for f in found))

    def test_private_keeps_latest_popular(self):
        self.assertIn("latest_popular", build.payload(self.con, "private"))

    def test_categories_have_no_articles(self):
        """カテゴリの棚の集計には、記事のURL・書き手・タイトルを入れない。"""
        for c in build.payload(self.con, "members")["categories"]:
            text = repr(c)
            self.assertNotIn("/n/n", text)
            for w in c["sections"]:
                self.assertEqual(set(w), {"word", "n", "likes_min", "likes_med", "likes_max", "age_med_h", "paid", "under10"})

    def test_detector_catches_article_meta_title(self):
        row = self.con.execute("SELECT title FROM article_meta WHERE own=0 AND length(title)>=10 LIMIT 1").fetchone()
        if row is None:
            self.skipTest("記事の分類のデータがない")
        html = build.render(build.payload(self.con, "members")).replace("</body>", f"<p>{row['title']}</p></body>")
        self.assertTrue(any(f.startswith("タイトル") for f in build.leaks(self.con, html, self.own)))


class ShelfCsv(unittest.TestCase):
    def test_shelf_rows_skip_note_banner_and_estimate_published(self):
        from observatory import ingest
        text = ("observed_at_jst,category,category_name,section,rank,url,likes,age_h,paid,title\n"
                "2026-11-01 16:53,business,ビジネス,事業承継,1,/someone/n/n0123456789ab,21,8,0,テスト\n"
                "2026-11-01 16:53,business,ビジネス,AI活用,5,/info/n/nea1b96233fbf,0,,0,創作を楽しもう！\n")
        kind, rows, skipped = ingest.rows_from_csv(text, "aitohal")
        self.assertEqual(kind, "shelf")
        self.assertEqual(len(rows), 1)
        self.assertEqual(skipped, 1)
        self.assertEqual(rows[0]["published_at"], "2026-11-01 08:53")
        self.assertEqual(rows[0]["surface"], "category")
        self.assertEqual(rows[0]["section"], "事業承継")

    def test_article_csv(self):
        from observatory import ingest
        text = ("checked_at_jst,url,category_path,category_name,hashtags,published_jst,title\n"
                "2026-11-01 11:05,/aitohal/n/n99d46a1c58ae,,ビジネス>収益化,note|タグ観測,2026-10-29 18:11,自分の記事\n")
        kind, rows, _ = ingest.rows_from_csv(text, "aitohal")
        self.assertEqual(kind, "article")
        self.assertEqual(rows[0]["own"], 1)
        self.assertEqual(rows[0]["category_name"], "ビジネス＞収益化")


class Scores(unittest.TestCase):
    """狙い目の点数（docs/metrics.md「狙い目の点数」）。"""

    def test_pct_rank(self):
        from observatory import metrics as m
        vs = [1, 2, 3, 4, 5]
        self.assertEqual(m.pct_rank(vs, 1), 0)
        self.assertEqual(m.pct_rank(vs, 5), 100)
        self.assertEqual(m.pct_rank(vs, 3), 50)
        self.assertEqual(m.pct_rank([1, 2, 2, 3], 2), 50)   # 同じ値は真ん中の位置
        self.assertEqual(m.pct_rank(vs, 0), 0)               # ない値も0〜100に収める
        self.assertIsNone(m.pct_rank([1], 1))

    def test_add_scores_direction_and_rank(self):
        from observatory import metrics as m
        rows = [{"k": "a", "x": 1, "y": 10}, {"k": "b", "x": 2, "y": 20}, {"k": "c", "x": 3, "y": None}]
        n = m.add_scores(rows, {"low_x": [("x", -1)], "high_y": [("y", +1)]}, {"low_x": 0.5, "high_y": 0.5})
        a, b, c = rows
        self.assertEqual(a["scores"]["low_x"], 100)      # x が小さいほど良い
        self.assertEqual(c["scores"]["low_x"], 0)
        self.assertEqual(b["scores"]["high_y"], 100)
        self.assertIsNone(c["scores"]["high_y"])         # 値がなければ点数なし
        self.assertEqual(c["scores"]["total"], 0)        # 点数のある軸だけで総合点
        self.assertEqual(a["ranks"]["low_x"], 1)
        self.assertEqual(n["high_y"], 2)
        for r in rows:
            for v in r["scores"].values():
                self.assertTrue(v is None or 0 <= v <= 100)

    def test_robust_z_flags_outlier(self):
        from observatory import metrics as m
        vs = [10, 11, 12, 13, 14, 15, 16, 17, 100]
        self.assertGreater(m.robust_z(vs, 100), 2.5)
        self.assertLess(abs(m.robust_z(vs, 14)), 1)

    def test_kept_by_author_when_no_article_id(self):
        from observatory import metrics as m
        prev = [{"url": "/a/x/1", "author": "a"}, {"url": "/b/x/2", "author": "b"}]
        cur = [{"url": "/a/n/n0123456789ab", "author": "a"}, {"url": "/c/n/n0123456789ac", "author": "c"}]
        kept, how = m.kept_urls(cur, prev)
        self.assertEqual(how, "author")
        self.assertEqual(kept, {"/a/n/n0123456789ab"})
        kept, how = m.kept_urls(cur, [{"url": "/a/n/n0123456789ab", "author": "a"}])
        self.assertEqual((kept, how), ({"/a/n/n0123456789ab"}, "url"))


class AppData(unittest.TestCase):
    """共有アプリのデータに、他の書き手を特定できるものが入らないこと。"""

    @classmethod
    def setUpClass(cls):
        cls.con = db.connect()
        cls.data = build.app_data(cls.con)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_no_leaks_outside_seen(self):
        import json
        d = dict(self.data)
        d.pop("seen")
        text = json.dumps(d, ensure_ascii=False)
        self.assertEqual(build.leaks(self.con, text, config.project()["own_account"]), [])

    def test_dist_is_numbers_only(self):
        for c in self.data["categories"]["items"]:
            for s in [c["base"]] + ([c["latest"]] if c["latest"] else []):
                for d in s["dist"]:
                    self.assertEqual(len(d), 3)
                    self.assertTrue(all(v is None or isinstance(v, (int, float)) for v in d))
                for w in s["words"]:
                    self.assertNotIn("author", w)
                    self.assertNotIn("url", w)

    def test_scores_in_range_and_ranks_consistent(self):
        cats = self.data["categories"]
        for c in cats["items"]:
            for k, v in c["scores"].items():
                self.assertTrue(v is None or 0 <= v <= 100, (c["path"], k, v))
            r = c["ranks"]["total"]
            if r is not None:
                self.assertLessEqual(r, cats["rank_of"]["total"] + 1)
        top = [c for c in cats["items"] if c["in_ref"] and c["ranks"]["total"] == 1]
        self.assertTrue(top)
        best = max(c["scores"]["total"] for c in cats["items"] if c["in_ref"])
        self.assertEqual(top[0]["scores"]["total"], best)

    def test_seen_keys_are_hashes(self):
        import re
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{16}", k) for k in self.data["seen"]))


class RollingBase(unittest.TestCase):
    """一巡の回が古くなったら、直近の巡回（1回10カテゴリずつ）で比べる。"""

    def test_rolling_when_sweep_is_old(self):
        import shutil
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "o.sqlite"
            shutil.copy(config.DB_PATH, path)
            con = db.connect(path)
            before = build.app_categories(con)
            self.assertEqual(before["base_kind"], "sweep")
            base = before["base_run"]
            cats = [r[0] for r in con.execute(
                "SELECT DISTINCT tag FROM obs WHERE kind='shelf' AND run_at=? ORDER BY tag", (base,))]
            cols = [r[1] for r in con.execute("PRAGMA table_info(obs)") if r[1] != "id"]
            for i, cat in enumerate(cats):
                shift = f"+{9 + i // 10} days"
                sel = ", ".join(
                    f"strftime('%Y-%m-%d %H:%M', datetime({c}, '{shift}'))" if c in ("observed_at", "run_at", "published_at") else c
                    for c in cols)
                con.execute(f"INSERT INTO obs ({', '.join(cols)}) SELECT {sel} FROM obs "
                            "WHERE kind='shelf' AND run_at=? AND tag=?", (base, cat))
            after = build.app_categories(con)
            self.assertEqual(after["base_kind"], "rolling")
            self.assertGreater(after["base_from"], base)
            self.assertLessEqual(after["base_from"], after["base_run"])
            used = {c["path"]: c["base"]["run"] for c in after["items"] if c["base_is_sweep"]}
            self.assertGreaterEqual(len(used), 0.8 * len(cats))
            self.assertTrue(all(r > base for r in used.values()))
            self.assertIsNotNone(after["overall"])
            # 次に見る棚は、いちばん古いものから
            plan = build.shelf_plan(con, 10)
            self.assertEqual(len(plan), 10)
            con.close()


class SheetsImport(unittest.TestCase):
    """スプレッドシートの写しから作る集計に、記事の URL・タイトル・メモが入らないこと。"""

    def test_import_keeps_aggregates_only(self):
        import json
        import shutil
        import tempfile
        from pathlib import Path
        root = config.ROOT
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d) / "raw"
            raw.mkdir()
            youten = [["観測時刻（JST）", "面", "本数", "一番下の公開時刻", "メモ", "スキ中央値（上位20）",
                       "公開からの時間・中央値（上位20、時間）", "新着の時間幅（時間）", "カテゴリ"],
                      ["2026-11-01 18:44", "tag:あ", "40", "2026-10-30 10:00", "件数 1000", "10", "20", "", "X＞Y"],
                      ["2026-11-05 18:44", "tag:あ", "40", "2026-11-03 10:00", "件数 1100", "12", "22", "", "X＞Y"],
                      ["2026-11-05 18:44", "tag:い", "0", "1899-12-30 00:00", "記事0本（文字数 1）"],
                      ["2026-11-04 18:50", "tag:う", "40", "2026-11-01 10:00", "基準／件数 500", "5", "30", "", "Z"],
                      ["2026-11-04 18:50", "tagnew:う", "40", "2026-11-01 10:00", "基準／件数 500", "1", "2", "9", "Z"]]
            shokate = [["観測時刻（JST）", "大カテ", "順番", "小カテ", "件数"],
                       ["2026-11-01 18:44", "あ", "1", "ことば", "100"], ["2026-11-05 18:44", "あ", "1", "ことば", "120"]]
            nodes = [["コード", "組", "タグ", "スキ（選んだとき）", "価格（円、0は無料）", "本の種類", "無料部分の字数", "ノード数",
                      "距離2の割合", "距離0の問いの数", "一次情報の割合", "告知・販売の位置", "分けた日", "メモ"],
                     ["上01", "上位", "あ", "100", "0", "集める本", "2000", "10", "40%", "2", "20%", "なし", "2026/11/05",
                      "https://note.com/someone/n/nabc 秘密のメモ文"],
                     ["外01", "上位外", "あ", "3", "500", "売る本", "1000", "5", "10%", "1", "0%", "なし", "2026/11/05", "メモ"]]
            for name, v in (("youten", youten), ("shokate", shokate), ("nodes", nodes), ("kijun", [["タグ", "大分類"], ["う", "くらし"]])):
                (raw / f"{name}.json").write_text(json.dumps({"values": v}, ensure_ascii=False), encoding="utf-8")
            backup = Path(d) / "analysis"
            shutil.copytree(root / "analysis", backup)
            try:
                out = build.import_sheets(raw)
                self.assertEqual(out, {"sheets_snapshot": 2, "node_compare": 2})
                snap = json.loads((root / "analysis" / "sheets_snapshot.json").read_text(encoding="utf-8"))
                a = next(t for t in snap["tags"] if t["tag"] == "あ")
                self.assertEqual((a["total"], a["total_per_day"]), (1100, 25))
                self.assertEqual(a["words"][0]["per_day"], 5.0)
                self.assertIn("い", snap["note"])
                self.assertEqual(snap["baseline"]["items"][0]["tag"], "う")
                self.assertTrue(snap["baseline"]["items"][0]["suspect"])
                text = (root / "analysis" / "node_compare.json").read_text(encoding="utf-8")
                self.assertNotIn("http", text)
                self.assertNotIn("秘密のメモ文", text)
                nc = json.loads(text)
                self.assertEqual(nc["groups"]["上位"]["dist2_pct"], 40)
            finally:
                shutil.rmtree(root / "analysis")
                shutil.copytree(backup, root / "analysis")


class EmbeddedApp(unittest.TestCase):
    """公開するページ（データを埋め込んだ1ファイル）が、データを正しく持っていること。"""

    def test_index_has_embedded_data(self):
        import json
        import re
        build.export_app()
        page = (config.SITE_DIR / "app" / "index.html").read_text(encoding="utf-8")
        m = re.search(r'<script id="obsData" type="application/json">(.*?)</script>', page, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn("</", m.group(1))
        d = json.loads(m.group(1))
        self.assertIn(d["categories"]["base_kind"], ("sweep", "rolling"))
        self.assertEqual(page.count('id="obsData"'), 1)

    def test_webapp_is_a_full_page(self):
        build.export_app()
        build.export_webapp()
        web = config.SITE_DIR / "web"
        page = (web / "index.html").read_text(encoding="utf-8")
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertEqual(page.count('id="obsData"'), 1)
        self.assertIn('name="robots" content="noindex', page)
        self.assertNotIn("claude:viewport", page)
        for f in ("manifest.webmanifest", "_headers", "robots.txt", "icon.svg"):
            self.assertTrue((web / f).exists(), f)


class TextScoring(unittest.TestCase):
    """一次情報の点：本文から合図を数える部品。"""

    def test_signals_and_no_text_in_result(self):
        from observatory import metrics
        rubric = config.load("scoring")
        self.assertEqual(len(rubric["items"]), 10)
        text = ("2026年10月3日に、自分で3回試しました。かかった時間を測ったら45分でした。\n"
                "最初はうまくいかなかったので、設定を変えました。\n"
                "まず準備します。次に実行します。画面に完了と出たら終わりです。\n"
                "初心者の人向けに書いています。")
        r = metrics.score_text(text, rubric, "2026-11-01")
        keys = {i["key"]: i for i in r["items"]}
        self.assertTrue(keys["体験"]["likely"])
        self.assertTrue(keys["失敗"]["likely"])
        self.assertTrue(keys["誰に"]["likely"])
        self.assertTrue(keys["新しさ"]["likely"])
        self.assertTrue(keys["出どころ"]["likely"])      # 外の情報なし
        self.assertGreaterEqual(r["numbers_with_unit"], 2)
        self.assertNotIn("自分で3回試しました", json.dumps(r, ensure_ascii=False))
        self.assertEqual(set(r["bonus"]), {"7W1H", "書き出し", "独自性", "読みやすさ"})
        self.assertEqual(len(rubric["bonus"]), 4)
        old = metrics.score_text("2024年1月に試した。", rubric, "2026-11-01")
        self.assertFalse({i["key"]: i for i in old["items"]}["新しさ"]["likely"])

    def test_app_data_has_rubric(self):
        import re
        build.export_app()
        d = json.loads((config.SITE_DIR / "app" / "data.json").read_text(encoding="utf-8"))
        self.assertEqual(len(d["scoring"]["items"]), 10)
        for it in d["scoring"]["items"]:
            for p in it["patterns"]:
                re.compile(p)


if __name__ == "__main__":
    unittest.main()
