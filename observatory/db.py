"""SQLite の作成と接続。データベースは data/raw の CSV からいつでも作り直せる。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    sha256      TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,          -- series（定時観測）| scan（狙い目タグ探し）
    rows        INTEGER NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS obs (
    id           INTEGER PRIMARY KEY,
    file_id      INTEGER NOT NULL REFERENCES files(id),
    kind         TEXT NOT NULL,         -- series | scan
    observed_at  TEXT NOT NULL,         -- 日本時間 'YYYY-MM-DD HH:MM'
    run_at       TEXT,                  -- 同じ回の観測をまとめた時刻（取り込み後に入れる）
    surface      TEXT NOT NULL,         -- popular（人気）| new（新着）| category（カテゴリの欄）| search（検索、10/23まで）
    tag          TEXT NOT NULL,         -- タグ名。category は面の名前、search は検索語
    section      TEXT NOT NULL DEFAULT '',  -- カテゴリの欄の名前
    page         TEXT NOT NULL DEFAULT '',  -- tag（キーワードページ）| hashtag | ''（不明）
    total        INTEGER,               -- ページに出ていた件数
    rank         INTEGER,
    url          TEXT NOT NULL,         -- '/ユーザー名/n/n…' の形
    author       TEXT NOT NULL,
    own          INTEGER NOT NULL DEFAULT 0,
    likes        INTEGER,
    published_at TEXT,                  -- 日本時間 'YYYY-MM-DD HH:MM'
    price        TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    text         TEXT NOT NULL DEFAULT '',
    UNIQUE (kind, observed_at, surface, tag, section, rank, url)
);

CREATE INDEX IF NOT EXISTS obs_run ON obs (kind, run_at, surface, tag);
CREATE INDEX IF NOT EXISTS obs_url ON obs (url);

-- 記事ページで見た分類（パンくず）とハッシュタグ。kind=shelf の obs は、カテゴリページの棚
--   （tag＝カテゴリのパス、section＝棚のキーワード、rank＝棚の中の順番、text＝カテゴリの名前、
--    published_at＝観測時刻 − カードの「◯時間前」から出した目安）
CREATE TABLE IF NOT EXISTS article_meta (
    id            INTEGER PRIMARY KEY,
    file_id       INTEGER NOT NULL REFERENCES files(id),
    checked_at    TEXT NOT NULL,        -- 日本時間 'YYYY-MM-DD HH:MM'
    url           TEXT NOT NULL,        -- '/ユーザー名/n/n…'
    author        TEXT NOT NULL,
    own           INTEGER NOT NULL DEFAULT 0,
    category_path TEXT NOT NULL DEFAULT '',   -- 'business/career_work' など（分からなければ空）
    category_name TEXT NOT NULL DEFAULT '',   -- 'ビジネス＞キャリア・働き方'
    hashtags      TEXT NOT NULL DEFAULT '',   -- '|' 区切り
    published_at  TEXT,
    title         TEXT NOT NULL DEFAULT '',
    UNIQUE (url, checked_at)
);
CREATE INDEX IF NOT EXISTS meta_url ON article_meta (url);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con
