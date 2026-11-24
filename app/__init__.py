"""Flask 应用工厂与页面路由。"""

from __future__ import annotations

import os

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import db as database
from . import service
from .csv_loader import UploadError, parse_csv
from .service import ServiceError


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        DATABASE=os.environ.get(
            "DATABASE_PATH", os.path.join(app.instance_path, "reconcile.sqlite3")
        ),
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,
        PAGE_SIZE=100,
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.instance_path, exist_ok=True)
    database_dir = os.path.dirname(os.path.abspath(app.config["DATABASE"]))
    os.makedirs(database_dir, exist_ok=True)
    database.init_app(app)

    with app.app_context():
        database.init_db()

    @app.context_processor
    def inject_helpers():
        return {
            "fmt_pct": _fmt_pct,
            "fmt_diff": _fmt_diff,
            "fmt_threshold": _fmt_threshold,
        }

    @app.get("/")
    def index():
        months = service.list_months()
        return render_template("index.html", months=months)

    @app.get("/months/<month>")
    def month_detail(month: str):
        month = _valid_month(month)
        active = service.get_active_batch(month)
        batches = service.list_batches(month)
        reports = service.list_reports_for_month(month)
        return render_template(
            "month.html",
            month=month,
            active=active,
            batches=batches,
            reports=reports,
            pending=service.list_pending_batches(month),
        )

    # -- 登记基线 ---------------------------------------------------------
    @app.route("/baseline", methods=["GET", "POST"])
    def register_baseline():
        if request.method == "POST":
            try:
                month, parsed, filename = _read_upload()
                result = service.register_baseline(month, filename, parsed)
            except (ServiceError, UploadError) as error:
                _flash_error(error)
                return redirect(url_for("register_baseline"))
            flash(
                f"{month} 首版基线 V{result['version']} 已验收登记。", "success"
            )
            return redirect(url_for("month_detail", month=month))
        return render_template(
            "upload.html", mode="baseline", months=service.list_months()
        )

    # -- 上传候选 ---------------------------------------------------------
    @app.route("/months/<month>/candidate", methods=["GET", "POST"])
    def upload_candidate(month: str):
        month = _valid_month(month)
        active = service.get_active_batch(month)
        if request.method == "POST":
            if active is None:
                flash(f"{month} 尚无已验收基线，请先登记首版。", "error")
                return redirect(url_for("upload_candidate", month=month))
            try:
                _, parsed, filename = _read_upload(month=month)
                result = service.register_candidate(month, filename, parsed)
            except (ServiceError, UploadError) as error:
                _flash_error(error)
                return redirect(url_for("upload_candidate", month=month))
            flash(
                f"候选批 V{result['version']} 已上传，可与现行基线"
                f" V{result['baseline_version']} 核对。",
                "success",
            )
            return redirect(
                url_for(
                    "batch_detail", month=month, batch_id=result["id"]
                )
            )
        return render_template(
            "upload.html", mode="candidate", month=month, active=active
        )

    @app.get("/months/<month>/batches/<int:batch_id>")
    def batch_detail(month: str, batch_id: int):
        month = _valid_month(month)
        batch = service.get_batch(batch_id)
        if batch is None or batch["month"] != month:
            abort(404)
        page = service.get_batch_rows(
            batch_id,
            limit=_page_size(request),
            offset=_page_offset(request),
        )
        reports = [
            report
            for report in service.list_reports_for_month(month, limit=500)
            if report["candidate_version"] == batch["version"]
        ]
        return render_template(
            "batch.html",
            month=month,
            batch=batch,
            page=page,
            reports=reports,
            active=service.get_active_batch(month),
        )

    # -- 核对 -------------------------------------------------------------
    @app.post("/months/<month>/batches/<int:batch_id>/reconcile")
    def run_reconcile(month: str, batch_id: int):
        month = _valid_month(month)
        batch = service.get_batch(batch_id)
        if batch is None or batch["month"] != month:
            abort(404)
        thresholds = {
            key: request.form.get(key, "")
            for key in (
                "max_key_missing_ratio",
                "max_null_rate_diff",
                "max_bin_share_diff",
            )
        }
        try:
            report = service.reconcile(batch_id, thresholds)
        except ServiceError as error:
            _flash_error(error)
            return redirect(url_for("batch_detail", month=month, batch_id=batch_id))
        if report["status"] == "stale":
            flash(f"报告 {report['report_no']}：{report['note']}", "error")
        elif report["verdict"] == "passed":
            flash(
                f"报告 {report['report_no']}：核对通过，可确认生效。", "success"
            )
        else:
            flash(
                f"报告 {report['report_no']}：核对未通过，已退回。", "error"
            )
        return redirect(url_for("report_detail", report_no=report["report_no"]))

    # -- 报告 -------------------------------------------------------------
    @app.get("/reports/<report_no>")
    def report_detail(report_no: str):
        report = service.get_report(report_no)
        if report is None:
            abort(404)
        candidate = service.get_batch(report["candidate_batch_id"])
        baseline = service.get_batch(report["baseline_batch_id"])
        return render_template(
            "report.html",
            report=report,
            candidate=candidate,
            baseline=baseline,
            active=service.get_active_batch(report["month"]),
        )

    @app.post("/reports/<report_no>/confirm")
    def confirm(report_no: str):
        try:
            outcome = service.confirm_report(report_no)
        except ServiceError as error:
            _flash_error(error)
            return redirect(url_for("report_detail", report_no=report_no))
        if outcome["ok"]:
            flash(outcome["message"], "success")
        else:
            flash(outcome["message"], "error")
        return redirect(url_for("report_detail", report_no=report_no))

    @app.get("/reports")
    def reports_search():
        report_no = (request.args.get("report_no") or "").strip()
        report = service.get_report(report_no) if report_no else None
        return render_template(
            "reports.html", report_no=report_no, report=report,
            months=service.list_months(),
        )

    # -- 下游取数 ---------------------------------------------------------
    @app.get("/months/<month>/active.csv")
    def active_csv(month: str):
        month = _valid_month(month)
        try:
            active, cursor = service.active_rows_iter(month)
        except ServiceError as error:
            _flash_error(error)
            return redirect(url_for("month_detail", month=month))

        # 先一次性取完，避免响应流在请求数据库连接关闭后再迭代。
        rows = [
            (row["channel"], row["day"], row["amount"], row["conversions"])
            for row in cursor
        ]

        def generate():
            yield "渠道,日期,金额,转化数\n"
            for channel, day, amount, conversions in rows:
                amount_text = "" if amount is None else _csv_num(amount)
                conversions_text = (
                    "" if conversions is None else _csv_num(conversions)
                )
                yield (
                    f"{_csv_cell(channel)},{day},"
                    f"{amount_text},{conversions_text}\n"
                )

        response = Response(generate(), mimetype="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{month}-V{active["version"]}.csv"'
        )
        return response

    @app.get("/months/<month>/active")
    def active_view(month: str):
        month = _valid_month(month)
        try:
            page = service.active_rows_page(
                month,
                limit=_page_size(request),
                offset=_page_offset(request),
            )
        except ServiceError as error:
            _flash_error(error)
            return redirect(url_for("month_detail", month=month))
        return render_template("active.html", month=month, page=page)

    @app.get("/healthz")
    def healthz():
        database.get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", message="页面或资源不存在"), 404

    @app.errorhandler(413)
    def too_large(_error):
        return render_template("error.html", message="文件超过 32 MB 上限"), 413

    return app


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _valid_month(month: str) -> str:
    try:
        return service.validate_month(month)
    except ServiceError:
        abort(404)


