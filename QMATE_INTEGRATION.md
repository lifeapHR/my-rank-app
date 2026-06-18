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
       ├─ 年齢/転職回数/短期離職数を取得（独立列が無ければ「備考」からAIで抽出）
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

| Secret名 | 値 | 必須 |
|---|---|---|
| `QMATE_API_KEY` | Qmate設定画面「外部連携 > APIキー」の値 | ✅ |
| `SLACK_WEBHOOK_URL` | 上で取得したWebhook URL | ✅ |
| `GEMINI_API_KEY` | Gemini APIキー（**備考からのAI抽出用**。アプリで使っているものでOK。カンマ区切りで複数可） | ▲ 備考から判定する場合 |

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

実際のQmate応募者CSV（`UID, 姓|必須, 名|必須, …, 生年月日, …, 選考ステータス, 備考`）は
そのまま自動対応します:
- **氏名**は `姓` + `名` を結合（`氏名`の単独列が無くてもOK）
- **年齢**は `生年月日` から算出（`年齢`列が無くてもOK）
- **転職回数・短期離職数**は `備考` の職歴テキストからAI抽出
- ヘッダの `|必須` 等の注記は無視して照合します

うまく当たらない場合は、環境変数で明示指定できます（自動推定より優先）：

| 環境変数 | 用途 |
|---|---|
| `QMATE_COL_NAME` | 氏名の列（1列に氏名がある場合） |
| `QMATE_COL_LAST_NAME` / `QMATE_COL_GIVEN_NAME` | 姓 / 名 の列（氏名が分かれている場合） |
| `QMATE_COL_AGE` | 年齢の列 |
| `QMATE_COL_DOB` | 生年月日の列（年齢列が無い場合に年齢を計算） |
| `QMATE_COL_JOB_CHANGES` | 転職回数の列 |
| `QMATE_COL_SHORT_TERM` | 短期離職数の列 |
| `QMATE_COL_COMPANY` | 応募先（掲載企業名）の列 |
| `QMATE_COL_JOB` | 応募求人/職種の列 |
| `QMATE_COL_DATE` | 応募日時の列 |
| `QMATE_COL_STATUS` | 選考ステータスの列 |
| `QMATE_COL_ID` | 応募ID（重複排除に使用。無ければ氏名＋応募日時で代替） |
| `QMATE_COL_REMARKS` | 備考など、経歴情報が書かれた自由記述の列（AI抽出の優先入力） |
| `QMATE_INCOMPLETE_MODE` | データ不足時の扱い。`hold`（既定/判定保留）または `zero`（0で暫定計算）。下記参照 |

## 備考・職歴からのAI抽出

年齢・転職回数・短期離職数が**独立した列として存在せず、「備考」などの自由記述に
書かれている**場合は、その文章からGeminiで3項目を抽出してランク判定します
（アプリ本体の抽出と同じ考え方）。

- 抽出には**備考だけでなく職歴の手掛かりを広く渡します**（`転職経験`/`経験年数`/
  `入社年・退職年`/`直近の企業名`/`最終学歴`/`志望動機` など。連絡先・住所等のPIIは除外）。
  `QMATE_COL_REMARKS` を指定すると、その列を優先入力にできます。
- `GEMINI_API_KEY` を Secrets に設定すると有効になります（カンマ区切りで複数キー可。
  レート制限時は自動で次のキーに切替）。
- AIで補完した項目は、Slack通知に `:robot_face: 備考・職歴からAI抽出: …（要確認）` と明記します。

> 実際の「備考」の書き方（例文1つ）をいただければ、抽出プロンプトを最適化できます。

## データ不足時の扱い（誤判定の防止）

転職回数・短期離職数を**0として埋めると経歴が「無傷」とみなされ、判定が甘く出ます**
（例：64歳でも転職0回扱いだと Class-A・優先度高 になってしまう）。これを防ぐため、
取得できない項目があった場合の挙動を `QMATE_INCOMPLETE_MODE` で選べます。

| 値 | 挙動 |
|---|---|
| `hold`（**既定**） | 転職回数/短期離職数が不明なら**確定ランクを出さず「判定保留・要確認」**にする。Slackには年齢など取得できた情報＋「手動で確認してください」を表示 |
| `zero` | 不足分を 0 として暫定計算し「データ不足（要確認）」を付ける（旧挙動） |

- 年齢が取得できない場合は、モードに関わらず常に判定保留です（スコアが意味を成さないため）。
- 構造化列やAIで3項目が揃った応募は、これまで通り確定ランクで通知されます。

## 補足
- 取得・判定・通知の本体は Python標準ライブラリのみ。**備考からのAI抽出を使う場合のみ**
  `google-genai` が必要です（ワークフローで自動インストール／ローカルは `pip install google-genai`）。
- ポーリング間隔は `qmate-watch.yml` の `cron` で調整可能（GitHub Actionsの最短は約5分、
  実行は多少遅延します）。
- 重複通知の防止状態は GitHub Actions の cache（`qmate_state.json`）で実行間を引き継ぎます。
