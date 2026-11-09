# note観測所（note-observatory-web）

noteのキーワードページ・カテゴリページを決まった時刻に読み、どこに出せば読まれやすいか、何を書けばよいかを数字で示すサイト。
観測（GitHub Actions）→ 集計（Python）→ サイト（Cloudflare Pages）まで、このリポジトリ1つで動く。PCは要らない。

- 観測：`observatory/collect.py`。ログアウトのまま HTML を取り、埋め込みのデータから記事の URL・タイトル・公開時刻・スキ・価格を読む。本文は保存しない。/api/ と /search は開かない。1回に開くページは40まで、1日に50〜60まで。
- 集計：`observatory/`（ingest → check → build → export）。データベースは毎回 `data/raw` の CSV から作り直す（コミットしない）。
- サイト：`site/web/`（`export` が作る）。Cloudflare Pages が push のたびに作り直す。入口は Cloudflare Access（メールでログイン）。

## 定時の観測（.github/workflows/observe.yml）

| 何を | 日本時間 | ページ数 |
|---|---|---|
| 6タグの人気・新着（note副業・note初心者・副業・在宅ワーク・生成AI・起業） | 06:50 / 18:50 | 12 |
| カテゴリの棚（観測が古い順に10カテゴリ、5日で一巡） | 16:40 | 10 |
| タグ探し（config/tags.json の fields から15タグ、人気と新着） | 日・水 10:20 | 30 |

手で動かすとき：Actions → observe → Run workflow → 何を観測するか選ぶ。

## 最初の設定（1回だけ）

### 1. GitHub
- このリポジトリは private のまま。
- Settings → Actions → General → Workflow permissions を **Read and write permissions** にする（観測の CSV を Actions が push するため）。

### 2. Cloudflare Pages（サイト）
1. Cloudflare ダッシュボード → Workers & Pages → Create → Pages → **Connect to Git** → このリポジトリを選ぶ。
2. Build settings：
   - Framework preset: None
   - Build command: `bash pages-build.sh`
   - Build output directory: `site/web`
   - Environment variables: `PYTHON_VERSION` = `3.12`
3. Save and Deploy。数分で `https://<project>.pages.dev` にサイトが出る。
4. 以後は、Actions が CSV を push するたびに自動で作り直される。

### 3. Cloudflare Access（登録した人だけ）
1. Zero Trust → Access → Applications → Add an application → **Self-hosted**。
2. Application domain: `<project>.pages.dev`（独自ドメインを付けたらそれも）。
3. Login methods: **One-time PIN** だけ残す（メールに届く数字でログイン）。
4. Policy: Action = Allow、Include = Emails（メンバーのアドレスを並べる）か Emails ending in。
5. 保存。Pages 側で「Access policy」を有効にする（Pages の Settings → Access policy → Enable）。

これで、サイトを開くとメールアドレスを聞かれ、届いた数字を入れた人だけが見られる。

## 手元で動かす

```
python3 -m observatory ingest --rebuild   # CSV からデータベースを作る
python3 -m observatory all                # check → build → export
python3 -m observatory serve              # http://localhost:8765/
python3 -m unittest discover tests
```

## 決まり
- 書き手の名前・ID・タイトル・URL を、メンバー向けの画面に出さない（`build` が確かめて止める）。
- 本文は保存しない。
- 観測の CSV は消さない・書き換えない（足すだけ）。
