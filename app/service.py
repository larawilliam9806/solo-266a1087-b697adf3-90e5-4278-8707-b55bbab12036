"""业务规则：登记基线、上传候选、核对、确认与下游取数。"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
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

EPSILON = 1e-12


class ServiceError(Exception):
    """可直接展示给值班员的业务错误。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


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


def validate_thresholds(values: dict[str, Any]) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    labels = {
        "max_key_missing_ratio": "键缺失比例上限",
        "max_null_rate_diff": "空值率差上限",
        "max_bin_share_diff": "分箱占比差上限",
    }
    for key, label in labels.items():
        raw = values.get(key)
        if raw is None or str(raw).strip() == "":
            raise ServiceError(f"请填写{label}")
        try:
            number = float(str(raw).strip())
        except ValueError:
            raise ServiceError(f"{label}必须是数字")
        if number != number or number in (float("inf"), float("-inf")):
            raise ServiceError(f"{label}必须是有限数字")
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


def _exceeds(value: float | None, limit: float) -> bool:
    return value is not None and abs(value) - limit > EPSILON


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
    """
    thresholds = validate_thresholds(raw_thresholds)
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
        thresholds,
    )
    verdict = REPORT_REJECTED if violations else REPORT_PASSED

    report = _save_report(
        candidate_id=int(candidate["id"]),
        month=candidate["month"],
        candidate_version=int(candidate["version"]),
        baseline_id=baseline_id,
        baseline_version=baseline_version,
        thresholds=thresholds,
        details=details,
        verdict=verdict,
        violations=violations,
    )
    return report


def _evaluate(
    baseline: dict,
    snapshot: dict,
    candidate_data: dict[str, dict],
    thresholds: dict[str, float],
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
            len(missing_keys) / base_key_count if base_key_count else 0.0
        )
        candidate_key_count = candidate["row_count"] if candidate else 0

        channel_violations: list[str] = []
        if _exceeds(missing_ratio, thresholds["max_key_missing_ratio"]):
            reason = (
                f"渠道 {channel}：键缺失比例 {missing_ratio:.4f}"
                f"（缺 {len(missing_keys)} / {base_key_count} 个键），"
                f"超过上限 {thresholds['max_key_missing_ratio']:.4f}"
            )
            channel_violations.append(reason)

        amount_bins = json.loads(stat["amount_bins"])
        conversion_bins = json.loads(stat["conversions_bins"])
        amount_candidate = _metric_comparison(
            amount_bins,
            candidate["amount_values"] if candidate else [],
            candidate_key_count,
            candidate["amount_null"] if candidate else None,
            float(stat["amount_null_rate"]),
            present=candidate is not None,
        )
        conversion_candidate = _metric_comparison(
            conversion_bins,
            candidate["conversion_values"] if candidate else [],
            candidate_key_count,
            candidate["conversions_null"] if candidate else None,
            float(stat["conversions_null_rate"]),
            present=candidate is not None,
        )

        for metric_label, comparison in (
            ("金额", amount_candidate),
            ("转化数", conversion_candidate),
        ):
            if not comparison["present"]:
                continue
            diff = comparison["null_rate_diff"]
            if _exceeds(diff, thresholds["max_null_rate_diff"]):
                reason = (
                    f"渠道 {channel}：{metric_label}空值率差 {diff:+.4f}，"
                    f"超过上限 {thresholds['max_null_rate_diff']:.4f}"
                )
                channel_violations.append(reason)
            for label, share_diff in zip(
                comparison["labels"], comparison["share_diffs"]
            ):
                if _exceeds(share_diff, thresholds["max_bin_share_diff"]):
                    reason = (
                        f"渠道 {channel}：{metric_label}分箱「{label}」"
                        f"非空占比差 {share_diff:+.4f}，"
                        f"超过上限 {thresholds['max_bin_share_diff']:.4f}"
                    )
                    channel_violations.append(reason)

        violations.extend(channel_violations)
        channels.append(
            {
                "channel": channel,
                "baseline_key_count": base_key_count,
                "candidate_key_count": candidate_key_count,
                "missing_key_count": len(missing_keys),
                "missing_ratio": missing_ratio,
                "amount": amount_candidate,
                "conversions": conversion_candidate,
                "violations": channel_violations,
            }
        )

    details = {
        "baseline": baseline,
        "thresholds": thresholds,
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
    baseline_null_rate: float,
    *,
    present: bool,
) -> dict:
    cuts = baseline_bins["cuts"]
    candidate_bins = stats.build_bins(candidate_values, cuts)
    if not present or candidate_key_count == 0:
        candidate_null_rate = None
        null_rate_diff = None
        share_diffs = None
    else:
        candidate_null_rate = candidate_null_count / candidate_key_count
        null_rate_diff = candidate_null_rate - baseline_null_rate
        share_diffs = [
            candidate_share - baseline_share
            for candidate_share, baseline_share in zip(
                candidate_bins["shares"], baseline_bins["shares"]
            )
        ]
    return {
        "present": present,
        "labels": baseline_bins["labels"],
        "cuts": cuts,
        "baseline_counts": baseline_bins["counts"],
        "candidate_counts": candidate_bins["counts"],
        "baseline_shares": baseline_bins["shares"],
        "candidate_shares": candidate_bins["shares"],
        "share_diffs": share_diffs,
        "baseline_nonempty": baseline_bins["nonempty"],
        "candidate_nonempty": candidate_bins["nonempty"],
        "baseline_null_rate": baseline_null_rate,
        "candidate_null_rate": candidate_null_rate,
        "null_rate_diff": null_rate_diff,
    }


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
    return report


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