def _read_upload(month: str | None = None):
    upload = request.files.get("csv")
    selected_month = month or (request.form.get("month") or "").strip()
    if upload is None or not upload.filename:
        raise ServiceError("请选择要上传的 CSV 文件")
    if not selected_month:
        raise ServiceError("请选择月份")
    raw = upload.read()
    if not raw:
        raise ServiceError("文件为空")
    parsed = parse_csv(raw)
    if month is not None and parsed.month != month:
        raise ServiceError(
            f"文件属于 {parsed.month}，与当前月份 {month} 不一致"
        )
    return selected_month, parsed, upload.filename


def _flash_error(error) -> None:
    if isinstance(error, UploadError):
        for reason in error.reasons:
            flash(reason, "error")
    else:
        flash(error.message, "error")


def _fmt_pct(value):
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def _fmt_diff(value):
    if value is None:
        return "—"
    return f"{value * 100:+.2f}%"


def _fmt_threshold(value):
    """上限展示：新报告存精确十进制文本，原样显示；旧报告为浮点，按百分比显示。"""
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    return f"{value * 100:.2f}%"


def _csv_num(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _page_size(request) -> int:
    return min(max(request.args.get("limit", 100, type=int) or 100, 1), 1000)


def _page_offset(request) -> int:
    return max(request.args.get("offset", 0, type=int) or 0, 0)


def _csv_cell(text: str) -> str:
    if any(char in text for char in ',"\n\r'):
        return '"' + text.replace('"', '""') + '"'
    return text
