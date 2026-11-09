"""使い方（プロジェクトのフォルダで）:

    py -m observatory ingest            data/raw の CSV を取り込む（新しいファイルだけ）
    py -m observatory ingest --rebuild  データベースを消して、全部取り込み直す
    py -m observatory build             ダッシュボードを site/ に書き出す（メンバー版は他人の情報が入っていないか調べてから）
    py -m observatory check             記事の材料に載せた数字が、今の計算でも同じに出るかを確かめる
    py -m observatory serve             http://localhost:8765/ でダッシュボードを開く
    py -m observatory export            Claude のダッシュボード用のデータを site/dash に書き出す
    py -m observatory all               ingest → check → build → export をまとめて
    py -m observatory sheets DIR        スプレッドシートの写し（DIR の youten/shokate/kijun/nodes.json）を analysis/ に取り込む
    py -m observatory shelfplan         次に見るカテゴリの棚（観測がいちばん古い10個）を出す
    py -m observatory pick TAG          週の採点で読む記事（人気の上位20＋新しく入った3本）を出す
    py -m observatory collect series    6タグの人気・新着を note から読んで CSV に書く（サーバーで動かす）
    py -m observatory collect shelves   次に見る10カテゴリの棚を読んで CSV に書く
    py -m observatory collect scans     タグ探し（15タグの人気・新着）を読んで CSV に書く

Mac や Linux では py の代わりに python3。
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import sys

from . import build, collect, config, db, ingest, metrics as m


def cmd_ingest(args) -> int:
    s = ingest.run(rebuild=args.rebuild)
    print(f"新しいファイル {s['files_new']}件（{s['rows_added']}行）、取り込み済みで飛ばした {s['files_skipped']}件、観測の回 {s['runs']}回")
    for e in s["errors"]:
        print("  読めなかった:", e)
    return 1 if s["errors"] else 0


def cmd_check(args) -> int:
    ref = json.loads((config.ROOT / "tests" / "reference.json").read_text(encoding="utf-8"))
    con = db.connect()
    bad = []

    def same(label, got, want, tol=0.05):
        ok = got is not None and abs(got - want) <= tol
        print(f"  {'OK ' if ok else 'NG '} {label}: {got if got is None else round(got, 2)}（記事の材料: {want}）")
        if not ok:
            bad.append(label)

    runs = con.execute("SELECT COUNT(DISTINCT run_at) FROM obs WHERE kind='series' AND run_at<=?", (ref["run_to"],)).fetchone()[0]
    same("定時観測の回数", runs, ref["runs"], 0)
    for tag, want in ref["tags"].items():
        e = m.entry(con, tag, *ref["entry_window"])
        same(f"#{tag} 新着→人気（入った本数）", e["entered"], want["entered"], 0)
        same(f"#{tag} 新着→人気（対象の本数）", e["n"], want["n"], 0)
        same(f"#{tag} 1日の新着（重なりなし）", m.unique_new(con, tag, *ref["unique_window"]), want["unique_new"], 0)
        sh = m.shelf(con, tag, "series", run_to=ref["run_to"], strict_depth=True)
        same(f"#{tag} 新着40本目の古さ（時間）", sh["new_depth_h"], want["new_depth_h"])
        same(f"#{tag} 人気の古さ（日）", sh["pop_age_days"], want["pop_age_days"], 0.06)
        same(f"#{tag} 人気のスキ", sh["pop_likes"], want["pop_likes"], 0)
    first_scan = con.execute("SELECT MIN(run_at) FROM obs WHERE kind='scan'").fetchone()[0]
    for tag, want in ref.get("scan_pop_age_days", {}).items():
        sh = m.shelf(con, tag, "scan", run_to=first_scan)
        same(f"タグ探し #{tag} 人気の古さ（日）", sh["pop_age_days"], want)
    con.close()
    print("すべて一致しました" if not bad else f"{len(bad)}件ずれています。計算か取り込みを変えたなら、理由を docs/metrics.md に書いてください")
    return 1 if bad else 0


def cmd_build(args) -> int:
    out = build.run()
    for k, v in out.items():
        print(f"  {k}: {v}")
    return 0


def cmd_export(args) -> int:
    for k, (path, n) in build.export_dash().items():
        print(f"  {k}: {n}行 → {path}")
    path, n = build.export_app()
    web = build.export_webapp()
    if web:
        print(f"  web: {web}")
    print(f"  app: 照合できる記事 {n}本 → {path}")
    return 0


def cmd_serve(args) -> int:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(config.SITE_DIR))
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as srv:
        print(f"http://localhost:{args.port}/private/  （自分用）")
        print(f"http://localhost:{args.port}/members/  （メンバー版）")
        print("止めるときは Ctrl+C")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def cmd_sheets(args) -> int:
    out = build.import_sheets(args.dir)
    if not out:
        print("取り込めるスプレッドシートの写しがありませんでした")
        return 1
    for k, n in out.items():
        print(f"  {k}: {n}")
    return 0


def cmd_shelfplan(args) -> int:
    con = db.connect()
    plan = build.shelf_plan(con, args.n)
    last = {r["tag"]: r["last"] for r in con.execute(
        "SELECT tag, MAX(run_at) AS last FROM obs WHERE kind='shelf' GROUP BY tag")}
    con.close()
    if args.json:
        print(json.dumps([{"path": p, "name": n, "last": last.get(p, "")} for p, n in plan], ensure_ascii=False))
    else:
        for p, n in plan:
            print(f"{p}\t{n}\t{last.get(p, '')}")
    return 0


def cmd_pick(args) -> int:
    con = db.connect()
    out = build.pick_candidates(con, args.tag, args.top, args.new, args.days)
    con.close()
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"{out['tag']}  最新の回 {out['run_at']}  比べた回 {out.get('prev_run')}")
        for it in out["items"]:
            print(f"  {it['rank']:>3}位  {it['why']}{'（新）' if it['new'] else ''}  スキ{it['likes']}  {it['published']}  {it['url']}")
    return 0


def cmd_collect(args) -> int:
    try:
        if args.what == "series":
            path = collect.collect_series()
        elif args.what == "shelves":
            con = db.connect()
            plan = build.shelf_plan(con, args.n)
            con.close()
            if not plan:
                print("棚の記録がまだ無いので、見るカテゴリを決められません（data/raw/shelves の CSV を取り込んでから）")
                return 1
            path = collect.collect_shelves(plan)
        else:
            con = db.connect()
            tags = collect.scan_tags(args.n, con)
            con.close()
            path = collect.collect_scans(tags)
    except collect.CollectError as e:
        print(f"観測できなかった: {e}")
        return 1
    print(f"書いた {path}")
    return 0


def cmd_all(args) -> int:
    args.rebuild = False
    rc = cmd_ingest(args)
    rc |= cmd_check(args)
    rc |= cmd_build(args)
    rc |= cmd_export(args)
    return rc


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="observatory", description="note タグ観測のデータをためて、ダッシュボードにする")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("ingest", help="CSV を取り込む")
    pi.add_argument("--rebuild", action="store_true", help="データベースを作り直す")
    sub.add_parser("check", help="記事の材料の数字と照らし合わせる")
    sub.add_parser("build", help="ダッシュボードを書き出す")
    sub.add_parser("export", help="Claude のダッシュボード用のデータ（site/dash）を書き出す")
    ps = sub.add_parser("serve", help="ダッシュボードを手元で開く")
    ps.add_argument("--port", type=int, default=8765)
    sub.add_parser("all", help="ingest → check → build → export")
    pq = sub.add_parser("sheets", help="スプレッドシートの写しを analysis/ に取り込む")
    pq.add_argument("dir", help="youten.json・shokate.json・kijun.json・nodes.json を置いたフォルダ")
    pp = sub.add_parser("shelfplan", help="次に見るカテゴリの棚を出す")
    pp.add_argument("-n", type=int, default=10)
    pp.add_argument("--json", action="store_true")
    pk = sub.add_parser("pick", help="週の採点で読む記事を出す")
    pk.add_argument("tag")
    pk.add_argument("--top", type=int, default=20)
    pk.add_argument("--new", type=int, default=3)
    pk.add_argument("--days", type=int, default=7)
    pk.add_argument("--json", action="store_true")
    pc = sub.add_parser("collect", help="note のページを読んで観測の CSV を書く")
    pc.add_argument("what", choices=["series", "shelves", "scans"])
    pc.add_argument("-n", type=int, default=None, help="shelves は見るカテゴリ数（10）、scans はタグ数（15）")
    args = p.parse_args(argv)
    if getattr(args, "cmd", "") == "collect" and args.n is None:
        args.n = 10 if args.what == "shelves" else 15
    return {"ingest": cmd_ingest, "check": cmd_check, "build": cmd_build, "export": cmd_export, "serve": cmd_serve,
            "all": cmd_all, "sheets": cmd_sheets, "shelfplan": cmd_shelfplan, "pick": cmd_pick,
            "collect": cmd_collect}[args.cmd](args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows のコンソールで文字化けしないように
    sys.exit(main())
