"""人気面の天気（凍結／流動／総入れ替え）の判定と、生成元の記録。"""
from __future__ import annotations

import csv
import io
import tempfile
import unittest
from pathlib import Path

from observatory import build, collect, db

TAGS = ["a", "b", "c", "d"]


def _insert(con, run_at, tag, urls, likes=None):
    for i, u in enumerate(urls, 1):
        author = u.split("/")[1]
        con.execute(
            "INSERT INTO obs (file_id, kind, observed_at, run_at, surface, tag, section, page, total, rank, url, author, own, likes, published_at, price, title, text) "
            "VALUES (1, 'series', ?, ?, 'popular', ?, '', 'tag', NULL, ?, ?, ?, 0, ?, '2026-11-01 10:00', '', '', '')",
            (run_at, run_at, tag, i, u, author, (likes or {}).get(u)))


class Weather(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = db.connect(Path(self.tmp.name) / "w.sqlite")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_states(self):
        con = self.con
        # 回1・回2：タグごとに別々の記事で、回2は20本のうち8本が入れかわる（流動）
        for t in TAGS:
            first = [f"/u{t}{i}/n/n{t}{i}" for i in range(20)]
            _insert(con, "2026-11-01 06:51", t, first)
            _insert(con, "2026-11-01 18:51", t, first[:12] + [f"/v{t}{i}/n/m{t}{i}" for i in range(8)])
        # 回3：全タグ同じ20本に総入れ替え。回4・回5：そのまま（凍結）
        same = [f"/w{i}/n/same{i}" for i in range(20)]
        for run in ["2026-11-02 06:51", "2026-11-02 18:51", "2026-11-03 06:51"]:
            for t in TAGS:
                _insert(con, run, t, same, likes={u: 100 + 10 * j for j, u in enumerate(same)})
        con.commit()
        w = build.popular_weather(con, tags=TAGS)
        states = [r["state"] for r in w["runs"]]
        self.assertEqual(states, ["unknown", "flowing", "switch", "frozen", "frozen"])
        st = w["state"]
        self.assertEqual(st["state"], "frozen")
        self.assertEqual(st["since"], "2026-11-02 06:51")
        self.assertEqual(st["last_switch"], "2026-11-02 06:51")
        self.assertEqual(w["runs"][-1]["dup2"], 20)
        self.assertEqual(w["runs"][-1]["dup3"], 20)
        self.assertEqual(w["runs"][1]["dup2"], 0)
        self.assertEqual(w["runs"][-1]["author_dup"], 0)
        self.assertEqual(w["runs"][-1]["gain_day"], 0)   # スキが増えていない
        # 出すのは数字だけ（URL・記事名を含まない）
        import json
        text = json.dumps(w, ensure_ascii=False)
        self.assertNotIn("/n/", text)
        self.assertNotIn("same", text)

    def test_source_rows_and_csv(self):
        notes = [{"source": "query_gtt_tag_notes_popular_v20261105"}] * 18 + [{"source": ""}] * 2
        rows = collect.source_rows("2026-11-16 06:51", "tag:note副業", notes)
        self.assertEqual(rows[0], ["2026-11-16 06:51", "tag:note副業", "query_gtt_tag_notes_popular_v20261105", 18])
        with tempfile.TemporaryDirectory() as d:
            p = collect.write_sources(rows, "2026-11-16_0651", Path(d))
            got = list(csv.DictReader(p.open(encoding="utf-8")))
            self.assertEqual(got[0]["source"], "query_gtt_tag_notes_popular_v20261105")
            self.assertEqual(p.name, "note_生成元_raw_2026-11-16_0651.csv")

    def test_note_row_keeps_source(self):
        n = {"key": "n0123456789ab", "name": "t", "publishAt": "2026-11-10T21:40:00+09:00", "likeCount": 3,
             "user": {"urlname": "x"}, "recommendSource": "query_gtt_tag_notes_popular_v20261105"}
        r = collect._note_row(n)
        self.assertEqual(r["source"], "query_gtt_tag_notes_popular_v20261105")


if __name__ == "__main__":
    unittest.main()
