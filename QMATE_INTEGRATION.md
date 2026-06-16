# Qmate → ランク自動判定 → Slack通知 連携

Qmateに応募が入ると、応募者一覧から情報を取得し、アプリと同じ基準でランク判定して
Slackへ自動通知します。

## 仕組み（なぜポーリングか）

Qmate WebAPIは「応募者一覧取得」エンドポイント1つで、**日付を渡すとその日の応募者一覧が
CSVで返る**だけです（`POST https://m.q-mate.jp/extapicsv/v1/applicant/list`）。
Webhook（応募時のプッシュ通知）が無いため、**GitHub Actionsで数分おきに取りに行く
（ポーリング）**方式にしています。新規応募だけを検出してSlackに流します。

```
GitHub Actions (cron 10分おき)
  └─ qmate_watcher.py
       ├─ Qmate API へ POST（api_key, export_date=今日）→ CSV
       ├─ 新規応募を抽出（前回分は qmate_state.json で重複排除）
       ├─ rank.py でランク判定（アプリと同一基準）
       └─ Slack Incoming Webhook へ通知
```

## セットアップ手順

### 1. Slack Incoming Webhook を用意
1. Slackで通知したいチャンネルを決める
2. https://api.slack.com/messaging/webhooks に沿って Incoming Webhook を作成
3. 発行された `https://hooks.slack.com/services/XXX/YYY/ZZZ` をコピー

### 2. GitHub Secrets を登録
リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で2つ登録：

| Secret名 | 値 |
|---|---|
| `QMATE_API_KEY` | Qmate設定画面「外部連携 > APIキー」の値 |
| `SLACK_WEBHOOK_URL` | 上で取得したWebhook URL |

> 🔒 APIキー・Webhookはコードに直接書かないでください。必ずSecretsで管理します。

### 3. マージで有効化
`.github/workflows/qmate-watch.yml` が **mainにマージされると** 定期実行が始まります
（GitHubの仕様上、scheduleはデフォルトブランチでのみ動作）。
有効化後、最初の1回は「既存応募を既読として記録するだけ（通知なし）」で、
**それ以降の新規応募から通知**します（過去分の大量通知を防ぐため）。

## 動作確認（サンプルCSVでのテスト）

APIを叩かず、手元のCSVで判定とSlack文面を確認できます。

```bash
# Slackに送らず、判定結果と通知文面だけ標準出力（おすすめ）
QMATE_CSV_FILE=sample.csv QMATE_DRY_RUN=1 QMATE_NOTIFY_ON_FIRST_RUN=1 python qmate_watcher.py

# 実際にSlackへ1件送ってみる
QMATE_CSV_FILE=sample.csv SLACK_WEBHOOK_URL="https://hooks.slack.com/..." \
  QMATE_NOTIFY_ON_FIRST_RUN=1 python qmate_watcher.py
```

## 列名のマッピングについて

ランク判定には **年齢・転職回数・短期離職数** が必要です。
スクリプトはCSVのヘッダ名から各項目を**自動推定**します（`COLUMN_HINTS`）。
うまく当たらない場合は、環境変数で明示指定できます（自動推定より優先）：

| 環境変数 | 用途 |
|---|---|
| `QMATE_COL_NAME` | 氏名の列 |
| `QMATE_COL_AGE` | 年齢の列 |
| `QMATE_COL_DOB` | 生年月日の列（年齢列が無い場合に年齢を計算） |
| `QMATE_COL_JOB_CHANGES` | 転職回数の列 |
| `QMATE_COL_SHORT_TERM` | 短期離職数の列 |
| `QMATE_COL_JOB` | 応募求人/職種の列 |
| `QMATE_COL_DATE` | 応募日時の列 |
| `QMATE_COL_ID` | 応募ID（重複排除に使用。無ければ氏名＋応募日時で代替） |

> 応募CSVに「転職回数」「短期離職数」が無いケースもあります。その場合は
> **0で暫定計算**し、Slack通知に「データ不足（要確認）」と明記します。
> 実際のCSVヘッダが分かれば、ここの扱いを最適化できます（サンプル共有ください）。

## 補足
- 追加パッケージは不要（Python標準ライブラリのみ）。
- ポーリング間隔は `qmate-watch.yml` の `cron` で調整可能（GitHub Actionsの最短は約5分、
  実行は多少遅延します）。
- 重複通知の防止状態は GitHub Actions の cache（`qmate_state.json`）で実行間を引き継ぎます。
