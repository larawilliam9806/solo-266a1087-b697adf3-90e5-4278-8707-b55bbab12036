from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app import service  # noqa: E402
from app.db import get_db  # noqa: E402
from app.stats import (  # noqa: E402
    bin_labels,
    build_bins,
    count_bins,
    quartile_cuts,
    quartile_positions,
)


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "SECRET_KEY": "test",
        }
    )
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def make_csv(rows, header="渠道,日期,金额,转化数"):
    lines = [header]
    for row in rows:
        lines.append(",".join("" if value is None else str(value) for value in row))
    return "\n".join(lines).encode("utf-8")


def upload(client, url, csv_bytes, filename="data.csv", month=None):
    data = {"csv": (io.BytesIO(csv_bytes), filename)}
    if month is not None:
        data["month"] = month
    return client.post(url, data=data, content_type="multipart/form-data",
                       follow_redirects=True)


def base_rows():
    rows = []
    for channel in ("A", "B"):
        for day in range(1, 9):
            rows.append((channel, f"2026-11-{day:02d}", day * 10, day))
    return rows


# ---------------------------------------------------------------------------
# 统计口径
# ---------------------------------------------------------------------------


def test_quartile_positions_use_ceiling():
    assert quartile_positions(1) == (1, 1, 1)
    assert quartile_positions(2) == (1, 1, 2)
    assert quartile_positions(3) == (1, 2, 3)
    assert quartile_positions(4) == (1, 2, 3)
    assert quartile_positions(5) == (2, 3, 4)
    assert quartile_positions(8) == (2, 4, 6)
    assert quartile_positions(9) == (3, 5, 7)


def test_quartile_cuts_merge_equal():
    assert quartile_cuts([1, 1, 1, 1]) == [1]
    assert quartile_cuts([1, 2, 3, 4]) == [1, 2, 3]
    assert quartile_cuts([5] * 8) == [5]


def test_bins_equal_to_cut_is_its_own_bin():
    values = [1, 2, 3, 4, 5, 6, 7, 8]
    cuts = quartile_cuts(values)  # [2, 4, 6]
    assert cuts == [2, 4, 6]
    counts = count_bins(values, cuts)
    # <2 | =2 | (2,4) | =4 | (4,6) | =6 | >6
    assert counts == [1, 1, 1, 1, 1, 1, 2]
    assert bin_labels(cuts) == [
        "< 2", "= 2", "(2, 4)", "= 4", "(4, 6)", "= 6", "> 6",
    ]


def test_bins_exclude_nulls_and_share_denominator_is_nonempty():
    result = build_bins([1, 2, 3, 4], quartile_cuts([1, 2, 3, 4]))
    assert result["nonempty"] == 4
    assert abs(sum(result["shares"]) - 1.0) < 1e-12


def test_single_distinct_value_produces_equal_bin_only():
    cuts = quartile_cuts([7, 7, 7])
    counts = count_bins([7, 7, 7], cuts)
    assert cuts == [7]
    assert counts == [0, 3, 0]


# ---------------------------------------------------------------------------
# CSV 校验
# ---------------------------------------------------------------------------


def test_duplicate_key_rejects_upload(client):
    rows = base_rows() + [("A", "2026-11-01", 99, 9)]
    response = upload(client, "/baseline", make_csv(rows), month="2026-11")
    assert "重复键" in response.get_data(as_text=True)
    with client.application.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


def test_all_null_metric_column_rejects_upload(client):
    rows = [(channel, day, None, 1) for channel, day in
            [("A", "2026-11-01"), ("A", "2026-11-02")]]
    response = upload(client, "/baseline", make_csv(rows), month="2026-11")
    assert "金额全空" in response.get_data(as_text=True)

    rows = [("A", "2026-11-01", 1, None), ("A", "2026-11-02", 2, None)]
    response = upload(client, "/baseline", make_csv(rows), month="2026-11")
    assert "转化数全空" in response.get_data(as_text=True)


def test_cross_month_file_rejected(client):
    rows = [("A", "2026-11-01", 1, 1), ("A", "2026-12-01", 2, 2)]
    response = upload(client, "/baseline", make_csv(rows), month="2026-11")
    assert "多个" in response.get_data(as_text=True) or "不一致" in response.get_data(as_text=True)


