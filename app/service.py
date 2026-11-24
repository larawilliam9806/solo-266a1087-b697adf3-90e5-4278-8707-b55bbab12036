"""业务规则：登记基线、上传候选、核对、确认与下游取数。"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from . import stats
from .csv_loader import ParsedBatch, UploadError
from .db import get_db, transaction

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"
STATUS_SUPERSEDED = "superseded"

REPORT_PASSED = "passed"
REPORT_REJECTED = "rejected"
REPORT_CONFIRMED = "confirmed"
REPORT_STALE = "stale"

# 报告 details 的格式版本：2 起为精确计数口径（上限按十进制精确比较，
# 判定所用计数与超限标记随报告留存）。缺失该标记的报告视为升级前旧口径。
REPORT_FORMAT = 2


class ServiceError(Exception):
    """可直接展示给值班员的业务错误。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class ExactThresholdText(str):
    """报告中留存的精确十进制上限文本。

    本质是字符串（页面原样展示所填数字），同时与同值的 int/float/Decimal
    宽松相等，方便回查时按数值或按文本核对。
    """

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return str.__eq__(self, other)
        if isinstance(other, bool):
            return False
        if isinstance(other, int):
            return Decimal(self) == Decimal(other)
        if isinstance(other, float):
            return Decimal(self) == Decimal(str(other))
        if isinstance(other, Decimal):
            return Decimal(self) == other
        return NotImplemented

    def __hash__(self) -> int:
        return str.__hash__(self)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_month(month: str) -> str:
    try:
        parsed = datetime.strptime(month, "%Y-%m")
    except ValueError:
        raise ServiceError("月份格式应为 YYYY-MM")
    if parsed.strftime("%Y-%m") != month:
        raise ServiceError("月份格式应为 YYYY-MM")
    return month


def validate_thresholds(values: dict[str, Any]) -> dict[str, Decimal]:
    """解析三项上限为精确十进制：只接受 0 到 1 之间的有限十进制数。"""
    thresholds: dict[str, Decimal] = {}
    labels = {
        "max_key_missing_ratio": "键缺失比例上限",
        "max_null_rate_diff": "空值率差上限",
        "max_bin_share_diff": "分箱占比差上限",
    }
    for key, label in labels.items():
        raw = values.get(key)
        if raw is None or str(raw).strip() == "":
            raise ServiceError(f"请填写{label}")
        text = str(raw).strip()
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ServiceError(f"{label}必须是有限十进制数")
        if number.is_nan() or number.is_infinite():
            raise ServiceError(f"{label}必须是有限十进制数")
        if number < 0 or number > 1:
            raise ServiceError(f"{label}应在 0 到 1 之间")
        thresholds[key] = number
    return thresholds


# ---------------------------------------------------------------------------
# 写：基线 / 候选
# ---------------------------------------------------------------------------


def _insert_rows(db: sqlite3.Connection, batch_id: int, parsed: ParsedBatch) -> None:
    db.executemany(
        "INSERT INTO batch_rows (batch_id, channel, day, amount, conversions)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (batch_id, row.channel, row.day, row.amount, row.conversions)
            for row in parsed.rows
        ],
    )


def _summarize_baseline(parsed: ParsedBatch) -> list[dict[str, Any]]:
    async_by_channel: dict[str, list] = defaultdict(list)
    for row in parsed.rows:
        async_by_channel[row.channel].append(row)

    summary: list[dict[str, Any]] = []
    for channel in parsed.channels:
        rows = async_by_channel[channel]
        key_count = len(rows)
        amount_values = [row.amount for row in rows if row.amount is not None]
        conversion_values = [
            row.conversions for row in rows if row.conversions is not None
        ]
        amount_null = key_count - len(amount_values)
        conversion_null = key_count - len(conversion_values)
        summary.append(
            {
                "channel": channel,
                "key_count": key_count,
                "amount_nonempty": len(amount_values),
                "amount_null_rate": amount_null / key_count,
                "amount_bins": stats.make_metric_bins(amount_values),
                "conversions_nonempty": len(conversion_values),
                "conversions_null_rate": conversion_null / key_count,
                "conversions_bins": stats.make_metric_bins(conversion_values),
            }
        )
    return summary


