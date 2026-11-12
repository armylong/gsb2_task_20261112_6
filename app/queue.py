"""任务队列与工作线程。

- queued / running / completed / failed / canceled 五态, 全部落库
- 有界线程池, 多个任务真实并行, 每个任务独立临时文件与输出名, 互不覆盖
- 取消: queued 直接置 canceled; running 杀掉 ffmpeg 进程组并清理临时文件
- 进度来自 ffmpeg `-progress` 的真实输出
"""
import json
import logging
import os
import re
import shutil
import threading
import time

from . import config, pipeline
from .db import Database

log = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._一-鿿-]+")


def safe_filename(name):
    """保留可读原名, 去掉路径分隔符等危险字符; 兜底 untitled。"""
    name = os.path.basename(name).replace("\x00", "")
    name = _SAFE_NAME_RE.sub("_", name).strip("._") or "untitled"
    return name[:120]


class JobManager:
    def __init__(self, db: Database):
        self.db = db
        self._wake = threading.Event()
        self._stop = threading.Event()
        # job_id -> {"proc": Popen|None, "cancel": Event}
        # 派发瞬间即建句柄, 覆盖"线程已起、ffmpeg 尚未启动"的取消窗口
        self._workers = {}
        self._workers_lock = threading.Lock()

    # ---------- 生命周期 ----------
    def start(self):
        """启动调度线程; 同时清理上一轮遗留临时文件, 接管中断任务状态。"""
        self.db.mark_stale_active_jobs_failed()
        self._cleanup_tmp_dir()
        self._stop.clear()
        dispatcher = threading.Thread(
            target=self._dispatch_loop, name="job-dispatcher", daemon=True)
        dispatcher.start()

    def shutdown(self):
        self._stop.set()
        self._wake.set()
        with self._workers_lock:
            handles = list(self._workers.values())
        for h in handles:
            if h["proc"] is not None:
                pipeline._terminate_process_group(h["proc"])

    # ---------- 取消 ----------
    def cancel(self, job_id):
        """返回 (是否成功, 说明)。状态已终态的任务不可取消。"""
        job = self.db.get_job(job_id)
        if job is None:
            return False, "任务不存在"
        if job["status"] in ("completed", "failed", "canceled"):
            return False, f"任务已处于终态({job['status']}), 无法取消"

        if job["status"] == "queued":
            # 抢占: 只有 queued -> canceled 成功才算数, 防止与 worker 抢任务
            cur = self.db.execute(
                "UPDATE jobs SET status='canceled', finished_at=? "
                "WHERE id=? AND status='queued'",
                (time.time(), job_id))
            self._wake.set()
            if cur.rowcount:
                return True, "排队中的任务已取消"
            # 竞态: 已被调度器领走, 走 running 分支
            job = self.db.get_job(job_id)

        with self._workers_lock:
            handle = self._workers.get(job_id)
        if handle is not None:
            handle["cancel"].set()
            if handle["proc"] is not None:
                # 立即杀进程组; worker 的取消轮询也会兜底
                pipeline._terminate_process_group(handle["proc"])
                return True, "正在停止 ffmpeg 进程并清理临时文件"
            return True, "任务即将启动, 已置取消标志, ffmpeg 不会执行"
        # 理论上到不了: running 必有句柄。稳妥兜底
        self.db.update_job(job_id, status="canceled",
                           error="取消请求与任务状态切换同时发生",
                           finished_at=time.time())
        return True, "已取消"

    # ---------- 调度 ----------
    def _dispatch_loop(self):
        while not self._stop.is_set():
            try:
                self._spawn_capacity_workers()
            except Exception:
                log.exception("调度异常")
            # 事件触发或 1 秒兜底轮询
            self._wake.wait(timeout=1.0)
            self._wake.clear()

    def _spawn_capacity_workers(self):
        concurrency = self._concurrency()
        with self._workers_lock:
            slots = concurrency - len(self._workers)
        if slots <= 0:
            return
        queued = self.db.queued_jobs()[:slots]
        for job in queued:
            # 原子认领: 只有 queued -> running 成功的线程才执行
            cur = self.db.execute(
                "UPDATE jobs SET status='running', started_at=? "
                "WHERE id=? AND status='queued'",
                (time.time(), job["id"]))
            if not cur.rowcount:
                continue
            handle = {"cancel": threading.Event(), "proc": None}
            with self._workers_lock:
                # 先占槽再建线程, 杜绝超额派发
                self._workers[job["id"]] = handle
            worker = threading.Thread(
                target=self._run_job, args=(job["id"], handle),
                name=f"job-{job['id']}", daemon=True)
            worker.start()

    def _concurrency(self):
        try:
            value = int(self.db.get_setting(
                "concurrency", config.DEFAULT_CONCURRENCY))
        except (TypeError, ValueError):
            value = config.DEFAULT_CONCURRENCY
        return max(1, min(config.MAX_CONCURRENCY, value))

    def refresh(self):
        """新任务入队或并发数变化后唤醒调度器。"""
        self._wake.set()

    # ---------- 单个任务 ----------
    def _run_job(self, job_id, handle):
        job = self.db.get_job(job_id)
        source = self.db.get_source(job["source_id"])
        options = json.loads(job["options_json"])
        fmt = config.OUTPUT_FORMATS[options["format"]]

        # 输出名带任务 id, 从命名上保证并行/重复任务绝不互相覆盖
        base = os.path.splitext(safe_filename(source["filename"]))[0]
        output_name = f"{base}__job{job_id}.{fmt['ext']}"
        final_path = os.path.join(config.OUTPUT_DIR, output_name)
        tmp_path = os.path.join(config.TMP_DIR, f"job{job_id}_{output_name}")

        def should_cancel():
            return handle["cancel"].is_set() or self._stop.is_set()

        def on_progress(percent):
            self.db.update_job(job_id, progress=round(percent, 2))

        def on_start(proc):
            handle["proc"] = proc
            # 登记期间取消请求可能已到
            if should_cancel():
                pipeline._terminate_process_group(proc)

        try:
            if should_cancel():
                raise pipeline.CancellationRequested()

            pipeline.run_transcode(
                source["path"], tmp_path, options,
                source["duration"] or 0,
                on_progress=on_progress,
                should_cancel=should_cancel,
                on_start=on_start)

            # 落盘前最后一次取消检查
            if should_cancel():
                raise pipeline.CancellationRequested()
            shutil.move(tmp_path, final_path)
            self.db.update_job(
                job_id, status="completed", progress=100.0,
                output_path=final_path, output_name=output_name,
                output_size=os.path.getsize(final_path),
                finished_at=time.time())
            log.info("任务 %s 完成 -> %s", job_id, output_name)

        except pipeline.CancellationRequested:
            self._safe_unlink(tmp_path)
            self.db.update_job(
                job_id, status="canceled",
                error="用户取消, ffmpeg 进程已终止, 临时文件已清理",
                finished_at=time.time())
            log.info("任务 %s 已取消", job_id)

        except (pipeline.FFmpegError, OSError) as exc:
            self._safe_unlink(tmp_path)
            self.db.update_job(
                job_id, status="failed", error=str(exc)[:1000],
                finished_at=time.time())
            log.warning("任务 %s 失败: %s", job_id, exc)

        except Exception as exc:  # 兜底, 线程不能静默死掉
            self._safe_unlink(tmp_path)
            self.db.update_job(
                job_id, status="failed", error=f"内部错误: {exc}"[:1000],
                finished_at=time.time())
            log.exception("任务 %s 内部错误", job_id)

        finally:
            with self._workers_lock:
                self._workers.pop(job_id, None)
            self._safe_unlink(tmp_path)
            self._wake.set()

    # ---------- 工具 ----------
    @staticmethod
    def _safe_unlink(path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            log.warning("临时文件删除失败: %s", path)

    @staticmethod
    def _cleanup_tmp_dir():
        """重启后清理 .tmp 下所有遗留临时文件。"""
        try:
            if not os.path.isdir(config.TMP_DIR):
                return
            for name in os.listdir(config.TMP_DIR):
                p = os.path.join(config.TMP_DIR, name)
                try:
                    if os.path.isfile(p) or os.path.islink(p):
                        os.remove(p)
                    else:
                        shutil.rmtree(p, ignore_errors=True)
                except OSError:
                    log.warning("遗留临时文件清理失败: %s", p)
        except OSError:
            log.exception("临时目录清理失败")
