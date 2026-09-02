#!/usr/bin/env python3
"""Qmate応募者 → ランク自動判定 → Slack通知 バッチ。

Qmate WebAPI「応募者一覧取得」(CSV応答) から対象日の応募者を取得し、
新規応募者についてアプリと同一基準（rank.py）でランク判定を行い、
Slack（Incoming Webhook）へ通知する。

Qmate APIはWebhook（プッシュ）非対応のため、本スクリプトを
GitHub Actions / cron で定期実行（ポーリング）して使う想定。
標準ライブラリのみで動作する（追加パッケージ不要）。

────────────────────────────────────────────────────────────
環境変数
  QMATE_API_KEY        Qmateの各社APIキー（必須・秘密情報）
  SLACK_WEBHOOK_URL    Slack Incoming Webhook URL（必須・秘密情報）
  QMATE_API_URL        既定: https://m.q-mate.jp/extapicsv/v1/applicant/list
  QMATE_EXPORT_DATE    取得対象日 yyyy/mm/dd（既定: 今日 JST）
  QMATE_STATE_FILE     通知済み管理ファイル（既定: qmate_state.json）
  QMATE_CSV_FILE       APIの代わりにローカルCSVを読む（テスト用）
  QMATE_DRY_RUN        "1" でSlack送信せず内容を標準出力（テスト用）
  QMATE_NOTIFY_ON_FIRST_RUN  "1" で初回（状態ファイル無し）でも既存応募を通知
                             （既定: 初回は通知せず現状を既読として記録）
  QMATE_COL_<FIELD>    列名の明示指定で自動判定を上書き。FIELD=
                       ID / NAME / LAST_NAME / GIVEN_NAME / AGE / DOB / JOB_CHANGES /
                       SHORT_TERM / COMPANY / JOB / DATE / STATUS / REMARKS
                       例: QMATE_COL_REMARKS="備考"
                       ※ 氏名列が無い場合は LAST_NAME(姓)+GIVEN_NAME(名) から組み立て、
                          年齢列が無い場合は DOB(生年月日) から算出する。
  GEMINI_API_KEY       備考などからのAI抽出に使用（カンマ区切りで複数キー可）
  QMATE_AI_MODEL       AI抽出に使うモデル（既定: gemini-2.5-flash）
  QMATE_INCOMPLETE_MODE  転職回数/短期離職数を取得できない場合の扱い。
                       zero(既定)=年齢で判定（不足分は0として暫定計算） /
                       hold=確定ランクを出さず「判定保留」にする（50歳未満のみ）。
  ※ Slack通知は S〜C ランクのみ（測定不能Class-Z・年齢50歳以上の一律Z・判定保留は通知しない）。
────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from rank import calc_rank

DEFAULT_API_URL = "https://m.q-mate.jp/extapicsv/v1/applicant/list"
DEFAULT_STATE_FILE = "qmate_state.json"
STATE_MAX_KEYS = 5000  # 状態ファイルの肥大化防止（古いものから捨てる）

# 列名の自動判定ヒント。各ヒントは「|必須」等の注記を除いた基本名での完全一致を優先し、
# 2文字以上のヒントのみ部分一致も許可する（"名"が"求人職種名"等に誤爆するのを防ぐ）。
# 実CSVに合わせて調整するか、QMATE_COL_<FIELD> で個別に上書きできる。
COLUMN_HINTS = {
    "id":           ["uid", "応募id", "応募ＩＤ", "管理番号", "応募番号", "応募者id"],
    # 氏名が1列にある場合（無ければ下の 姓+名 から組み立てる）
    "name":         ["氏名", "応募者名", "お名前", "名前"],
    "last_name":    ["姓"],
    "given_name":   ["名"],
    "age":          ["年齢", "ご年齢"],
    "dob":          ["生年月日", "誕生日"],
    "job_changes":  ["転職回数", "転職社数"],
    "short_term":   ["短期離職", "短期離職数"],
    "company":      ["掲載企業名", "応募企業名", "企業名", "会社名"],
    "job":          ["求人職種名", "応募求人", "求人名", "募集求人", "応募職種", "求人"],
    "date":         ["応募日時", "応募日", "登録日時", "登録日", "日時"],
    "status":       ["選考ステータス", "ステータス", "選考状況"],
    # 転職回数/短期離職数などが独立列に無く、職歴が自由記述で書かれている列（AI抽出の入力）
    "remarks":      ["備考", "特記事項", "特記", "職歴", "経歴", "自己PR", "コメント", "補足", "通信欄"],
}


def log(msg: str) -> None:
    print(f"[qmate] {msg}", flush=True)


# ──────────────────────────────────────────────
# 取得（API or ローカルCSV）
# ──────────────────────────────────────────────
def jst_today() -> str:
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    return now.strftime("%Y/%m/%d")


def decode_bytes(raw: bytes) -> str:
    """日本語CSVの想定エンコーディングを順に試す。"""
    for enc in ("utf-8-sig", "cp932", "shift_jis", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_csv_text(cfg: dict) -> str:
    """ローカルCSV（テスト用）またはQmate APIからCSV本文を取得する。"""
    if cfg["csv_file"]:
        log(f"ローカルCSVを読み込み: {cfg['csv_file']}")
        with open(cfg["csv_file"], "rb") as f:
            return decode_bytes(f.read())

    data = urllib.parse.urlencode(
        {"api_key": cfg["api_key"], "export_date": cfg["export_date"]}
    ).encode("utf-8")
    log(f"Qmate API取得: {cfg['api_url']} (export_date={cfg['export_date']})")
    req = urllib.request.Request(cfg["api_url"], data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return decode_bytes(resp.read())


# ──────────────────────────────────────────────
# パース・列マッピング・値抽出
# ──────────────────────────────────────────────
def parse_rows(text: str):
    """CSV本文を (ヘッダ一覧, 行dictのリスト) に変換する。"""
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = [h.strip() for h in (reader.fieldnames or [])]
    rows = []
    for row in reader:
        rows.append({(k.strip() if k else k): (v if v is not None else "") for k, v in row.items()})
    return fieldnames, rows


def looks_like_error_csv(fieldnames, rows) -> bool:
    """Qmateがエラー時に返す error.csv っぽいか判定する。"""
    joined = " ".join(fieldnames).lower()
    if "error" in joined or "エラー" in joined:
        return True
    # 列が1つしかなく中身がエラーメッセージのみ、等
    if len(fieldnames) <= 1 and rows:
        return True
    return False


def _match_header(hint: str, fieldnames: list):
    """ヒントに一致するヘッダ名を返す。

    1) 「|必須」等の注記を除いた基本名での完全一致（"姓"/"名" の単独列に対応）
    2) 2文字以上のヒントのみ部分一致（"名"が"求人職種名"等に誤爆するのを防ぐ）
    """
    hint_l = hint.lower()
    for h in fieldnames:
        base = h.split("|")[0].strip().lower()
        if base == hint_l:
            return h
    if len(hint) >= 2:
        for h in fieldnames:
            if hint_l in h.lower():
                return h
    return None


def resolve_columns(fieldnames, cfg: dict) -> dict:
    """各論理フィールドに対応する実ヘッダ名を決める（env上書き優先）。"""
    resolved = {}
    for field, hints in COLUMN_HINTS.items():
        override = cfg["col_overrides"].get(field)
        if override and override in fieldnames:
            resolved[field] = override
            continue
        found = None
        for hint in hints:
            found = _match_header(hint, fieldnames)
            if found:
                break
        resolved[field] = found
    return resolved


def cell(row: dict, col):
    return (row.get(col) or "").strip() if col else ""


def extract_int(value: str):
    if not value:
        return None
    m = re.search(r"-?\d+", value.replace(",", ""))
    return int(m.group()) if m else None


def age_from_dob(value: str):
    """生年月日文字列から満年齢を概算する。"""
    if not value:
        return None
    nums = re.findall(r"\d+", value)
    if len(nums) < 3:
        return None
    y, mth, d = int(nums[0]), int(nums[1]), int(nums[2])
    if y < 1900 or not (1 <= mth <= 12) or not (1 <= d <= 31):
        return None
    today = datetime.date.today()
    return today.year - y - ((today.month, today.day) < (mth, d))


def applicant_key(row: dict, cols: dict) -> str:
    """重複通知を防ぐための安定キー。ID列があれば優先、無ければ氏名＋応募日時のハッシュ。"""
    rid = cell(row, cols.get("id"))
    if rid:
        return f"id:{rid}"
    basis = "|".join([
        cell(row, cols.get("name")) or cell(row, cols.get("last_name")) + cell(row, cols.get("given_name")),
        cell(row, cols.get("date")),
        cell(row, cols.get("job")),
    ])
    return "h:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


# ──────────────────────────────────────────────
# 自由記述（備考など）からのAI抽出
# ──────────────────────────────────────────────
# Geminiの失敗は原因で対処が違う。
#   quota    : 429 / RESOURCE_EXHAUSTED（そのキーの枠切れ）→ 次のキーへ
#   overload : 503 UNAVAILABLE など（Google側の混雑）→ キーを替えても直らないので待つ
QUOTA_MARKERS = ("429", "RESOURCE_EXHAUSTED", "quota")
OVERLOAD_MARKERS = ("503", "UNAVAILABLE", "overloaded", "high demand", "INTERNAL", "DEADLINE_EXCEEDED")
AI_OVERLOAD_BACKOFF = (5, 15, 30)


def classify_api_error(message: str) -> str:
    """APIエラー文字列を quota / overload / fatal に分類する。"""
    lowered = str(message).lower()
    if any(m.lower() in lowered for m in QUOTA_MARKERS):
        return "quota"
    if any(m.lower() in lowered for m in OVERLOAD_MARKERS):
        return "overload"
    return "fatal"


def extract_with_ai(text: str, keys: list, model: str):
    """備考などの自由記述から 年齢/転職回数/短期離職数 を抽出する（Gemini）。

    年齢・転職回数・短期離職数が独立した列に無く、備考に書かれているケース用の補完。
    失敗時は None を返し、呼び出し側でフォールバックする。
    google-genai は遅延importなので、AI抽出を使わない環境では依存不要。
    """
    if not text or not keys:
        return None
    try:
        from google import genai  # 遅延import
    except Exception as e:
        log(f"google-genai 未導入のためAI抽出をスキップ: {e}")
        return None

    prompt = (
        "次の応募者メモから以下をJSONのみで出力（マークダウン・説明文は禁止）。\n"
        '{"age": 数値かnull, "job_changes": 数値かnull, "short_term": 数値かnull}\n'
        "定義: age=年齢 / job_changes=転職回数(在職中も含めた合計社数-1) / "
        "short_term=1年以内での短期離職の回数。読み取れない項目は null。\n"
        f"【応募者メモ】\n{text[:4000]}"
    )

    def _to_int(v):
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            m = re.search(r"-?\d+", v)
            return int(m.group()) if m else None
        return None

    last_err = ""
    last_kind = ""
    for key in keys:
        overload_used = 0
        while True:
            try:
                client = genai.Client(api_key=key)
                resp = client.models.generate_content(model=model, contents=prompt)
                raw = (resp.text or "").replace("```json", "").replace("```", "").strip()
                data = json.loads(raw)
                return {
                    "age": _to_int(data.get("age")),
                    "job_changes": _to_int(data.get("job_changes")),
                    "short_term": _to_int(data.get("short_term")),
                }
            except Exception as e:
                last_err = str(e)
                last_kind = classify_api_error(last_err)

                # Google側の混雑（503）はキーを替えても直らないので、待って同じキーで再試行する。
                if last_kind == "overload" and overload_used < len(AI_OVERLOAD_BACKOFF):
                    wait = AI_OVERLOAD_BACKOFF[overload_used]
                    overload_used += 1
                    log(f"AI抽出: Gemini混雑（503）。{wait}秒待って再試行します。")
                    time.sleep(wait)
                    continue

                if last_kind in ("quota", "overload"):
                    break  # このキーでは無理 → 次のキーへ

                log(f"AI抽出エラー: {last_err}")
                return None

    if last_kind == "overload":
        log(f"AI抽出: Gemini側が混雑中のため取得できませんでした（{last_err}）")
    else:
        log(f"AI抽出: 全APIキーが制限に達しました（{last_err}）")
    return None


# AI抽出に渡す「職歴の手掛かり」を広く集めるための列名ヒント。
# 転職回数・短期離職数は備考以外（転職経験・入社/退職年・直近企業名など）にも散在するため、
# 該当しそうな列をまとめてAIに渡し、抽出できる確率を上げる。連絡先・住所等のPIIは除外する。
_CAREER_HINTS = (
    "備考", "特記", "職歴", "経歴", "転職", "経験", "入社", "退職", "在職", "就業",
    "直近", "現職", "現在", "年収", "学歴", "学校", "学部", "資格", "志望",
    "きっかけ", "メモ", "自由質問", "回答", "職種", "雇用", "コメント", "補足", "通信欄",
)
_CAREER_EXCLUDE = (
    "電話", "メール", "mail", "郵便", "住所", "都道府県", "市区", "番地",
    "セイ", "メイ", "uid",
)


def build_career_text(row: dict) -> str:
    """転職回数・短期離職などの手掛かりになりそうな列だけを集めてAI入力テキストにする。"""
    parts = []
    for k, v in row.items():
        if not k or not v:
            continue
        base = k.split("|")[0].strip()
        low = base.lower()
        if any(x in base or x in low for x in _CAREER_EXCLUDE):
            continue
        if any(h in base for h in _CAREER_HINTS):
            parts.append(f"{base}: {v}")
    return "\n".join(parts)


# ──────────────────────────────────────────────
# 判定 & Slack整形
# ──────────────────────────────────────────────
def evaluate(row: dict, cols: dict, cfg: dict) -> dict:
    """1応募者を判定し、通知用の情報をまとめる。

    年齢・転職回数・短期離職数が構造化列に無い場合は、備考などの自由記述から
    AI抽出して補完する（ランク式はアプリと同一）。
    """
    name = cell(row, cols.get("name"))
    if not name:  # 氏名列が無い場合は 姓+名 から組み立てる
        last = cell(row, cols.get("last_name"))
        given = cell(row, cols.get("given_name"))
        name = f"{last} {given}".strip()
    name = name or "（氏名不明）"
    company = cell(row, cols.get("company"))
    job = cell(row, cols.get("job"))
    status = cell(row, cols.get("status"))
    date = cell(row, cols.get("date"))

    age = extract_int(cell(row, cols.get("age")))
    if age is None:
        age = age_from_dob(cell(row, cols.get("dob")))
    jc = extract_int(cell(row, cols.get("job_changes")))
    st_cnt = extract_int(cell(row, cols.get("short_term")))

    # 構造化列に無い項目は、備考や職歴系カラム(転職経験/入社・退職年/直近企業名など)からAI抽出して補完
    ai_source = []
    if (age is None or jc is None or st_cnt is None) and cfg.get("gemini_keys"):
        # 備考だけでなく職歴の手掛かりを広く渡す。拾えなければ備考単独→行全体の順でフォールバック。
        ai_text = (
            build_career_text(row)
            or cell(row, cols.get("remarks"))
            or " / ".join(f"{k}:{v}" for k, v in row.items() if v)
        )
        ai = extract_with_ai(ai_text, cfg["gemini_keys"], cfg["ai_model"])
        if ai:
            if age is None and ai.get("age") is not None:
                age = ai["age"]; ai_source.append("年齢")
            if jc is None and ai.get("job_changes") is not None:
                jc = ai["job_changes"]; ai_source.append("転職回数")
            if st_cnt is None and ai.get("short_term") is not None:
                st_cnt = ai["short_term"]; ai_source.append("短期離職数")

    missing = []
    if age is None:
        missing.append("年齢")
    if jc is None:
        missing.append("転職回数")
    if st_cnt is None:
        missing.append("短期離職数")

    info = {
        "name": name, "company": company, "job": job, "status": status, "date": date,
        "age": age, "job_changes": jc, "short_term": st_cnt,
        "missing": missing, "ai_source": ai_source,
    }

    # 年齢が無いとスコアが出せないため常に判定保留（通知対象外）。
    # 既定: 年齢があれば、転職回数/短期離職が不明でも年齢で判定する（不足分は0で暫定計算）。
    #       50歳以上は calc_rank が一律Zにする。
    # QMATE_INCOMPLETE_MODE=hold にすると、50歳未満で項目不足の場合は判定保留にできる。
    hold = (age is None) or (
        age < 50 and missing and cfg.get("incomplete_mode", "zero") == "hold"
    )
    if hold:
        info["rank"] = None
        return info

    info["rank"] = calc_rank(age, jc or 0, st_cnt or 0)
    return info


PRIORITY_EMOJI = {"高": ":fire:", "中": ":warning:", "低": ":large_blue_circle:"}


def build_slack_payload(info: dict) -> dict:
    rank = info["rank"]
    if rank is None:
        miss = "・".join(info.get("missing") or []) or "必要項目"
        header = ":grey_question: 新規応募（判定保留・要確認）"
        rank_line = f"ランク: 判定保留（{miss}が取得できず。0埋めは誤判定の恐れがあるため自動判定を保留）"
    else:
        header = f"{PRIORITY_EMOJI.get(rank['priority'], '')} 新規応募 / 優先度：{rank['priority']}"
        if rank.get("forced_z_age"):
            rank_line = f"ランク: {rank['rank_name']}　優先度: {rank['priority']}（年齢50歳以上のため一律 測定不能）"
        else:
            rank_line = f"ランク: {rank['rank_name']}　スコア: {rank['total']}　優先度: {rank['priority']}"

    def show(v):
        return v if (v is not None and v != "") else "—"

    applied = " / ".join(x for x in [info.get("company"), info.get("job")] if x) or "—"
    detail = (
        f"*氏名*: {info['name']}\n"
        f"*応募先*: {applied}\n"
        f"*年齢*: {show(info['age'])}　*転職回数*: {show(info['job_changes'])}　*短期離職*: {show(info['short_term'])}\n"
        f"*応募日時*: {show(info['date'])}\n"
        f"{rank_line}"
    )
    if info.get("status"):
        detail += f"\n*選考ステータス*: {info['status']}"
    if info.get("ai_source"):
        detail += f"\n:robot_face: 備考・職歴からAI抽出: {', '.join(info['ai_source'])}（要確認）"
    if info["missing"]:
        if rank is None:
            detail += f"\n:small_red_triangle: 取得できなかった項目: {', '.join(info['missing'])} → 手動で確認してください"
        elif not rank.get("forced_z_age"):
            detail += f"\n:small_red_triangle: データ不足（要確認）: {', '.join(info['missing'])}（不足分は0で暫定計算）"

    text = f"{header}\n{detail}"
    return {
        "text": text,  # 通知/フォールバック用
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
            {"type": "section", "text": {"type": "mrkdwn", "text": detail}},
        ],
    }


def post_slack(webhook: str, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


# ──────────────────────────────────────────────
# 状態（通知済み）管理
# ──────────────────────────────────────────────
def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"seen": [], "updated": ""}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("seen", [])
        return data
    except Exception as e:
        log(f"状態ファイル読込失敗（新規扱い）: {e}")
        return {"seen": [], "updated": ""}


def save_state(path: str, seen_keys) -> None:
    trimmed = list(seen_keys)[-STATE_MAX_KEYS:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"seen": trimmed, "updated": datetime.datetime.now().isoformat()},
            f, ensure_ascii=False, indent=0,
        )


# ──────────────────────────────────────────────
# 設定読み込み
# ──────────────────────────────────────────────
def load_config() -> dict:
    col_overrides = {}
    for field in COLUMN_HINTS:
        v = os.environ.get(f"QMATE_COL_{field.upper()}")
        if v:
            col_overrides[field] = v.strip()
    return {
        "api_key": os.environ.get("QMATE_API_KEY", "").strip(),
        "webhook": os.environ.get("SLACK_WEBHOOK_URL", "").strip(),
        "api_url": os.environ.get("QMATE_API_URL", DEFAULT_API_URL).strip(),
        "export_date": os.environ.get("QMATE_EXPORT_DATE", "").strip() or jst_today(),
        "state_file": os.environ.get("QMATE_STATE_FILE", DEFAULT_STATE_FILE).strip(),
        "csv_file": os.environ.get("QMATE_CSV_FILE", "").strip(),
        "dry_run": os.environ.get("QMATE_DRY_RUN", "") == "1",
        "notify_first_run": os.environ.get("QMATE_NOTIFY_ON_FIRST_RUN", "") == "1",
        "col_overrides": col_overrides,
        # 備考などからの年齢/転職回数/短期離職数のAI抽出に使用（カンマ区切りで複数キー可）
        "gemini_keys": [k.strip() for k in os.environ.get("GEMINI_API_KEY", "").split(",") if k.strip()],
        "ai_model": os.environ.get("QMATE_AI_MODEL", "gemini-2.5-flash").strip(),
        # データ不足時の扱い: "zero"=年齢で判定（不足分は0で暫定計算・既定） / "hold"=判定保留
        "incomplete_mode": (os.environ.get("QMATE_INCOMPLETE_MODE", "zero").strip().lower() or "zero"),
    }


def masked_ref(info: dict) -> str:
    """公開されるActionsログ用の、氏名を含まない参照子。

    このリポジトリはpublicでActionsログも公開されるため、ログには氏名等のPIIを出さず、
    非可逆ハッシュの短い識別子のみを記録する（ログ行どうしの突き合わせ用）。
    """
    basis = f"{info.get('name', '')}|{info.get('date', '')}".encode("utf-8")
    return "応募者#" + hashlib.sha1(basis).hexdigest()[:6]


def is_notifiable(info: dict) -> bool:
    """Slack通知の対象判定: S〜Cランクのみ通知する。

    測定不能(Class-Z)・年齢50歳以上の一律Z・判定保留(rank=None)は通知しない。
    """
    rank = info.get("rank")
    if not rank or rank.get("forced_z_age"):
        return False
    return "Class-Z" not in rank.get("rank_name", "")


# ──────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────
def main() -> int:
    cfg = load_config()

    # 秘密情報が未設定なら（初期セットアップ前など）エラーにせずスキップ。
    # → 定期実行のCIが赤くなり続けるのを防ぐ。
    if not cfg["csv_file"] and not cfg["api_key"]:
        log("QMATE_API_KEY 未設定のためスキップ（セットアップ前と判断）。")
        return 0
    if not cfg["dry_run"] and not cfg["webhook"]:
        log("SLACK_WEBHOOK_URL 未設定のためスキップ（DRY_RUNでもありません）。")
        return 0

    try:
        text = fetch_csv_text(cfg)
    except Exception as e:
        log(f"CSV取得に失敗: {e}")
        return 0  # 一時的な失敗で定期実行を赤くしない

    fieldnames, rows = parse_rows(text)
    if not fieldnames:
        log("CSVが空、またはヘッダを読めませんでした。")
        return 0
    if looks_like_error_csv(fieldnames, rows):
        log(f"Qmateがエラー応答(error.csv)を返した可能性があります。内容先頭: {text[:300]!r}")
        return 0

    cols = resolve_columns(fieldnames, cfg)
    log(f"検出ヘッダ: {fieldnames}")
    log(f"列マッピング: {{ {', '.join(f'{k}->{v}' for k, v in cols.items() if v)} }}")
    unresolved = []
    if not cols.get("name") and not (cols.get("last_name") and cols.get("given_name")):
        unresolved.append("name(または姓+名)")
    if not cols.get("age") and not cols.get("dob"):
        unresolved.append("age(またはdob)")
    if unresolved:
        log(f"⚠️ 重要な列を自動判定できませんでした: {unresolved} → QMATE_COL_* で指定するか COLUMN_HINTS を調整してください。")
    if cfg["gemini_keys"]:
        log(f"AI補完: 有効（{cfg['ai_model']}）。構造化列に無い年齢/転職回数/短期離職数を備考・職歴系カラムから抽出します。")
    else:
        log("AI補完: 無効（GEMINI_API_KEY未設定）。")
    if cfg.get("incomplete_mode") == "hold":
        log("データ不足時: holdモード（50歳未満で転職回数/短期離職が不明なら判定保留）。")
    else:
        log("データ不足時: 年齢で判定（不足分は0で暫定計算）。")
    log("通知対象: S〜Cランクのみ（測定不能Class-Z・年齢50歳以上の一律Z・判定保留は通知しません）。")

    state = load_state(cfg["state_file"])
    seen = list(state.get("seen", []))
    seen_set = set(seen)
    first_run = not os.path.exists(cfg["state_file"])

    current_keys = [applicant_key(r, cols) for r in rows]
    new_indices = [i for i, k in enumerate(current_keys) if k not in seen_set]
    log(f"応募 {len(rows)}件 / 新規 {len(new_indices)}件 / 状態ファイル: "
        f"{'なし(初回)' if first_run else cfg['state_file']}")

    # 初回はシードのみ（既存応募の大量通知を防止）。明示時のみ通知。
    if first_run and not cfg["notify_first_run"] and not cfg["dry_run"]:
        for k in current_keys:
            if k not in seen_set:
                seen.append(k); seen_set.add(k)
        save_state(cfg["state_file"], seen)
        log("初回実行のため既存応募を既読として記録（通知なし）。"
            "次回以降の新規応募から通知します。QMATE_NOTIFY_ON_FIRST_RUN=1 で初回も通知可。")
        return 0

    notified = 0
    skipped = 0
    for i in new_indices:
        info = evaluate(rows[i], cols, cfg)

        # S〜Cランクのみ通知。測定不能(Z)・年齢50歳以上の一律Z・判定保留は通知せず既読化のみ。
        # 公開Actionsログに氏名を残さないため、ログ上は masked_ref（非個人情報）で参照する。
        if not is_notifiable(info):
            r = info.get("rank")
            label = r["rank_name"] if r else "判定保留（年齢不明）"
            log(f"通知対象外（S〜Cのみ通知）: {masked_ref(info)} → {label}")
            skipped += 1
            seen.append(current_keys[i]); seen_set.add(current_keys[i])
            continue

        if cfg["dry_run"]:
            # DRY RUNの内容は公開ログに出るため、表示用は氏名をマスクする。
            masked = {**info, "name": masked_ref(info)}
            log("---- DRY RUN (Slack未送信) ----")
            print(build_slack_payload(masked)["text"])
            notified += 1
        else:
            try:
                post_slack(cfg["webhook"], build_slack_payload(info))  # 本番Slackは実名で送信
                notified += 1
            except Exception as e:
                log(f"Slack送信失敗（{masked_ref(info)}）: {e}")
                continue  # 失敗分は既読にせず次回再試行
        seen.append(current_keys[i]); seen_set.add(current_keys[i])

    if not cfg["dry_run"]:
        save_state(cfg["state_file"], seen)
    log(f"完了: 新規 {len(new_indices)}件中 {notified}件をSlack通知 / {skipped}件は対象外(Z・保留)"
        f"{'（DRY RUN）' if cfg['dry_run'] else ''}。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
