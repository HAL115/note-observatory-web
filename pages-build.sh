#!/usr/bin/env bash
# Cloudflare Pages のビルドコマンド。出力は site/web。
set -euo pipefail
python3 --version
python3 -m observatory ingest --rebuild
python3 -m observatory build
python3 -m observatory export
test -s site/web/index.html
