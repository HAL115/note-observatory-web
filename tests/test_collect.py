"""collect（ブラウザなしの観測）の解析と CSV の形を確かめる。

note の HTML は Next.js の self.__next_f.push([1,"…"]) に JSON を埋め込んでいる（2026-11-10 に確認）。
ここでは同じ形の HTML を作って、記事一覧が取り出せること、CSV が ingest で読める形であることを見る。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from observatory import collect, ingest


def _note(key, user, name, publish, likes, price):
    return {"id": 1, "key": key, "type": "TextNote", "name": name, "publishAt": publish,
            "eyecatchUrl": "https://assets.st-note.com/x.png?fit=bounds&format=jpeg",
            "user": {"id": 2, "key": "u", "nickname": user + "さん, 作家", "urlname": user},
            "isLiked": False, "canRead": price == 0, "likeCount": likes,
            "recommendSource": "query_gtt_tag_notes_popular_v20261105", "price": price}


def _flight_html(initial: dict) -> str:
    # 実物と同じく、React の flight の行の中に initialData が入り、全体が JS の文字列として埋め込まれる
    text = '1:"$Sreact.fragment"\n2:["$","div",null,{"hashtag":{"name":"note副業"},"initialData":' \
           + json.dumps(initial, ensure_ascii=False) + '}]\n'
    literal = json.dumps(text, ensure_ascii=False)  # 両端の " を含む JS の文字列
    return ('<!DOCTYPE html><html><head><title>note副業の人気・新着記事まとめ｜note</title></head><body>'
            '<script>self.__next_f.push([1,"1:I[6407,[],\\"\\"]\\n"])</script>'
            f'<script>self.__next_f.push([1,{literal}])</script></body></html>')


TAG_HTML = _flight_html({"notes": [
    _note("nef3c22e4034e", "akari_seaart", "やあ人類、君のうろこは\"もう\"生えてるよ", "2026-11-03T23:22:07.000+09:00", 586, 100),
    _note("nf21a37911945", "pakucha", "note副業を1年間やったら, 稼げた", "2026-10-30T08:00:00.000+09:00", 441, 1480),
    _note("n9d4f4ec5a8e5", "oimocyan", "無料の記事", "2026-11-02T20:00:00.000+09:00", 1421, 0),
]})

TOPIC_HTML = _flight_html({"sections": [
    {"type": "hashtag_notes", "labelName": "GMOインターネット", "notes": [
        _note("n8e5c45f36c42", "sier_manager", "10月だけで5件", "2026-11-09T22:14:43.000+09:00", 1, 0)]},
    {"type": "hashtag_notes", "labelName": "業務改善", "notes": [
        _note("naa32911eeca2", "jun_x", "信頼崩壊", "2026-11-08T18:25:36.000+09:00", 12, 300),
        _note("nbadkey", "x", "壊れた行", "2026-11-08T18:25:36.000+09:00", 0, 0)]},
]})


def _ref_html() -> str:
    """恋愛のキーワードページで見た形：最初の initialData は別物への参照で、記事一覧は後ろの行にある。"""
    notes = {"notes": [_note("n6a84a8da6783", "writer_a", "参照先の記事", "2026-11-09T20:00:00.000+09:00", 70, 0)]}
    lines = [
        '1:"$Sreact.fragment"',
        '7c:{"targetType":"gtt","targetKey":"恋愛","sort":"hot","thread":{"data":null}}',
        '7e:' + json.dumps(notes, ensure_ascii=False),
        'c:[["$","$L7b","恋愛",{"initialData":"$@7c","children":["$","section",null,{"children":[["$","$L75","50代恋愛-8",'
        '{"keyword":{"name":"50代恋愛","noteCount":608},"tagName":"恋愛"}],["$","$L76",null,{"tagKey":"恋愛","initialData":"$@7e"}]]}]}]]',
    ]
    text = "\n".join(lines) + "\n"
    return '<html><body><script>self.__next_f.push([1,' + json.dumps(text, ensure_ascii=False) + '])</script></body></html>'


class Parse(unittest.TestCase):
    def test_reference_form(self):
        html = _ref_html()
        notes = collect.notes_from_tag_page(html)
        self.assertEqual([n["url"] for n in notes], ["/writer_a/n/n6a84a8da6783"])
        self.assertEqual(collect.related_keywords(html), [("50代恋愛", 608)])

    def test_tag_page_notes(self):
        notes = collect.notes_from_tag_page(TAG_HTML)
        self.assertEqual([n["url"] for n in notes],
                         ["/akari_seaart/n/nef3c22e4034e", "/pakucha/n/nf21a37911945", "/oimocyan/n/n9d4f4ec5a8e5"])
        self.assertEqual(notes[0]["likes"], 586)
        self.assertEqual(notes[0]["published"], "2026-11-03 23:22")
        self.assertEqual(notes[0]["price"], "¥100")
        self.assertEqual(notes[1]["price"], "¥1,480")
        self.assertEqual(notes[2]["price"], "")
        self.assertIn('"もう"', notes[0]["title"])          # 題名の引用符は壊れない
        self.assertNotIn(",", notes[1]["title"])             # 半角カンマは全角に
        self.assertEqual(collect.page_title(TAG_HTML), "note副業の人気・新着記事まとめ")

    def test_topic_sections_skip_broken(self):
        secs = collect.sections_from_topic_page(TOPIC_HTML)
        self.assertEqual([s for s, _ in secs], ["GMOインターネット", "業務改善"])
        self.assertEqual(len(secs[1][1]), 1)                 # key の形がおかしい行は捨てる

    def test_missing_initial_data(self):
        with self.assertRaises(collect.CollectError):
            collect.notes_from_tag_page("<html><body>no data</body></html>")

    def test_urls(self):
        self.assertEqual(collect.tag_url("note副業"), "https://note.com/tag/note%E5%89%AF%E6%A5%AD")
        self.assertEqual(collect.tag_url("note副業", new=True), "https://note.com/tag/note%E5%89%AF%E6%A5%AD?mode=new")
        self.assertEqual(collect.hashtag_url("M&A"), "https://note.com/hashtag/M%26A")
        self.assertEqual(collect.topic_url("lifestyle/housing_living"), "https://note.com/topic/lifestyle/housing_living")


class RoundTrip(unittest.TestCase):
    """collect が書いた CSV を ingest がそのまま読めること。"""

    def _fake_fetch(self, url, timeout=30):
        return url, (TOPIC_HTML if "/topic/" in url else TAG_HTML)

    def test_series_csv_is_ingestable(self):
        now = dt.datetime(2026, 11, 10, 6, 50, tzinfo=collect.JST)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(collect, "fetch", self._fake_fetch), \
                mock.patch.object(collect, "pause", lambda: None), \
                mock.patch.object(collect.Status, "save", lambda self: None):
            path = collect.collect_series(["note副業", "副業"], Path(d), now)
            self.assertEqual(path.name, "note_観測ログ_raw_2026-11-10_0650.csv")
            text = path.read_text(encoding="utf-8")
            kind, rows, skipped = ingest.rows_from_csv(text, "aitohal")
        self.assertEqual(kind, "series")
        self.assertEqual(skipped, 0)
        self.assertEqual(len(rows), 12)                       # 2タグ × 2面 × 3本
        self.assertEqual({r["surface"] for r in rows}, {"popular", "new"})
        self.assertEqual(rows[0]["observed_at"], "2026-11-10 06:50")
        self.assertEqual(rows[0]["published_at"], "2026-11-03 23:22")
        self.assertEqual(rows[0]["likes"], 586)
        self.assertEqual(rows[1]["price"], "¥1,480")

    def test_shelves_csv_is_ingestable(self):
        now = dt.datetime(2026, 11, 10, 16, 40, tzinfo=collect.JST)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(collect, "fetch", self._fake_fetch), \
                mock.patch.object(collect, "pause", lambda: None), \
                mock.patch.object(collect.Status, "save", lambda self: None):
            path = collect.collect_shelves([("business", "ビジネス")], Path(d), now)
            kind, rows, skipped = ingest.rows_from_csv(path.read_text(encoding="utf-8"), "aitohal")
        self.assertEqual(kind, "shelf")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["tag"], "business")
        self.assertEqual(rows[0]["section"], "GMOインターネット")
        self.assertEqual(rows[1]["price"], "有料")
        self.assertTrue(rows[0]["published_at"].startswith("2026-11-09 22:1"))   # age_h から戻した公開時刻（分の丸め込みあり）

    def test_scans_csv_is_ingestable(self):
        now = dt.datetime(2026, 11, 11, 10, 20, tzinfo=collect.JST)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(collect, "fetch", lambda url, timeout=30: ("https://note.com/tag/x", TAG_HTML)), \
                mock.patch.object(collect, "pause", lambda: None), \
                mock.patch.object(collect.Status, "save", lambda self: None):
            path = collect.collect_scans(["業務改善"], Path(d), now)
            kind, rows, skipped = ingest.rows_from_csv(path.read_text(encoding="utf-8"), "aitohal")
        self.assertEqual(kind, "scan")
        self.assertEqual(len(rows), 6)
        self.assertEqual({r["surface"] for r in rows}, {"popular", "new"})
        self.assertEqual(rows[0]["tag"], "業務改善")


if __name__ == "__main__":
    unittest.main()
