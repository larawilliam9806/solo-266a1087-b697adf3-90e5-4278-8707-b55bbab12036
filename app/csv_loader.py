"""CSV 解析与上传校验。

文件列：渠道、日期、金额、转化数（也接受英文列名）。
- 渠道 + 日期 为唯一键，重复即整批退回；
- 任一渠道的任一数值列全空即整批退回；
- 一个文件只能属于同一个月。
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

HEADER_ALIASES = {
    "渠道": "channel",
    "channel": "channel",
    "日期": "day",
    "date": "day",
    "金额": "amount",
    "amount": "amount",
    "转化数": "conversions",
    "conversion": "conversions",
    "conversions": "conversions",
}

DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d")
NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


class UploadError(Exception):
    """整批退回；reasons 为面向值班员的说明列表。"""

    def __init__(self, reasons: list[str]):
        super().__init__("；".join(reasons))
        self.reasons = reasons


@dataclass
class ParsedRow:
    channel: str
    day: str
    amount: float | None
    conversions: float | None


@dataclass
class ParsedBatch:
    month: str
    rows: list[ParsedRow] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)


def month_of(day_iso: str) -> str:
    return day_iso[:7]


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UploadError(["无法识别文件编码，请使用 UTF-8 或 GBK 保存的 CSV"])


def _parse_date(text: str) -> str | None:
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_number(text: str) -> tuple[float | None, bool]:
    """返回 (数值, 是否语法合法)；空串视为缺失值。"""
    text = text.strip().replace(",", "").replace("，", "")
    if text == "":
        return None, True
    if not NUMBER_RE.match(text):
        return None, False
    value = float(text)
    if not math.isfinite(value):
        return None, False
    return value, True


def parse_csv(raw: bytes) -> ParsedBatch:
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise UploadError(["文件为空"])

    header = [cell.strip() for cell in header]
    if not any(header):
        raise UploadError(["文件为空"])

    columns: dict[str, int] = {}
    for index, name in enumerate(header):
        canonical = HEADER_ALIASES.get(name.lower() if name.isascii() else name)
        if canonical is None:
            continue
        if canonical in columns:
            raise UploadError([f"表头列「{name}」重复"])
        columns[canonical] = index
    missing = [name for name in ("channel", "day", "amount", "conversions")
               if name not in columns]
    if missing:
        pretty = {"channel": "渠道", "day": "日期", "amount": "金额",
                  "conversions": "转化数"}
        raise UploadError(["缺少必需列：" + "、".join(pretty[name] for name in missing)])

    reasons: list[str] = []
    rows: list[ParsedRow] = []
    seen: set[tuple[str, str]] = set()
    duplicates: list[tuple[str, str]] = []
    months: set[str] = set()
    bad_channels = 0
    bad_days = 0
    bad_numbers: list[str] = []

    for line_no, raw_row in enumerate(reader, start=2):
        if not raw_row or all(cell.strip() == "" for cell in raw_row):
            continue
        width = max(columns.values()) + 1
        if len(raw_row) < width:
            reasons.append(f"第 {line_no} 行列数不足")
            continue

        channel = raw_row[columns["channel"]].strip()
        day_text = raw_row[columns["day"]].strip()
        amount_text = raw_row[columns["amount"]]
        conversions_text = raw_row[columns["conversions"]]

        if channel == "":
            bad_channels += 1
            continue
        day = _parse_date(day_text)
        if day is None:
            bad_days += 1
            continue

        amount, amount_ok = _parse_number(amount_text)
        conversions, conversions_ok = _parse_number(conversions_text)
        if not amount_ok:
            bad_numbers.append(f"第 {line_no} 行金额「{amount_text.strip()}」不是数字")
            continue
        if not conversions_ok:
            bad_numbers.append(f"第 {line_no} 行转化数「{conversions_text.strip()}」不是数字")
            continue

        key = (channel, day)
        if key in seen:
            duplicates.append(key)
            continue
        seen.add(key)
        months.add(month_of(day))
        rows.append(ParsedRow(channel, day, amount, conversions))

    if bad_channels:
        reasons.append(f"有 {bad_channels} 行渠道为空")
    if bad_days:
        reasons.append(f"有 {bad_days} 行日期无法识别")
    if bad_numbers:
        reasons.extend(bad_numbers[:10])
        if len(bad_numbers) > 10:
            reasons.append(f"另有 {len(bad_numbers) - 10} 行数值不合法")
    if duplicates:
        sample = "、".join(f"{channel}@{day}" for channel, day in duplicates[:5])
        reasons.append(f"存在 {len(duplicates)} 个重复键（{sample}）")
    if not rows and not reasons:
        reasons.append("文件没有数据行")
    if len(months) > 1:
        reasons.append("文件跨越多个月份：" + "、".join(sorted(months)))
    if reasons:
        raise UploadError(reasons)

    batch = ParsedBatch(month=months.pop(), rows=rows)
    batch.channels = sorted({row.channel for row in rows})

    # 任一渠道的任一数值列全空 → 退回
    null_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"amount_null": 0, "conversions_null": 0, "rows": 0}
    )
    for row in rows:
        stats = null_stats[row.channel]
        stats["rows"] += 1
        stats["amount_null"] += row.amount is None
        stats["conversions_null"] += row.conversions is None
    empty_columns = []
    for channel in batch.channels:
        stats = null_stats[channel]
        if stats["amount_null"] == stats["rows"]:
            empty_columns.append(f"{channel} 的金额全空")
        if stats["conversions_null"] == stats["rows"]:
            empty_columns.append(f"{channel} 的转化数全空")
    if empty_columns:
        raise UploadError(["存在整列为空的渠道：" + "；".join(empty_columns)])

    return batch