def register_baseline(month: str, filename: str, parsed: ParsedBatch) -> dict:
    validate_month(month)
    if parsed.month != month:
        raise ServiceError(f"文件属于 {parsed.month}，与所选月份 {month} 不一致")

    with transaction() as db:
        existing = db.execute(
            "SELECT id, version, status FROM batches WHERE month = ? ORDER BY version",
            (month,),
        ).fetchall()
        if existing:
            active = next(
                (row for row in existing if row["status"] == STATUS_ACTIVE), existing[-1]
            )
            raise ServiceError(
                f"{month} 已有批次（现行 V{active['version']}），"
                "已验收首版无需重复登记；后续请上传候选批核对。",
                409,
            )

        summary = _summarize_baseline(parsed)
        timestamp = now_iso()
        cursor = db.execute(
            "INSERT INTO batches (month, version, status, source_filename, row_count,"
            " channel_count, created_at, activated_at)"
            " VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
            (
                month,
                STATUS_ACTIVE,
                filename,
                len(parsed.rows),
                len(parsed.channels),
                timestamp,
                timestamp,
            ),
        )
        batch_id = int(cursor.lastrowid)
        _insert_rows(db, batch_id, parsed)
        db.executemany(
            "INSERT INTO batch_keys (batch_id, channel, day) VALUES (?, ?, ?)",
            [(batch_id, row.channel, row.day) for row in parsed.rows],
        )
        db.executemany(
            "INSERT INTO batch_channel_stats (batch_id, channel, key_count,"
            " amount_nonempty, amount_null_rate, amount_bins,"
            " conversions_nonempty, conversions_null_rate, conversions_bins)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    batch_id,
                    item["channel"],
                    item["key_count"],
                    item["amount_nonempty"],
                    item["amount_null_rate"],
                    json.dumps(item["amount_bins"], ensure_ascii=False),
                    item["conversions_nonempty"],
                    item["conversions_null_rate"],
                    json.dumps(item["conversions_bins"], ensure_ascii=False),
                )
                for item in summary
            ],
        )
        return {"id": batch_id, "version": 1}


def register_candidate(month: str, filename: str, parsed: ParsedBatch) -> dict:
    validate_month(month)
    if parsed.month != month:
        raise ServiceError(f"文件属于 {parsed.month}，与所选月份 {month} 不一致")

    with transaction() as db:
        active = db.execute(
            "SELECT id, version FROM batches WHERE month = ? AND status = ?",
            (month, STATUS_ACTIVE),
        ).fetchone()
        if active is None:
            raise ServiceError(
                f"{month} 尚无已验收基线，请先登记首版，再上传候选批。", 409
            )
        baseline_channels = {
            row["channel"]
            for row in db.execute(
                "SELECT channel FROM batch_channel_stats WHERE batch_id = ?",
                (active["id"],),
            ).fetchall()
        }
        candidate_channels = set(parsed.channels)
        missing_channels = sorted(baseline_channels - candidate_channels)
        if missing_channels:
            raise ServiceError(
                "缺少基线渠道，整批退回：" + "、".join(missing_channels)
            )

        version = db.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM batches WHERE month = ?",
            (month,),
        ).fetchone()["next"]
        cursor = db.execute(
            "INSERT INTO batches (month, version, status, source_filename, row_count,"
            " channel_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                month,
                version,
                STATUS_PENDING,
                filename,
                len(parsed.rows),
                len(parsed.channels),
                now_iso(),
            ),
        )
        batch_id = int(cursor.lastrowid)
        _insert_rows(db, batch_id, parsed)
        # 预存键与统计：本轮核对只用基线的预存数据，且该候选日后可成为基线。
        db.executemany(
            "INSERT INTO batch_keys (batch_id, channel, day) VALUES (?, ?, ?)",
            [(batch_id, row.channel, row.day) for row in parsed.rows],
        )
        summary = _summarize_baseline(parsed)
        db.executemany(
            "INSERT INTO batch_channel_stats (batch_id, channel, key_count,"
            " amount_nonempty, amount_null_rate, amount_bins,"
            " conversions_nonempty, conversions_null_rate, conversions_bins)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    batch_id,
                    item["channel"],
                    item["key_count"],
                    item["amount_nonempty"],
                    item["amount_null_rate"],
                    json.dumps(item["amount_bins"], ensure_ascii=False),
                    item["conversions_nonempty"],
                    item["conversions_null_rate"],
                    json.dumps(item["conversions_bins"], ensure_ascii=False),
                )
                for item in summary
            ],
        )
        return {"id": batch_id, "version": version, "baseline_version": active["version"]}


