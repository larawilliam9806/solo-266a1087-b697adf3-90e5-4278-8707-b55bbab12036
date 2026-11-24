"""统计口径：四分位切点与分箱。

所有分箱都只使用基线批次的四分位切点：
- 分位点 = 非空值升序第 ceil(样本数 * 分位比例) 项（1 起算）；
- 三个分位比例 1/4、1/2、3/4，相同切点合并；
- 切点本身单独成箱，区间箱只包含严格介于两个切点之间的值。
"""

from __future__ import annotations

import bisect
from typing import Iterable, Sequence


def quartile_positions(sample_size: int) -> tuple[int, int, int]:
    """返回 Q1、Q2、Q3 在升序非空样本中的 1 起算位置。

    使用整数运算实现 ceil(n * q)，避免浮点误差：
    ceil(n/4) = (n+3)//4, ceil(n/2) = (n+1)//2, ceil(3n/4) = (3n+3)//4。
    """
    if sample_size <= 0:
        raise ValueError("样本数必须为正")
    return (
        (sample_size + 3) // 4,
        (sample_size + 1) // 2,
        (3 * sample_size + 3) // 4,
    )


def quartile_cuts(sorted_values: Sequence[float]) -> list[float]:
    """按规定的分位点位置从升序非空值中取切点，并合并相同切点。"""
    n = len(sorted_values)
    if n == 0:
        return []
    cuts: list[float] = []
    for position in quartile_positions(n):
        value = sorted_values[position - 1]
        if not cuts or value != cuts[-1]:
            cuts.append(value)
    return cuts


def fmt_number(value: float) -> str:
    """用于分箱标签的紧凑数字格式（不参与计算）。"""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.10g}"


def bin_labels(cuts: Sequence[float]) -> list[str]:
    """生成与 count_bins 对齐的箱标签。

    切点 c1 < c2 < ... < ck 对应 2k+1 个箱：
    (<c1), (=c1), (c1,c2), (=c2), ..., (=ck), (>ck)。
    """
    if not cuts:
        return ["（无非空值）"]
    labels: list[str] = []
    for index, cut in enumerate(cuts):
        if index == 0:
            labels.append(f"< {fmt_number(cut)}")
        else:
            labels.append(f"({fmt_number(cuts[index - 1])}, {fmt_number(cut)})")
        labels.append(f"= {fmt_number(cut)}")
    labels.append(f"> {fmt_number(cuts[-1])}")
    return labels


def count_bins(values: Iterable[float], cuts: Sequence[float]) -> list[int]:
    """按切点统计每个箱的样本数；等于切点的值进入等值箱。"""
    counts = [0] * (2 * len(cuts) + 1)
    for value in values:
        position = bisect.bisect_left(cuts, value)
        if position < len(cuts) and cuts[position] == value:
            counts[2 * position + 1] += 1
        else:
            counts[2 * position] += 1
    return counts


def build_bins(values: Iterable[float], cuts: Sequence[float]) -> dict:
    """用给定切点统计一组非空值，返回可直接存 JSON 的分箱结果。"""
    materialized = list(values)
    counts = count_bins(materialized, cuts)
    total = len(materialized)
    shares = [(count / total) if total else 0.0 for count in counts]
    return {
        "cuts": [float(cut) for cut in cuts],
        "labels": bin_labels(cuts),
        "counts": counts,
        "nonempty": total,
        "shares": shares,
    }


def make_metric_bins(nonempty_values: Iterable[float]) -> dict:
    """为一组非空值计算自己的四分位切点并分箱（用于基线预存统计）。"""
    values = sorted(nonempty_values)
    return build_bins(values, quartile_cuts(values))
