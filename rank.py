"""応募者ランク判定ロジック（UI非依存の純粋関数）。

Qmate連携バッチ（qmate_watcher.py）が使用する、ランク判定の純粋関数モジュール。
判定式はアプリ本体（modes/mode1_rank.py / modes/mode_auto.py の _calc_rank）と
同一（全パターンで一致を検証済み）で、Slack通知のランクが画面と必ず揃うようにしている。
将来的にはアプリ側も本モジュールへ寄せて一本化できる。

Streamlitには依存しないので、ヘッドレスなバッチからも安全にimportできる。
"""

from __future__ import annotations

# ランク区分のしきい値: (合計点の下限, ランク名, 表示カラー)。上から順に評価する。
RANK_TIERS = [
    (23, "優秀 (Class-S)", "#00ff00"),
    (18, "良好 (Class-A)", "#00e5ff"),
    (13, "標準 (Class-B)", "#ffff00"),
    (8,  "要努力 (Class-C)", "#ff9900"),
]
RANK_FALLBACK = ("測定不能 (Class-Z)", "#ff0000")


def calc_score(age: int, job_changes: int, short_term: int) -> int:
    """年齢・転職回数・短期離職数から合計スコアを算出する。"""
    if age < 20:
        age_s = -8
    elif 20 <= age <= 21:
        age_s = 8
    elif 22 <= age <= 25:
        age_s = 10
    elif 26 <= age <= 29:
        age_s = 8
    elif 30 <= age <= 35:
        age_s = 7
    else:
        age_s = 5

    job_bonus = 0
    if age <= 24 and job_changes == 0:
        job_bonus = 10
    elif 25 <= age <= 29 and job_changes <= 1:
        job_bonus = 10
    elif 25 <= age <= 29 and job_changes <= 2:
        job_bonus = 7
    elif 30 <= age <= 35 and job_changes <= 2:
        job_bonus = 10
    elif 30 <= age <= 35 and job_changes <= 3:
        job_bonus = 7
    elif 35 <= age <= 85 and job_changes <= 2:
        job_bonus = 10
    elif 35 <= age <= 85 and job_changes <= 3:
        job_bonus = 7
    elif 50 <= age <= 85 and job_changes <= 4:
        job_bonus = 5
    elif job_changes <= 1:
        job_bonus = 5

    job_penalty = 0
    if job_changes == 2:
        job_penalty = -5
    elif job_changes == 3:
        job_penalty = -10
    elif job_changes >= 5:
        job_penalty = -20

    return age_s + job_bonus + job_penalty - (short_term * 10) + 5


def priority_label(total: int) -> str:
    """エージェント向けの優先度ラベルを返す（高/中/低）。"""
    if total >= 15:
        return "高"
    elif total >= 7:
        return "中"
    return "低"


def calc_rank(age: int, job_changes: int, short_term: int) -> dict:
    """スコア・ランク名・表示カラー・優先度をまとめて返す。

    Returns:
        dict: {"total", "rank_name", "rank_color", "priority"}
    """
    total = calc_score(age, job_changes, short_term)
    rank_name, rank_color = RANK_FALLBACK
    for threshold, name, color in RANK_TIERS:
        if total >= threshold:
            rank_name, rank_color = name, color
            break
    return {
        "total": total,
        "rank_name": rank_name,
        "rank_color": rank_color,
        "priority": priority_label(total),
    }
