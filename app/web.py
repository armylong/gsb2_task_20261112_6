"""HTTP 路由: 上传/导入、任务 CRUD、试听、下载、并发设置。"""
import json
import logging
import os
import uuid

from flask import (Flask, abort, jsonify, request, send_file,
                   send_from_directory)
from werkzeug.utils import secure_filename

from . import config, pipeline
from .db import Database
from .queue import JobManager, safe_filename

log = logging.getLogger(__name__)


def create_app(db: Database = None, manager: JobManager = None):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024

    db = db or Database()
    manager = manager or JobManager(db)
    app.extensions["db"] = db
    app.extensions["manager"] = manager

    # ---------------- 序列化 ----------------
    def source_to_dict(row):
        return {
            "id": row["id"],
            "filename": row["filename"],
            "size_bytes": row["size_bytes"],
            "duration": row["duration"],
            "sample_rate": row["sample_rate"],
            "channels": row["channels"],
            "peak_db": row["peak_db"],
            "codec_name": row["codec_name"],
            "bit_rate": row["bit_rate"],
            "created_at": row["created_at"],
        }

    def job_to_dict(row):
        try:
            options = json.loads(row["options_json"])
        except (TypeError, ValueError):
            options = {}
        return {
            "id": row["id"],
            "source_id": row["source_id"],
            "source_filename": row["source_filename"],
            "source_path": row["source_path"],
            "source_duration": row["source_duration"],
            "source_sample_rate": row["source_sample_rate"],
            "source_channels": row["source_channels"],
            "source_peak_db": row["source_peak_db"],
            "status": row["status"],
            "options": options,
            "progress": row["progress"],
            "error": row["error"],
            "output_name": row["output_name"],
            "output_size": row["output_size"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    # ---------------- 页面与静态资源 ----------------
    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/meta")
    def meta():
        return jsonify({
            "formats": list(config.OUTPUT_FORMATS.keys()),
            "sample_rates": config.SAMPLE_RATES,
            "max_upload_mb": config.MAX_UPLOAD_MB,
            "concurrency": manager._concurrency(),
            "max_concurrency": config.MAX_CONCURRENCY,
        })

    # ---------------- 源文件 ----------------
    @app.post("/api/sources")
    def upload_source():
        file = request.files.get("file")
        if file is None or not file.filename:
            abort(400, "缺少 file 字段")
        original_name = file.filename
        ext = os.path.splitext(original_name)[1].lower()
        if ext not in config.ALLOWED_EXTENSIONS:
            abort(415, f"不支持的音频格式 {ext or '(无扩展名)'}")

        # 存储名加 uuid 前缀, 同名上传互不覆盖; 展示名保留原名
        kept = safe_filename(secure_filename(original_name) or "audio")
        stored_name = f"{uuid.uuid4().hex[:12]}_{kept}"
        dest = os.path.join(config.UPLOAD_DIR, stored_name)
        try:
            file.save(dest)
        except OSError as exc:
            abort(500, f"文件保存失败: {exc}")

        return _register_source(dest, original_name, stored_name)

    @app.post("/api/sources/import")
    def import_source():
        """导入服务器本地路径(仍是本机 ffmpeg 工作流的一部分)。"""
        payload = request.get_json(silent=True) or {}
        path = payload.get("path")
        if not path:
            abort(400, "缺少 path 字段")
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            abort(404, "路径不存在或不是文件")
        ext = os.path.splitext(path)[1].lower()
        if ext not in config.ALLOWED_EXTENSIONS:
            abort(415, f"不支持的音频格式 {ext or '(无扩展名)'}")

        original_name = os.path.basename(path)
        stored_name = f"{uuid.uuid4().hex[:12]}_{safe_filename(original_name)}"
        dest = os.path.join(config.UPLOAD_DIR, stored_name)
        try:
            # 复制进工作区, 后续处理不依赖原始位置, 源文件本身永不修改
            with open(path, "rb") as src, open(dest, "wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        except OSError as exc:
            abort(500, f"文件复制失败: {exc}")
        return _register_source(dest, original_name, stored_name)

    def _register_source(dest, original_name, stored_name):
        try:
            info = pipeline.probe_full(dest)
        except pipeline.FFmpegError as exc:
            _quiet_remove(dest)
            abort(422, f"音频探测失败: {exc}")
        size = os.path.getsize(dest)
        source_id = db.insert_source(
            original_name, stored_name, dest, size, info)
        row = db.get_source(source_id)
        return jsonify(source_to_dict(row)), 201

    @app.get("/api/sources")
    def list_sources():
        return jsonify([source_to_dict(r) for r in db.list_sources()])

    @app.get("/api/sources/<int:source_id>/stream")
    def stream_source(source_id):
        row = db.get_source(source_id)
        if row is None:
            abort(404)
        # send_file 自带 Range 支持, 浏览器可拖动试听
        return send_file(row["path"], conditional=True,
                         download_name=row["filename"])

    # ---------------- 任务 ----------------
    @app.post("/api/jobs")
    def create_job():
        payload = request.get_json(silent=True) or {}
        source_id = payload.get("source_id")
        source = db.get_source(source_id) if source_id is not None else None
        if source is None:
            abort(400, "source_id 无效")

        options, err = _validate_options(payload)
        if err:
            abort(400, err)

        # 重复提交行为明确化: 同一源文件已有进行中任务 -> 409, 绝不静默吞掉
        active = db.active_jobs_for_source(source_id)
        if active:
            ids = ", ".join(str(r["id"]) for r in active)
            abort(409, f"该文件已有进行中的任务(#{ids}), 请等待结束或先取消; "
                       "历史任务不会阻止再次提交")

        # 淡入淡出提前校验, 避免任务入队后才失败
        fade = options["fade_seconds"]
        if fade > 0 and source["duration"] and fade * 2 >= source["duration"]:
            abort(400, f"淡入淡出时长 {fade:g}s 过长: 音频总时长 "
                       f"{source['duration']:.2f}s, 两段淡变之和必须更小")

        job_id = db.insert_job(source_id, options)
        manager.refresh()
        return jsonify(job_to_dict(db.get_job_detail(job_id))), 201

    @app.get("/api/jobs")
    def list_jobs():
        return jsonify([job_to_dict(r) for r in db.list_jobs()])

    @app.get("/api/jobs/<int:job_id>")
    def get_job(job_id):
        row = _get_job_row(job_id)
        return jsonify(job_to_dict(row))

    @app.post("/api/jobs/<int:job_id>/cancel")
    def cancel_job(job_id):
        _get_job_row(job_id)
        ok, message = manager.cancel(job_id)
        if not ok:
            abort(409, message)
        manager.refresh()
        return jsonify({"ok": True, "message": message,
                        "job": job_to_dict(_get_job_row(job_id))})

    @app.get("/api/jobs/<int:job_id>/stream")
    def stream_job_output(job_id):
        row = _get_job_row(job_id)
        if row["status"] != "completed" or not row["output_path"]:
            abort(409, "任务尚未完成, 没有可试听的输出")
        if not os.path.isfile(row["output_path"]):
            abort(410, "输出文件已被从磁盘移除")
        return send_file(row["output_path"], conditional=True,
                         download_name=row["output_name"])

    @app.get("/api/jobs/<int:job_id>/download")
    def download_job_output(job_id):
        row = _get_job_row(job_id)
        if row["status"] != "completed" or not row["output_path"]:
            abort(409, "任务尚未完成")
        if not os.path.isfile(row["output_path"]):
            abort(410, "输出文件已被从磁盘移除")
        # as_attachment 触发下载而非内联播放
        return send_file(row["output_path"], as_attachment=True,
                         download_name=row["output_name"])

    @app.get("/api/jobs/<int:job_id>/output-info")
    def output_info(job_id):
        """对成品再跑一次 ffprobe, 用于前后对比展示真实输出参数。"""
        row = _get_job_row(job_id)
        if row["status"] != "completed" or not row["output_path"]:
            abort(409, "任务尚未完成")
        try:
            info = pipeline.probe_full(row["output_path"])
        except pipeline.FFmpegError as exc:
            abort(422, str(exc))
        return jsonify(info)

    # ---------------- 设置 ----------------
    @app.post("/api/settings/concurrency")
    def set_concurrency():
        payload = request.get_json(silent=True) or {}
        try:
            value = int(payload.get("concurrency"))
        except (TypeError, ValueError):
            abort(400, "concurrency 必须是整数")
        if not 1 <= value <= config.MAX_CONCURRENCY:
            abort(400, f"并发数范围 1~{config.MAX_CONCURRENCY}")
        db.set_setting("concurrency", value)
        manager.refresh()
        return jsonify({"ok": True, "concurrency": value})

    # ---------------- 错误处理 ----------------
    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(410)
    @app.errorhandler(415)
    @app.errorhandler(413)
    @app.errorhandler(422)
    def handle_error(err):
        status = getattr(err, "code", 500)
        return jsonify({"error": getattr(err, "description", str(err))}), status

    @app.errorhandler(500)
    def handle_500(err):
        log.exception("服务端错误")
        return jsonify({"error": "服务端内部错误"}), 500

    # ---------------- 辅助 ----------------
    def _get_job_row(job_id):
        row = db.get_job_detail(job_id)
        if row is None:
            abort(404, "任务不存在")
        return row

    return app


def _validate_options(payload):
    options = {}
    fmt = payload.get("format")
    if fmt not in config.OUTPUT_FORMATS:
        return None, f"format 必须是 {list(config.OUTPUT_FORMATS)} 之一"
    options["format"] = fmt

    try:
        sr = int(payload.get("sample_rate"))
    except (TypeError, ValueError):
        return None, "sample_rate 必须是整数"
    if sr not in config.SAMPLE_RATES:
        return None, f"sample_rate 必须是 {config.SAMPLE_RATES} 之一"
    options["sample_rate"] = sr

    loudness = payload.get("loudness_lufs")
    if loudness in (None, "", "none"):
        options["loudness_lufs"] = None
    else:
        try:
            loudness = float(loudness)
        except (TypeError, ValueError):
            return None, "loudness_lufs 必须是数字或留空"
        if not -70 <= loudness <= -5:
            return None, "响度目标范围 -70 ~ -5 LUFS"
        options["loudness_lufs"] = loudness

    try:
        fade = float(payload.get("fade_seconds") or 0)
    except (TypeError, ValueError):
        return None, "fade_seconds 必须是非负数字"
    if fade < 0 or fade > 60:
        return None, "淡入淡出时长范围 0~60 秒"
    options["fade_seconds"] = fade
    return options, None


def _quiet_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