# ---------------------------------------------------------------------------
# 读：批次
# ---------------------------------------------------------------------------


def list_months() -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT month, status, version, id, row_count, channel_count, created_at,"
        " activated_at, activated_report_no FROM batches ORDER BY month, version"
    ).fetchall()
    months: dict[str, dict] = {}
    for row in rows:
        entry = months.setdefault(
            row["month"],
            {"month": row["month"], "active": None, "batch_count": 0,
             "pending_count": 0},
        )
        entry["batch_count"] += 1
        if row["status"] == STATUS_ACTIVE:
            entry["active"] = dict(row)
        elif row["status"] == STATUS_PENDING:
            entry["pending_count"] += 1
    return [months[key] for key in sorted(months, reverse=True)]


def get_active_batch(month: str) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM batches WHERE month = ? AND status = ?",
        (month, STATUS_ACTIVE),
    ).fetchone()
    return dict(row) if row else None


def list_batches(month: str) -> list[dict]:
    rows = get_db().execute(
        "SELECT b.*,"
        " (SELECT COUNT(*) FROM reports r WHERE r.candidate_batch_id = b.id)"
        " AS report_count"
        " FROM batches b WHERE b.month = ? ORDER BY b.version DESC",
        (month,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_batch(batch_id: int) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM batches WHERE id = ?", (batch_id,)
    ).fetchone()
    return dict(row) if row else None


def list_pending_batches(month: str) -> list[dict]:
    rows = get_db().execute(
        "SELECT * FROM batches WHERE month = ? AND status = ? ORDER BY version DESC",
        (month, STATUS_PENDING),
    ).fetchall()
    return [dict(row) for row in rows]


def get_batch_rows(batch_id: int, limit: int = 200, offset: int = 0) -> dict:
    db = get_db()
    batch = get_batch(batch_id)
    if batch is None:
        raise ServiceError("批次不存在", 404)
    total = db.execute(
        "SELECT COUNT(*) AS c FROM batch_rows WHERE batch_id = ?", (batch_id,)
    ).fetchone()["c"]
    rows = db.execute(
        "SELECT channel, day, amount, conversions FROM batch_rows"
        " WHERE batch_id = ? ORDER BY channel, day LIMIT ? OFFSET ?",
        (batch_id, limit, offset),
    ).fetchall()
    return {
        "batch": batch,
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": [dict(row) for row in rows],
    }


# ---------------------------------------------------------------------------
# 核对
# ---------------------------------------------------------------------------


def _finite_decimal_text(value: Fraction) -> str | None:
    """若分数可写成有限十进制，返回其精确小数字符串，否则返回 None。"""
    numerator, denominator = value.numerator, value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        return None
    places = max(twos, fives)
    scaled = numerator * (2 ** (places - twos)) * (5 ** (places - fives))
    sign = "-" if scaled < 0 else ""
    digits = str(abs(scaled)).rjust(places + 1, "0")
    if places:
        digits = digits[:-places] + "." + digits[-places:]
    return sign + digits


def _ratio_text(value: Fraction) -> str:
    """比例的精确文本：有限小数直接写出，否则给 分子/分母≈近似值。"""
    text = _finite_decimal_text(value)
    if text is not None:
        return text
    return f"{value.numerator}/{value.denominator}≈{float(value):.6f}"


def _diff_text(value: Fraction) -> str:
    """带符号的差值文本，用于报告原因与页面展示。"""
    if value == 0:
        return "0"
    text = _ratio_text(abs(value))
    return ("+" if value > 0 else "-") + text


def _candidate_channel_data(candidate_id: int) -> dict[str, dict]:
    """读取候选明细，按渠道聚合出键、空值率与两列非空值。"""
    rows = get_db().execute(
        "SELECT channel, day, amount, conversions FROM batch_rows WHERE batch_id = ?",
        (candidate_id,),
    ).fetchall()
    grouped: dict[str, dict] = {}
    for row in rows:
        bucket = grouped.setdefault(
            row["channel"],
            {"keys": set(), "row_count": 0, "amount_null": 0,
             "conversions_null": 0, "amount_values": [], "conversion_values": []},
        )
        bucket["keys"].add((row["channel"], row["day"]))
        bucket["row_count"] += 1
        if row["amount"] is None:
            bucket["amount_null"] += 1
        else:
            bucket["amount_values"].append(row["amount"])
        if row["conversions"] is None:
            bucket["conversions_null"] += 1
        else:
            bucket["conversion_values"].append(row["conversions"])
    return grouped


def _load_baseline_snapshot(baseline_id: int) -> dict:
    db = get_db()
    channel_stats = db.execute(
        "SELECT * FROM batch_channel_stats WHERE batch_id = ?", (baseline_id,)
    ).fetchall()
    keys_rows = db.execute(
        "SELECT channel, day FROM batch_keys WHERE batch_id = ?", (baseline_id,)
    ).fetchall()
    keys: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in keys_rows:
        keys[row["channel"]].add((row["channel"], row["day"]))
    return {"stats": channel_stats, "keys": keys}


def reconcile(candidate_id: int, raw_thresholds: dict[str, Any]) -> dict:
    """只读候选明细与基线预存键/统计，生成核对报告。

    读阶段使用独立的只读快照，不写库；报告写入阶段重新确认基线版本。
    三类偏差都由计数算出精确分数，与所填十进制上限按绝对值精确比较，
    等于上限也算达标。
    """
    thresholds = validate_thresholds(raw_thresholds)
    limits = {key: Fraction(value) for key, value in thresholds.items()}
    threshold_texts = {
        key: format(value, "f") for key, value in thresholds.items()
    }
    db = get_db()

    db.execute("BEGIN")
    try:
        candidate = db.execute(
            "SELECT * FROM batches WHERE id = ?", (candidate_id,)
        ).fetchone()
        if candidate is None:
            raise ServiceError("候选批次不存在", 404)
        if candidate["status"] != STATUS_PENDING:
            raise ServiceError("该批次已生效或已归档，不能作为候选核对", 409)
        baseline = db.execute(
            "SELECT * FROM batches WHERE month = ? AND status = ?",
            (candidate["month"], STATUS_ACTIVE),
        ).fetchone()
        if baseline is None:
            raise ServiceError(f"{candidate['month']} 尚无现行基线批", 409)
        baseline_id = int(baseline["id"])
        baseline_version = int(baseline["version"])
        candidate_data = _candidate_channel_data(candidate_id)
        snapshot = _load_baseline_snapshot(baseline_id)
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")

    details, violations = _evaluate(
        {
            "id": baseline_id,
            "version": baseline_version,
        },
        snapshot,
        candidate_data,
        limits,
        threshold_texts,
    )
    verdict = REPORT_REJECTED if violations else REPORT_PASSED

    report = _save_report(
        candidate_id=int(candidate["id"]),
        month=candidate["month"],
        candidate_version=int(candidate["version"]),
        baseline_id=baseline_id,
        baseline_version=baseline_version,
        thresholds=threshold_texts,
        details=details,
        verdict=verdict,
        violations=violations,
    )
    return report


def _evaluate(
    baseline: dict,
    snapshot: dict,
    candidate_data: dict[str, dict],
    limits: dict[str, Fraction],
    threshold_texts: dict[str, str],
) -> tuple[dict, list[str]]:
    violations: list[str] = []
    channels: list[dict] = []
    baseline_channels = sorted(row["channel"] for row in snapshot["stats"])
    candidate_channels = set(candidate_data)
    missing_channels = [ch for ch in baseline_channels if ch not in candidate_channels]
    extra_channels = sorted(candidate_channels - set(baseline_channels))

    if missing_channels:
        violations.append("缺少基线渠道：" + "、".join(missing_channels))

    for stat in snapshot["stats"]:
        channel = stat["channel"]
        base_keys = snapshot["keys"].get(channel, set())
        base_key_count = int(stat["key_count"])
        candidate = candidate_data.get(channel)
        candidate_keys = candidate["keys"] if candidate else set()
        missing_keys = base_keys - candidate_keys
        missing_ratio = (
            Fraction(len(missing_keys), base_key_count)
            if base_key_count
            else Fraction(0)
        )
        candidate_key_count = candidate["row_count"] if candidate else 0
        missing_over = abs(missing_ratio) > limits["max_key_missing_ratio"]

        channel_violations: list[str] = []
        if missing_over:
            reason = (
                f"渠道 {channel}：键缺失比例 {_ratio_text(missing_ratio)}"
                f"（缺 {len(missing_keys)} / {base_key_count} 个键），"
                f"超过上限 {threshold_texts['max_key_missing_ratio']}"
            )
            channel_violations.append(reason)

        amount_bins = json.loads(stat["amount_bins"])
        conversion_bins = json.loads(stat["conversions_bins"])
        amount_candidate = _metric_comparison(
            amount_bins,
            candidate["amount_values"] if candidate else [],
            candidate_key_count,
            candidate["amount_null"] if candidate else None,
            base_key_count,
            limits,
            present=candidate is not None,
        )
        conversion_candidate = _metric_comparison(
            conversion_bins,
            candidate["conversion_values"] if candidate else [],
            candidate_key_count,
            candidate["conversions_null"] if candidate else None,
            base_key_count,
            limits,
            present=candidate is not None,
        )

        for metric_label, comparison in (
            ("金额", amount_candidate),
            ("转化数", conversion_candidate),
        ):
            if not comparison["present"]:
                continue
            if comparison["null_rate_over"]:
                reason = (
                    f"渠道 {channel}：{metric_label}空值率差"
                    f" {comparison['null_rate_diff_text']}"
                    f"（候选空值 {comparison['candidate_null_count']}"
                    f"/{candidate_key_count}，"
                    f"基线空值 {comparison['baseline_null_count']}"
                    f"/{base_key_count}），"
                    f"超过上限 {threshold_texts['max_null_rate_diff']}"
                )
                channel_violations.append(reason)
            for label, diff_text, over, candidate_count, baseline_count in zip(
                comparison["labels"],
                comparison["share_diff_texts"] or [],
                comparison["share_overs"] or [],
                comparison["candidate_counts"],
                comparison["baseline_counts"],
            ):
                if over:
                    reason = (
                        f"渠道 {channel}：{metric_label}分箱「{label}」"
                        f"非空占比差 {diff_text}"
                        f"（候选 {candidate_count}"
                        f"/{comparison['candidate_nonempty']}，"
                        f"基线 {baseline_count}"
                        f"/{comparison['baseline_nonempty']}），"
                        f"超过上限 {threshold_texts['max_bin_share_diff']}"
                    )
                    channel_violations.append(reason)

        violations.extend(channel_violations)
        channels.append(
            {
                "channel": channel,
                "baseline_key_count": base_key_count,
                "candidate_key_count": candidate_key_count,
                "missing_key_count": len(missing_keys),
                "missing_ratio": float(missing_ratio),
                "missing_ratio_text": _ratio_text(missing_ratio),
                "missing_over": missing_over,
                "amount": amount_candidate,
                "conversions": conversion_candidate,
                "violations": channel_violations,
            }
        )

    details = {
        "format": REPORT_FORMAT,
        "baseline": baseline,
        "thresholds": threshold_texts,
        "channels": channels,
        "missing_channels": missing_channels,
        "extra_channels": extra_channels,
        "violations": violations,
    }
    return details, violations


def _metric_comparison(
    baseline_bins: dict,
    candidate_values: list[float],
    candidate_key_count: int,
    candidate_null_count: int | None,
    baseline_key_count: int,
    limits: dict[str, Fraction],
    *,
    present: bool,
) -> dict:
    """单项指标对比：空值率差与分箱占比差都由计数算出精确分数。

    基线侧只使用预存的键数、非空数与各箱计数；候选侧由明细计数。
    超限标记在此一次判定并随报告留存，回查页面直接读取。
    """
    cuts = baseline_bins["cuts"]
    candidate_bins = stats.build_bins(candidate_values, cuts)
    baseline_nonempty = int(baseline_bins["nonempty"])
    baseline_counts = [int(count) for count in baseline_bins["counts"]]
    baseline_null_count = baseline_key_count - baseline_nonempty
    baseline_null_rate = (
        Fraction(baseline_null_count, baseline_key_count)
        if baseline_key_count
        else Fraction(0)
    )
    candidate_nonempty = int(candidate_bins["nonempty"])
    candidate_counts = [int(count) for count in candidate_bins["counts"]]

    comparison: dict[str, Any] = {
        "present": present,
        "labels": baseline_bins["labels"],
        "cuts": cuts,
        "baseline_counts": baseline_counts,
        "candidate_counts": candidate_counts,
        "baseline_nonempty": baseline_nonempty,
        "candidate_nonempty": candidate_nonempty,
        "baseline_null_count": baseline_null_count,
        "baseline_null_rate": float(baseline_null_rate),
        "baseline_shares": [
            float(Fraction(count, baseline_nonempty)) if baseline_nonempty else 0.0
            for count in baseline_counts
        ],
        "candidate_shares": candidate_bins["shares"],
        "candidate_null_count": None,
        "candidate_null_rate": None,
        "null_rate_diff": None,
        "null_rate_diff_text": None,
        "null_rate_over": False,
        "share_diffs": None,
        "share_diff_texts": None,
        "share_overs": None,
    }
    if not present or candidate_key_count == 0:
        return comparison

    candidate_null_rate = Fraction(candidate_null_count, candidate_key_count)
    null_rate_diff = candidate_null_rate - baseline_null_rate
    share_diffs: list[Fraction] = []
    for candidate_count, baseline_count in zip(candidate_counts, baseline_counts):
        candidate_share = (
            Fraction(candidate_count, candidate_nonempty)
            if candidate_nonempty
            else Fraction(0)
        )
        baseline_share = (
            Fraction(baseline_count, baseline_nonempty)
            if baseline_nonempty
            else Fraction(0)
        )
        share_diffs.append(candidate_share - baseline_share)
    comparison.update(
        {
            "candidate_null_count": candidate_null_count,
            "candidate_null_rate": float(candidate_null_rate),
            "null_rate_diff": float(null_rate_diff),
            "null_rate_diff_text": _diff_text(null_rate_diff),
            "null_rate_over": abs(null_rate_diff) > limits["max_null_rate_diff"],
            "share_diffs": [float(diff) for diff in share_diffs],
            "share_diff_texts": [_diff_text(diff) for diff in share_diffs],
            "share_overs": [
                abs(diff) > limits["max_bin_share_diff"] for diff in share_diffs
            ],
        }
    )
    return comparison


def _next_report_no(db: sqlite3.Connection) -> str:
    prefix = "R" + datetime.now(timezone.utc).strftime("%Y%m%d")
    row = db.execute(
        "SELECT MAX(CAST(substr(report_no, ?) AS INTEGER)) AS last"
        " FROM reports WHERE report_no LIKE ?",
        (len(prefix) + 2, prefix + "-%"),
    ).fetchone()
    return f"{prefix}-{(row['last'] or 0) + 1:04d}"


def _save_report(
    *,
    candidate_id: int,
    month: str,
    candidate_version: int,
    baseline_id: int,
    baseline_version: int,
    thresholds: dict,
    details: dict,
    verdict: str,
    violations: list[str],
) -> dict:
    with transaction() as db:
        current = db.execute(
            "SELECT b.id, b.version FROM batches b"
            " WHERE b.month = ? AND b.status = ?",
            (month, STATUS_ACTIVE),
        ).fetchone()
        status = REPORT_PASSED if verdict == REPORT_PASSED else REPORT_REJECTED
        baseline_note = None
        if current is None or int(current["id"]) != baseline_id:
            status = REPORT_STALE
            baseline_note = (
                "核对期间基线已换版（当前"
                + (f" V{current['version']}" if current else "无现行批")
                + "），该结论需重算"
            )
        report_no = _next_report_no(db)
        cursor = db.execute(
            "INSERT INTO reports (report_no, month, baseline_batch_id,"
            " baseline_version, candidate_batch_id, candidate_version, thresholds,"
            " details, verdict, reasons, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report_no,
                month,
                baseline_id,
                baseline_version,
                candidate_id,
                candidate_version,
                json.dumps(thresholds, ensure_ascii=False),
                json.dumps(details, ensure_ascii=False),
                verdict,
                json.dumps(
                    violations + ([baseline_note] if baseline_note else []),
                    ensure_ascii=False,
                ),
                status,
                now_iso(),
            ),
        )
        return {"id": int(cursor.lastrowid), "report_no": report_no,
                "status": status, "verdict": verdict, "note": baseline_note}


