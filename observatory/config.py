"""設定ファイルとパスの読み込み。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "observatory.sqlite"
SITE_DIR = ROOT / "site"
WEB_DIR = ROOT / "web"


def load(name: str) -> dict:
    """config/<name>.json を読む。"""
    with open(CONFIG_DIR / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


def project() -> dict:
    return load("project")


def source_dirs() -> list[Path]:
    """CSV を探すフォルダ。相対パスはプロジェクトの場所から数える。"""
    dirs = []
    for s in project().get("sources", ["data/raw"]):
        p = Path(s).expanduser()
        if not p.is_absolute():
            p = ROOT / p
        dirs.append(p)
    return dirs


def field_of_tag() -> dict[str, str]:
    """タグ名 → 分野名。"""
    out: dict[str, str] = {}
    for f in load("tags").get("fields", []):
        for t in f["tags"]:
            out.setdefault(t, f["name"])
    return out