def test_missing_channel_rejects_candidate(client, app):
    upload(client, "/baseline", make_csv(base_rows()), month="2026-11")
    candidate = [row for row in base_rows() if row[0] == "A"]
    response = upload(client, "/months/2026-11/candidate", make_csv(candidate))
    text = response.get_data(as_text=True)
    assert "缺少基线渠道" in text
    with app.app_context():
        assert get_db().execute(
            "SELECT COUNT(*) FROM batches WHERE status = 'pending'"
        ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 流程：登记 → 候选 → 核对 → 确认
# ---------------------------------------------------------------------------


def _seed(app):
    client = app.test_client()
    response = upload(client, "/baseline", make_csv(base_rows()), month="2026-11")
    assert "已验收登记" in response.get_data(as_text=True)
    return client


def _candidate(app, rows, filename="cand.csv"):
    client = app.test_client()
    response = upload(client, "/months/2026-11/candidate", make_csv(rows),
                      filename=filename)
    assert response.status_code == 200
    with app.app_context():
        row = get_db().execute(
            "SELECT * FROM batches WHERE status = 'pending' ORDER BY id DESC"
        ).fetchone()
    assert row is not None
    return row


def _reconcile(app, batch_id, key=0.0, null=0.0, share=0.0):
    with app.app_context():
        return service.reconcile(
            batch_id,
            {
                "max_key_missing_ratio": key,
                "max_null_rate_diff": null,
                "max_bin_share_diff": share,
            },
        )


def test_identical_candidate_passes_and_confirms(app):
    client = _seed(app)
    candidate = _candidate(app, base_rows())
    report = _reconcile(app, candidate["id"])
    assert report["verdict"] == "passed"
    assert report["status"] == "passed"

    detail = client.get(f"/reports/{report['report_no']}")
    assert detail.status_code == 200

    confirm = client.post(f"/reports/{report['report_no']}/confirm",
                          follow_redirects=True)
    assert "现行批已切换" in confirm.get_data(as_text=True)
    with app.app_context():
        active = service.get_active_batch("2026-11")
        assert active["version"] == 2
        stored = service.get_report(report["report_no"])
        assert stored["status"] == "confirmed"
        old = get_db().execute(
            "SELECT status FROM batches WHERE version = 1"
        ).fetchone()
        assert old["status"] == "superseded"


def test_report_lookup_by_number_returns_original_conclusion(app):
    client = _seed(app)
    candidate = _candidate(app, base_rows())
    report = _reconcile(app, candidate["id"])
    page = client.get(f"/reports?report_no={report['report_no']}")
    text = page.get_data(as_text=True)
    assert report["report_no"] in text
    assert "通过" in text


def test_missing_keys_exceed_threshold_rejects(app):
    _seed(app)
    rows = [row for row in base_rows() if not (row[0] == "A" and row[1] == "2026-11-01")]
    candidate = _candidate(app, rows)
    report = _reconcile(app, candidate["id"], key=0.0)
    assert report["verdict"] == "rejected"
    with app.app_context():
        stored = service.get_report(report["report_no"])
    assert any("键缺失比例" in reason for reason in stored["reasons"])
    assert stored["status"] == "rejected"


def test_null_rate_difference_rejects(app):
    _seed(app)
    rows = []
    for row in base_rows():
        if row[0] == "A" and row[1] == "2026-11-02":
            rows.append((row[0], row[1], None, row[3]))
        else:
            rows.append(row)
    candidate = _candidate(app, rows)
    report = _reconcile(app, candidate["id"], null=0.01)
    assert report["verdict"] == "rejected"
    with app.app_context():
        stored = service.get_report(report["report_no"])
    assert any("金额空值率差" in reason for reason in stored["reasons"])


def test_bin_share_difference_rejects_and_uses_baseline_cuts(app):
    _seed(app)
    rows = []
    for channel, day, amount, conversions in base_rows():
        if channel == "A":
            amount = amount * 100
            conversions = conversions * 10
        rows.append((channel, day, amount, conversions))
    candidate = _candidate(app, rows)
    report = _reconcile(app, candidate["id"], share=0.05)
    assert report["verdict"] == "rejected"
    with app.app_context():
        stored = service.get_report(report["report_no"])
    assert any("分箱" in reason and "非空占比差" in reason for reason in stored["reasons"])

    details = stored["details"]
    amount = next(c for c in details["channels"] if c["channel"] == "A")["amount"]
    # 候选按基线的切点分箱：A 的金额全 > 基线 Q3
    assert amount["cuts"] == [20.0, 40.0, 60.0]
    assert amount["candidate_counts"] == [0, 0, 0, 0, 0, 0, 8]


def test_threshold_validation(client, app):
    _seed(app)
    candidate = _candidate(app, base_rows())
    response = client.post(
        f"/months/2026-11/batches/{candidate['id']}/reconcile",
        data={"max_key_missing_ratio": "2", "max_null_rate_diff": "0.1",
              "max_bin_share_diff": "0.1"},
        follow_redirects=True,
    )
    assert "0 到 1" in response.get_data(as_text=True)


def test_reconcile_read_only_and_allows_other_month_registration(app, monkeypatch):
    _seed(app)
    candidate = _candidate(app, base_rows())
    original = service._load_baseline_snapshot
    other_month_done = {}

    def during_read(*args, **kwargs):
        # 此时核对仍处于读取基线的只读事务中；另一月份应能照常登记。
        def register_other_month():
            client = app.test_client()
            rows = [("X", "2026-12-01", 1, 1)]
            upload(client, "/baseline", make_csv(rows), month="2026-12")
            other_month_done["ok"] = True

        thread = threading.Thread(target=register_other_month)
        thread.start()
        thread.join(timeout=20)
        other_month_done["finished"] = not thread.is_alive()
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_load_baseline_snapshot", during_read)
    report = _reconcile(app, candidate["id"])
    assert report["verdict"] == "passed"
    assert other_month_done == {"ok": True, "finished": True}
    with app.app_context():
        assert service.get_active_batch("2026-12") is not None


def test_confirm_reports_stale_when_baseline_changed(app):
    _seed(app)
    candidate = _candidate(app, base_rows())
    report = _reconcile(app, candidate["id"])
    assert report["verdict"] == "passed"

    # 模拟核对后基线被别的候选替换：直接切换 V1 → V3
    with app.app_context():
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE batches SET status = 'superseded' WHERE id = 1")
        cursor = db.execute(
            "INSERT INTO batches (month, version, status, row_count, channel_count,"
            " created_at) VALUES ('2026-11', 3, 'active', 16, 2, 'now')"
        )
        db.execute("COMMIT")
        new_active_id = cursor.lastrowid

    with app.app_context():
        outcome = service.confirm_report(report["report_no"])
        assert outcome["ok"] is False
        assert "基线已换版" in outcome["message"]
        active = service.get_active_batch("2026-11")
        assert active["id"] == new_active_id
        stored = service.get_report(report["report_no"])
        assert stored["status"] == "stale"


def test_confirm_failure_keeps_original_batch(app):
    _seed(app)
    candidate = _candidate(app, base_rows())
    report = _reconcile(app, candidate["id"])

    with app.app_context():
        db = get_db()
        db.execute(
            "CREATE TRIGGER abort_confirm BEFORE UPDATE OF status ON reports"
            " WHEN NEW.status = 'confirmed'"
            " BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
        )
        with pytest.raises(sqlite3.Error):
            service.confirm_report(report["report_no"])
        active = service.get_active_batch("2026-11")
        assert active["version"] == 1
        pending = db.execute(
            "SELECT status FROM batches WHERE id = ?", (candidate["id"],)
        ).fetchone()
        assert pending["status"] == "pending"
        assert service.get_report(report["report_no"])["status"] == "passed"


def test_rejected_report_cannot_be_confirmed(app):
    _seed(app)
    rows = [row for row in base_rows() if not (row[0] == "B" and row[1] == "2026-11-03")]
    candidate = _candidate(app, rows)
    report = _reconcile(app, candidate["id"], key=0.0)
    assert report["verdict"] == "rejected"
    with app.app_context():
        with pytest.raises(service.ServiceError):
            service.confirm_report(report["report_no"])


def test_new_candidate_can_become_baseline_for_next_round(app):
    _seed(app)
    candidate = _candidate(app, base_rows())
    report = _reconcile(app, candidate["id"])
    with app.app_context():
        service.confirm_report(report["report_no"])

    # V2 现在作为基线：缺其渠道应被退回；同数据候选可再次通过
    client = app.test_client()
    bad = upload(client, "/months/2026-11/candidate", make_csv(
        [row for row in base_rows() if row[0] == "A"]
    ))
    assert "缺少基线渠道" in bad.get_data(as_text=True)

    second = _candidate(app, base_rows())
    with app.app_context():
        baseline = service.get_active_batch("2026-11")
        assert baseline["version"] == 2
        report2 = service.reconcile(
            second["id"],
            {"max_key_missing_ratio": 0, "max_null_rate_diff": 0,
             "max_bin_share_diff": 0},
        )
    assert report2["verdict"] == "passed"


def test_downstream_active_batch_switches_only_after_confirm(app):
    client = _seed(app)
    rows = base_rows()
    rows[0] = ("A", "2026-11-01", 999, 99)
    candidate = _candidate(app, rows)
    report = _reconcile(app, candidate["id"], share=1.0, null=1.0, key=1.0)
    assert report["verdict"] == "passed"

    before = client.get("/months/2026-11/active.csv").get_data(as_text=True)
    assert "999" not in before

    client.post(f"/reports/{report['report_no']}/confirm", follow_redirects=True)
    after = client.get("/months/2026-11/active.csv").get_data(as_text=True)
    assert "999" in after


def test_pending_batch_cannot_be_reconciled_twice_as_candidate(app):
    _seed(app)
    candidate = _candidate(app, base_rows())
    report = _reconcile(app, candidate["id"])
    with app.app_context():
        service.confirm_report(report["report_no"])
        with pytest.raises(service.ServiceError):
            service.reconcile(
                candidate["id"],
                {"max_key_missing_ratio": 0, "max_null_rate_diff": 0,
                 "max_bin_share_diff": 0},
            )


def test_report_numbers_increment_and_stay_unique(app):
    _seed(app)
    numbers = []
    for _ in range(3):
        candidate = _candidate(app, base_rows())
        numbers.append(_reconcile(app, candidate["id"])["report_no"])
    assert len(set(numbers)) == 3
    assert numbers == sorted(numbers)
    with app.app_context():
        stored = [service.get_report(number) for number in numbers]
    assert [item["report_no"] for item in stored] == numbers
    assert stored[0]["report_no"].split("-")[-1] == "0001"


def test_confirmed_report_cannot_be_confirmed_again(app):
    _seed(app)
    candidate = _candidate(app, base_rows())
    report = _reconcile(app, candidate["id"])
    with app.app_context():
        service.confirm_report(report["report_no"])
        with pytest.raises(service.ServiceError):
            service.confirm_report(report["report_no"])


def test_reconcile_does_not_mutate_batches(app):
    """核对只读明细与基线预存统计：批次状态保持原样。"""
    _seed(app)
    candidate = _candidate(app, base_rows())
    with app.app_context():
        before = [
            dict(row)
            for row in get_db().execute(
                "SELECT id, status, version FROM batches ORDER BY id"
            ).fetchall()
        ]
    report = _reconcile(app, candidate["id"])
    assert report["verdict"] == "passed"
    with app.app_context():
        after = [
            dict(row)
            for row in get_db().execute(
                "SELECT id, status, version FROM batches ORDER BY id"
            ).fetchall()
        ]
    assert before == after
    assert [row["status"] for row in after] == ["active", "pending"]


def test_each_candidate_gets_independent_report_and_stale_on_other_confirm(app):
    _seed(app)
    first = _candidate(app, base_rows())
    first_report = _reconcile(app, first["id"])
    second = _candidate(app, base_rows())
    second_report = _reconcile(app, second["id"])
    assert first_report["report_no"] != second_report["report_no"]

    with app.app_context():
        assert service.confirm_report(second_report["report_no"])["ok"] is True
        # 第一份报告基于的基线已被替换，确认时提示重算
        with pytest.raises(service.ServiceError, match="基线已换版"):
            service.confirm_report(first_report["report_no"])
        assert service.get_report(first_report["report_no"])["status"] == "stale"
        assert service.get_active_batch("2026-11")["version"] == 3
        assert get_db().execute(
            "SELECT status FROM batches WHERE id = ?", (first["id"],)
        ).fetchone()["status"] == "pending"


# ---------------------------------------------------------------------------
# 精确比较：上限为有限十进制，偏差从计数算出，等于上限也达标
# ---------------------------------------------------------------------------


def test_ratio_text_exact_rendering():
    from fractions import Fraction

    assert service._ratio_text(Fraction(0)) == "0"
    assert service._ratio_text(Fraction(1, 2)) == "0.5"
    assert service._ratio_text(Fraction(1, 4)) == "0.25"
    assert service._ratio_text(Fraction(1, 8)) == "0.125"
    assert service._ratio_text(Fraction(1, 10)) == "0.1"
    assert service._ratio_text(Fraction(3, 40)) == "0.075"
    assert service._ratio_text(Fraction(7, 2)) == "3.5"
    assert service._ratio_text(Fraction(1, 3)) == "1/3≈0.333333"
    assert service._ratio_text(Fraction(2, 7)) == "2/7≈0.285714"
    assert service._diff_text(Fraction(1, 10)) == "+0.1"
    assert service._diff_text(Fraction(-1, 8)) == "-0.125"
    assert service._diff_text(Fraction(0)) == "0"
    assert service._diff_text(Fraction(-1, 3)) == "-1/3≈0.333333"


def _seed_rows(app, rows, month="2026-11"):
    client = app.test_client()
    response = upload(client, "/baseline", make_csv(rows), month=month)
    assert "已验收登记" in response.get_data(as_text=True)
    return client


def _downgrade_report_to_legacy(app, report_no):
    """把报告改写成升级前的旧格式（无 format 标记、上限为浮点数）。"""
    with app.app_context():
        db = get_db()
        row = db.execute(
            "SELECT thresholds, details FROM reports WHERE report_no = ?",
            (report_no,),
        ).fetchone()
        thresholds = {
            key: float(value)
            for key, value in json.loads(row["thresholds"]).items()
        }
        details = json.loads(row["details"])
        details.pop("format", None)
        details["thresholds"] = thresholds
        for channel in details["channels"]:
            channel.pop("missing_ratio_text", None)
            channel.pop("missing_over", None)
            for metric_key in ("amount", "conversions"):
                metric = channel[metric_key]
                for key in (
                    "null_rate_diff_text",
                    "null_rate_over",
                    "share_diff_texts",
                    "share_overs",
                    "baseline_null_count",
                    "candidate_null_count",
                ):
                    metric.pop(key, None)
        db.execute(
            "UPDATE reports SET thresholds = ?, details = ? WHERE report_no = ?",
            (json.dumps(thresholds), json.dumps(details), report_no),
        )


def test_key_missing_ratio_exact_boundary(app):
    """基线 3 天、候选缺 1 天：1/3 精确大于 0.3333333333333333，应退回。"""
    _seed_rows(app, [("A", f"2026-11-0{d}", d * 10, d) for d in (1, 2, 3)])
    rows = [("A", "2026-11-01", 10, 1), ("A", "2026-11-02", 20, 2)]
    candidate = _candidate(app, rows)
    report = _reconcile(
        app, candidate["id"], key="0.3333333333333333", null=1, share=1
    )
    assert report["verdict"] == "rejected"
    with app.app_context():
        stored = service.get_report(report["report_no"])
    assert any("键缺失比例" in reason for reason in stored["reasons"])
    # 退回的报告没有确认入口
    page = app.test_client().get(f"/reports/{report['report_no']}")
    assert "确认生效</button>" not in page.get_data(as_text=True)

    # 上限略大于 1/3 即可通过
    candidate2 = _candidate(app, rows, filename="cand2.csv")
    report2 = _reconcile(
        app, candidate2["id"], key="0.3333333333333334", null=1, share=1
    )
    assert report2["verdict"] == "passed"


def test_key_missing_ratio_equal_to_limit_passes(app):
    """缺失比例恰为有限小数上限（1/4 = 0.25）时达标。"""
    _seed_rows(app, [("A", f"2026-11-0{d}", d * 10, d) for d in (1, 2, 3, 4)])
    rows = [("A", f"2026-11-0{d}", d * 10, d) for d in (1, 2, 3)]
    candidate = _candidate(app, rows)
    report = _reconcile(app, candidate["id"], key="0.25", null=1, share=1)
    assert report["verdict"] == "passed"


def test_null_rate_diff_equal_limit_passes_and_page_consistent(app):
    """空值率 3/10 → 4/10，差恰为 0.1，上限 0.1：通过且页面不标红。"""
    base = [
        ("A", f"2026-11-{d:02d}", (None if d <= 3 else d * 10), d)
        for d in range(1, 11)
    ]
    _seed_rows(app, base)
    rows = [
        ("A", f"2026-11-{d:02d}", (None if d <= 4 else d * 10), d)
        for d in range(1, 11)
    ]
    candidate = _candidate(app, rows)
    report = _reconcile(app, candidate["id"], key=1, null="0.1", share=1)
    assert report["verdict"] == "passed"
    with app.app_context():
        stored = service.get_report(report["report_no"])
    assert stored["details"]["format"] == 2
    channel = stored["details"]["channels"][0]
    assert channel["amount"]["null_rate_diff_text"] == "+0.1"
    assert channel["amount"]["null_rate_over"] is False
    page = app.test_client().get(f"/reports/{report['report_no']}")
    assert 'class="over"' not in page.get_data(as_text=True)


def test_bin_share_diff_exact_boundary(app):
    """占比差恰为 0.25 时达标；上限小任意一点即退回。"""
    _seed_rows(app, [("A", f"2026-11-0{d}", d, d) for d in (1, 2, 3, 4)])
    rows = [
        ("A", "2026-11-01", 1, 1),
        ("A", "2026-11-02", 1, 2),
        ("A", "2026-11-03", 2, 3),
        ("A", "2026-11-04", 3, 4),
    ]
    candidate = _candidate(app, rows)
    report = _reconcile(app, candidate["id"], key=1, null=1, share="0.25")
    assert report["verdict"] == "passed"

    candidate2 = _candidate(app, rows, filename="cand2.csv")
    report2 = _reconcile(
        app, candidate2["id"], key=1, null=1, share="0.24999999999999999"
    )
    assert report2["verdict"] == "rejected"
    with app.app_context():
        stored = service.get_report(report2["report_no"])
    assert any("非空占比差" in reason for reason in stored["reasons"])


def test_threshold_validation_finite_decimal(app):
    _seed(app)
    for accepted in ("0.3333333333333333", "1e-1", "0", "1", "0.0000000000000001"):
        thresholds = service.validate_thresholds(
            {
                "max_key_missing_ratio": accepted,
                "max_null_rate_diff": accepted,
                "max_bin_share_diff": accepted,
            }
        )
        assert all(0 <= value <= 1 for value in thresholds.values())
    for rejected, message in (
        ("abc", "有限十进制"),
        ("nan", "有限十进制"),
        ("inf", "有限十进制"),
        ("-0.1", "0 到 1"),
        ("1.5", "0 到 1"),
        ("", "请填写"),
    ):
        with pytest.raises(service.ServiceError) as excinfo:
            service.validate_thresholds(
                {
                    "max_key_missing_ratio": rejected,
                    "max_null_rate_diff": "0.1",
                    "max_bin_share_diff": "0.1",
                }
            )
        assert message in str(excinfo.value)


def test_reconcile_form_posts_long_decimal_threshold(client, app):
    _seed(app)
    candidate = _candidate(app, base_rows())
    page = client.get(f"/months/2026-11/batches/{candidate['id']}")
    assert 'step="any"' in page.get_data(as_text=True)
    response = client.post(
        f"/months/2026-11/batches/{candidate['id']}/reconcile",
        data={
            "max_key_missing_ratio": "0.3333333333333333",
            "max_null_rate_diff": "0.1",
            "max_bin_share_diff": "0.05",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "核对通过" in response.get_data(as_text=True)


def test_new_report_keeps_thresholds_and_counts(app):
    """新报告保留所填上限与判定所用计数，页面标记与结论一致。"""
    _seed_rows(app, [("A", f"2026-11-0{d}", d * 10, d) for d in (1, 2, 3)])
    rows = [("A", "2026-11-01", 10, 1), ("A", "2026-11-02", 20, 2)]
    candidate = _candidate(app, rows)
    report = _reconcile(
        app, candidate["id"], key="0.3333333333333333", null="0.10", share="0.5"
    )
    assert report["verdict"] == "rejected"
    with app.app_context():
        stored = service.get_report(report["report_no"])
    # 所填上限按十进制文本原样留存
    assert stored["thresholds"] == {
        "max_key_missing_ratio": "0.3333333333333333",
        "max_null_rate_diff": "0.10",
        "max_bin_share_diff": "0.5",
    }
    channel = stored["details"]["channels"][0]
    # 判定所用计数与超限标记随报告留存
    assert channel["baseline_key_count"] == 3
    assert channel["candidate_key_count"] == 2
    assert channel["missing_key_count"] == 1
    assert channel["missing_ratio_text"] == "1/3≈0.333333"
    assert channel["missing_over"] is True
    assert channel["amount"]["baseline_null_count"] == 0
    assert channel["amount"]["candidate_null_count"] == 0
    assert channel["amount"]["null_rate_over"] is False
    assert channel["amount"]["share_overs"] is not None

    # 回查页面：超限标记、退回原因与结论一致
    page = app.test_client().get(f"/reports/{report['report_no']}")
    text = page.get_data(as_text=True)
    assert 'class="over"' in text
    assert "1/3≈0.333333" in text
    assert "超过上限 0.3333333333333333" in text
    assert "核对未通过" in text


def test_legacy_passed_report_cannot_be_confirmed(app):
    """升级前通过但未确认的报告：不能确认，页面提示重新核对。"""
    _seed(app)
    candidate = _candidate(app, base_rows())
    report = _reconcile(app, candidate["id"])
    assert report["verdict"] == "passed"
    _downgrade_report_to_legacy(app, report["report_no"])

    with app.app_context():
        with pytest.raises(service.ServiceError, match="重新核对"):
            service.confirm_report(report["report_no"])
        # 报告状态、候选批与现行批均保持不变
        assert service.get_report(report["report_no"])["status"] == "passed"
        assert service.get_active_batch("2026-11")["version"] == 1
        assert get_db().execute(
            "SELECT status FROM batches WHERE id = ?", (candidate["id"],)
        ).fetchone()["status"] == "pending"

    page = app.test_client().get(f"/reports/{report['report_no']}")
    text = page.get_data(as_text=True)
    assert "旧口径" in text
    assert "重新核对" in text
    assert "确认生效</button>" not in text

    # 用已上传的候选批重新核对，取得新通过报告后可以确认
    new_report = _reconcile(app, candidate["id"])
    assert new_report["verdict"] == "passed"
    with app.app_context():
        outcome = service.confirm_report(new_report["report_no"])
        assert outcome["ok"] is True
        assert service.get_active_batch("2026-11")["version"] == 2


def test_legacy_report_lookup_keeps_original_conclusion(app):
    """旧报告仍可按号查原结论，页面按当时口径展示。"""
    _seed(app)
    rows = [
        row
        for row in base_rows()
        if not (row[0] == "B" and row[1] == "2026-11-03")
    ]
    candidate = _candidate(app, rows)
    rejected = _reconcile(app, candidate["id"], key=0.0)
    assert rejected["verdict"] == "rejected"
    _downgrade_report_to_legacy(app, rejected["report_no"])

    client = app.test_client()
    page = client.get(f"/reports/{rejected['report_no']}")
    text = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "核对未通过" in text
    assert "键缺失比例" in text
    assert 'class="over"' in text  # 旧口径页面标记保持原样

    search = client.get(f"/reports?report_no={rejected['report_no']}")
    search_text = search.get_data(as_text=True)
    assert rejected["report_no"] in search_text
    assert "退回" in search_text