# ---------------------------------------------------------------------------
# 确认
# ---------------------------------------------------------------------------


def get_report(report_no: str) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM reports WHERE report_no = ?", (report_no,)
    ).fetchone()
    if row is None:
        return None
    report = dict(row)
    report["thresholds"] = json.loads(report["thresholds"])
    report["details"] = json.loads(report["details"])
    report["reasons"] = json.loads(report["reasons"])
    if report["details"].get("format") != REPORT_FORMAT:
        _backfill_legacy_flags(report)
    else:
        report["thresholds"] = {
            key: ExactThresholdText(value) if isinstance(value, str) else value
            for key, value in report["thresholds"].items()
        }
        details_thresholds = report["details"].get("thresholds")
        if isinstance(details_thresholds, dict):
            report["details"]["thresholds"] = {
                key: ExactThresholdText(value) if isinstance(value, str) else value
                for key, value in details_thresholds.items()
            }
    return report


def _backfill_legacy_flags(report: dict) -> None:
    """为升级前的旧报告补算页面超限标记。

    旧报告没有随存的判定标记，这里按当时的浮点 > 口径补算，
    保证按号回查时页面展示与原始结论一致。
    """
    thresholds = report["thresholds"]

    def _number(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    for channel in report["details"].get("channels", []):
        missing_ratio = channel.get("missing_ratio")
        key_limit = thresholds.get("max_key_missing_ratio")
        channel["missing_over"] = (
            _number(missing_ratio)
            and _number(key_limit)
            and missing_ratio > key_limit
        )
        for metric_key in ("amount", "conversions"):
            metric = channel.get(metric_key)
            if not metric:
                continue
            diff = metric.get("null_rate_diff")
            null_limit = thresholds.get("max_null_rate_diff")
            metric["null_rate_over"] = (
                _number(diff) and _number(null_limit) and abs(diff) > null_limit
            )
            share_diffs = metric.get("share_diffs")
            share_limit = thresholds.get("max_bin_share_diff")
            metric["share_overs"] = (
                [
                    _number(diff)
                    and _number(share_limit)
                    and abs(diff) > share_limit
                    for diff in share_diffs
                ]
                if share_diffs
                else None
            )


def list_reports_for_month(month: str, limit: int = 100) -> list[dict]:
    rows = get_db().execute(
        "SELECT report_no, month, baseline_version, candidate_version, verdict,"
        " status, created_at, confirmed_at FROM reports WHERE month = ?"
        " ORDER BY id DESC LIMIT ?",
        (month, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def confirm_report(report_no: str) -> dict:
    """候选批次与通过报告原子生效；失败则现行批保持不变。

    返回 {"ok": bool, "message": str}；基线换版时 ok=False 且报告标记为失效。
    """
    with transaction() as db:
        report = db.execute(
            "SELECT * FROM reports WHERE report_no = ?", (report_no,)
        ).fetchone()
        if report is None:
            raise ServiceError("报告不存在", 404)
        if report["status"] == REPORT_CONFIRMED:
            raise ServiceError("该报告已确认生效", 409)
        if report["status"] == REPORT_STALE:
            raise ServiceError("基线已换版，该报告已失效，请重新核对", 409)
        if report["status"] != REPORT_PASSED:
            raise ServiceError("核对未通过，不能确认", 409)

        details = json.loads(report["details"])
        if details.get("format") != REPORT_FORMAT:
            raise ServiceError(
                "该报告是升级前的旧口径结论，不能确认；"
                "请用已上传的候选批重新核对，取得新通过报告后再确认。",
                409,
            )

        month = report["month"]
        active = db.execute(
            "SELECT * FROM batches WHERE month = ? AND status = ?",
            (month, STATUS_ACTIVE),
        ).fetchone()
        if active is None or int(active["id"]) != int(report["baseline_batch_id"]):
            db.execute(
                "UPDATE reports SET status = ? WHERE id = ? AND status = ?",
                (REPORT_STALE, report["id"], REPORT_PASSED),
            )
            current_version = active["version"] if active else None
            return {
                "ok": False,
                "message": (
                    "基线已换版（当前"
                    + (f" V{current_version}" if current_version else "无现行批")
                    + "），本报告基线 V"
                    + str(report["baseline_version"])
                    + " 已不是现行批，请重新核对后确认。"
                ),
            }

        candidate = db.execute(
            "SELECT * FROM batches WHERE id = ?", (report["candidate_batch_id"],)
        ).fetchone()
        if candidate is None or candidate["status"] != STATUS_PENDING:
            raise ServiceError("候选批次状态异常，无法确认", 409)

        timestamp = now_iso()
        db.execute(
            "UPDATE batches SET status = ? WHERE id = ?",
            (STATUS_SUPERSEDED, active["id"]),
        )
        db.execute(
            "UPDATE batches SET status = ?, activated_at = ?,"
            " activated_report_no = ? WHERE id = ?",
            (STATUS_ACTIVE, timestamp, report_no, candidate["id"]),
        )
        db.execute(
            "UPDATE reports SET status = ?, confirmed_at = ? WHERE id = ?",
            (REPORT_CONFIRMED, timestamp, report["id"]),
        )
        db.execute(
            "UPDATE reports SET status = ? WHERE month = ? AND status = ?"
            " AND id != ? AND baseline_batch_id = ?",
            (REPORT_STALE, month, REPORT_PASSED, report["id"], active["id"]),
        )
        return {
            "ok": True,
            "message": (
                f"{month} 现行批已切换为 V{candidate['version']}"
                f"（报告 {report_no}）"
            ),
            "candidate_version": candidate["version"],
        }


# ---------------------------------------------------------------------------
# 下游取数
# ---------------------------------------------------------------------------


def active_rows_page(month: str, limit: int, offset: int) -> dict:
    active = get_active_batch(month)
    if active is None:
        raise ServiceError(f"{month} 尚无现行批", 404)
    page = get_batch_rows(active["id"], limit, offset)
    page["month"] = month
    page["active"] = True
    return page


def active_rows_iter(month: str):
    active = get_active_batch(month)
    if active is None:
        raise ServiceError(f"{month} 尚无现行批", 404)
    cursor = get_db().execute(
        "SELECT channel, day, amount, conversions FROM batch_rows"
        " WHERE batch_id = ? ORDER BY channel, day",
        (active["id"],),
    )
    return active, cursor
