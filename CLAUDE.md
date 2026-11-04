# CLAUDE.md

このファイルは、本リポジトリで作業する Claude Code 向けのガイドです。

## プロジェクト概要

**auto-masking-app-for-db-data** — 業務データ（Excel .xlsx・CSV の複数表）に含まれる機密性のある名称・識別コードを、Windows ローカルで連番名称・代替コードに置き換えるアプリケーション。構造・データ型・表間の関係を維持し、金額・数量・日付は原則そのまま残す。

設計書は [docs/design/](docs/design/README.md) にあります（段階0〜5 を実装済み。進捗は 09 章。次の段階は指示を受けてから着手する）。

- 名称・コードの置換であり、匿名化を保証する製品ではない。画面文言で「完全匿名化済み」等と表示しない。
- 開発・試験には合成データのみを使う。実データをリポジトリや Claude Code に読み込ませない。

## 技術スタック

- 言語 / GUI: Python 3.12 系 + PySide6（確定）
- 保存: SQLite（プロジェクトごとに1ファイル）
- xlsx: zipfile + lxml による部品単位の書き換え（openpyxl は検証用の読み直しのみ）
- ローカルLLM: OpenVINO GenAI（確定。既定は GPU、推奨モデル Qwen3-4B int4。設計書 02 章 1.4）
- テスト・リント: pytest、ruff

## 開発コマンド

Windows の Git Bash / PowerShell から実行する（Python 3.12）。

```bash
# セットアップ（初回）
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pip install -e ".[dev]"

# テスト実行（試験プロセスでは外部通信が遮断される）
.venv/Scripts/python -m pytest -q

# アプリの起動（画面）
.venv/Scripts/python -m maskapp

# AI（端末内の LLM）を使う場合の追加の依存（openvino-genai）
.venv/Scripts/python -m pip install -r requirements-ai.txt

# AI の計測（モデルは C:/Users/<利用者>/maskapp_models 等、リポジトリの外に置く）
.venv/Scripts/python -m tests.llm.bench <モデルのフォルダ> GPU
MASKAPP_TEST_MODEL=<モデルのフォルダ> .venv/Scripts/python -m pytest -m llm

# Excel での確認（手元に Excel がある場合のみ。出力を Excel で開き、再計算後の値を比べる）
.venv/Scripts/python -m pytest -m excel

# リント
.venv/Scripts/python -m ruff check src tests

# 合成データの書き出し（出力先はリポジトリ外にする。*.csv / *.xlsx は .gitignore で除外済み）
.venv/Scripts/python -m tests.synthetic <出力フォルダ>
```

## 実装上の約束

- ログは `maskapp.infra.safelog.get_logger()` の `event()` だけを使う。文字列（セル値・ファイル名・シート名・パス）は渡せない設計になっている。数値・真偽・列挙型・UUID のみ。
- 業務データを扱うプロセスは起動直後に `maskapp.infra.netguard.install_network_guard()` を呼ぶ。
- 一時ファイルは `Project.temp_dir`（プロジェクト内の work/tmp）に作る。%TEMP% は使わない。
- 変換作業は `maskapp.app.Workbench` を通して行う（計画 → 書出し → 検証 → 確定 → 公開）。外部フォルダへの公開は対応表の確定後のみ。
- 画面（`maskapp.ui`）は表示と操作の受付だけを持ち、処理は `maskapp.app.session.Session` を通す。画面の文言に「完全匿名化済み」「安全」等を使わない（試験で確認している）。元の値を表示する部品は「元の値を伏せる」（`ui.common.MASK`）に従う。
- xlsx は `maskapp.formats.xlsx` で部品単位に扱う。openpyxl で開いて保存し直さない（openpyxl は検証の第二の読込器としてだけ使う）。新しい部品・機能に対応するときは、先に設計書 06 章の対応表を更新する。
- AI（LLM）は `maskapp.llm` のワーカープロセスだけで動かす。本体のプロセスで openvino を読み込まない。AI の出力は候補としてだけ使い（`llm.tasks` で検証）、置換に直接使わない。AI の入出力をログに残さない。
- project.db への書込は `Project.transaction()` の中で行う。スキーマ変更は `project/schema.py` の MIGRATIONS に追加し、既存の移行は変更しない。

## コーディング方針

- 本アプリは機密データを扱うため、**実データ・接続情報・認証情報をコードやコミットに含めない**こと。
  - テスト・サンプルには合成データ生成器（`tests/synthetic/`）で作ったデータのみを使用する。
- ログ・例外メッセージ・一時ファイルにマスキング前の値を出力しない。
- 業務データ処理中にネットワーク通信を行うコードを書かない。

## Git 運用ルール

- **コードや設計書を変更するたびに、変更をコミットして GitHub にプッシュすること。**
  - 1つの変更（機能追加・修正・リファクタリング等）が完了した時点で、`git add` → `git commit` → `git push` まで行う。
  - 作業途中の壊れた状態ではプッシュしない。可能であればテストを通してからプッシュする。
- コミットメッセージは変更内容が分かるように簡潔に書く（日本語可）。
  - 例: `マスキングルールにメールアドレス形式を追加`
- コミット前に `git status` / `git diff` で差分を確認し、機密情報（`.env`、認証情報、実データのダンプ等）が含まれていないことを確認する。
- `git push --force` やコミット履歴の書き換えは、ユーザーの明示的な指示がない限り行わない。
- プッシュに失敗した場合（リモートとの競合など）は、勝手に強制解決せずユーザーに報告する。
