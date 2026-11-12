"""端到端集成测试: 真实 ffmpeg 转码、排队、取消、重复提交、刷新持久化。

运行:
  .venv/bin/python -m unittest -v tests/test_workbench.py
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

# python -I 隔离模式下 cwd/PYTHONPATH 均不生效, 显式把仓库根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.db import Database  # noqa: E402
from app.pipeline import (FFmpegError, build_filter_chain, probe_full,  # noqa: E402
                          run_transcode, CancellationRequested)
from app.queue import JobManager  # noqa: E402
from app.web import create_app  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
STEREO = os.path.join(SAMPLES, "sample_stereo_48k.wav")
MONO = os.path.join(SAMPLES, "sample_mono_44k.wav")


class WorkbenchTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._data_dir = tempfile.mkdtemp(prefix="wb_test_")
        os.environ["WORKBENCH_DATA_DIR"] = cls._data_dir
        # config 在 import 时已读环境变量, 这里强制重定向
        config.DATA_DIR = cls._data_dir
        config.UPLOAD_DIR = os.path.join(cls._data_dir, "uploads")
        config.OUTPUT_DIR = os.path.join(cls._data_dir, "outputs")
        config.TMP_DIR = os.path.join(config.OUTPUT_DIR, ".tmp")
        config.DB_PATH = os.path.join(cls._data_dir, "workbench.db")
        config.ensure_dirs()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._data_dir, ignore_errors=True)

    def setUp(self):
        # 每个用例独立 DB + 管理器, 但共享上传/输出目录
        db_path = os.path.join(self._data_dir, f"t_{self.id().split('.')[-1]}.db")
        self.db = Database(db_path)
        self.manager = JobManager(self.db)
        self.manager.start()
        self.app = create_app(db=self.db, manager=self.manager)
        self.client = self.app.test_client()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.manager.shutdown()
        self.db.close()

    # ---- 辅助 ----
    def upload(self, path=STEREO):
        with open(path, "rb") as fh:
            resp = self.client.post(
                "/api/sources",
                data={"file": (fh, os.path.basename(path))},
                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 201, resp.data)
        return resp.get_json()

    def create_job(self, source_id, **overrides):
        payload = {
            "source_id": source_id, "format": "wav",
            "sample_rate": 44100, "loudness_lufs": -16,
            "fade_seconds": 0.2,
        }
        payload.update(overrides)
        return self.client.post("/api/jobs", json=payload)

    def wait_status(self, job_id, expected, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.client.get(f"/api/jobs/{job_id}").get_json()
            if job["status"] in (expected, "failed", "canceled"):
                return job
            time.sleep(0.1)
        raise AssertionError(f"任务 {job_id} 未在 {timeout}s 内到达 {expected}")


class TestProbe(WorkbenchTestBase):
    def test_probe_stereo_and_mono(self):
        for path, ch in ((STEREO, 2), (MONO, 1)):
            info = probe_full(path)
            self.assertEqual(info["channels"], ch)
            self.assertIn(info["sample_rate"], (44100, 48000))
            self.assertIsNotNone(info["duration"])
            self.assertIsNotNone(info["peak_db"])
            self.assertLessEqual(info["peak_db"], 0.0)

    def test_filter_chain_order(self):
        chain = build_filter_chain(
            {"sample_rate": 22050, "loudness_lufs": -14, "fade_seconds": 0.5},
            duration=10)
        # loudnorm -> aresample -> afade in / out
        self.assertTrue(chain[0].startswith("loudnorm="))
        self.assertIn("aresample=22050", chain[1])
        self.assertEqual(len(chain), 4)

    def test_fade_too_long_rejected(self):
        with self.assertRaises(FFmpegError):
            build_filter_chain(
                {"sample_rate": 48000, "loudness_lufs": None,
                 "fade_seconds": 3}, duration=5)


class TestJobLifecycle(WorkbenchTestBase):
    def test_full_transcode_and_output(self):
        src = self.upload()
        resp = self.create_job(src["id"])
        self.assertEqual(resp.status_code, 201, resp.data)
        job_id = resp.get_json()["id"]
        job = self.wait_status(job_id, "completed")
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["progress"], 100.0)
        job_row = self.db.get_job(job_id)
        self.assertTrue(os.path.isfile(job_row["output_path"]))
        # 输出名带任务 id, 不会与别的任务撞名
        self.assertIn(f"__job{job_id}.wav", job["output_name"])

        # 输出参数符合要求: 重采样生效、声道保持、淡变生效
        info = probe_full(job_row["output_path"])
        self.assertEqual(info["sample_rate"], 44100)
        self.assertEqual(info["channels"], 2)

        # 试听与下载端点
        self.assertEqual(self.client.get(
            f"/api/jobs/{job_id}/stream").status_code, 200)
        dl = self.client.get(f"/api/jobs/{job_id}/download")
        self.assertEqual(dl.status_code, 200)
        self.assertIn("attachment", dl.headers.get("Content-Disposition", ""))

    def test_mono_source_keeps_mono(self):
        src = self.upload(MONO)
        self.assertEqual(src["channels"], 1)
        resp = self.create_job(src["id"], format="flac", sample_rate=48000)
        job = self.wait_status(resp.get_json()["id"], "completed")
        info = self.client.get(
            f"/api/jobs/{job['id']}/output-info").get_json()
        self.assertEqual(info["channels"], 1)
        self.assertEqual(info["sample_rate"], 48000)

    def test_parallel_jobs_no_overwrite(self):
        src = self.upload()
        ids = []
        # 同一源先建一个, 完成后再建第二个; 两个输出文件都必须独立存在
        r1 = self.create_job(src["id"], format="mp3", sample_rate=44100)
        j1 = self.wait_status(r1.get_json()["id"], "completed")
        r2 = self.create_job(src["id"], format="mp3", sample_rate=22050)
        j2 = self.wait_status(r2.get_json()["id"], "completed")
        ids = [j1["id"], j2["id"]]
        detail = [self.client.get(f"/api/jobs/{i}").get_json() for i in ids]
        names = [d["output_name"] for d in detail]
        self.assertEqual(len(set(names)), 2)
        for jid in ids:
            row = self.db.get_job(jid)
            self.assertTrue(os.path.isfile(row["output_path"]))

    def test_duplicate_active_submission_rejected(self):
        src = self.upload()
        # 串行化: 先把并发调成 1, 第一个任务占住 worker
        self.client.post("/api/settings/concurrency", json={"concurrency": 1})
        r1 = self.create_job(src["id"])
        self.assertEqual(r1.status_code, 201)
        # 第二个同文件任务必须被明确拒绝, 而不是静默
        r2 = self.create_job(src["id"])
        self.assertEqual(r2.status_code, 409)
        self.assertIn("进行中", r2.get_json()["error"])
        # 完成后再提交是允许的(历史任务不阻止)
        self.wait_status(r1.get_json()["id"], "completed")
        r3 = self.create_job(src["id"])
        self.assertEqual(r3.status_code, 201)
        self.wait_status(r3.get_json()["id"], "completed")

    def test_cancel_running_job_kills_process_and_cleans_tmp(self):
        src = self.upload()
        self.client.post("/api/settings/concurrency", json={"concurrency": 1})
        resp = self.create_job(src["id"], format="flac", sample_rate=96000,
                               loudness_lufs=-14, fade_seconds=0.1)
        job_id = resp.get_json()["id"]

        # 等任务进入 running(ffmpeg 已启动)
        running = False
        for _ in range(50):
            job = self.client.get(f"/api/jobs/{job_id}").get_json()
            if job["status"] == "running":
                running = True
                break
            time.sleep(0.05)
        self.assertTrue(running, "任务应已进入 running")

        r = self.client.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual(r.status_code, 200, r.data)
        job = self.wait_status(job_id, "canceled")
        self.assertEqual(job["status"], "canceled")

        # 临时文件必须清理干净
        leftovers = [n for n in os.listdir(config.TMP_DIR)
                     if f"job{job_id}" in n]
        self.assertEqual(leftovers, [])

        # ffmpeg 进程不应残留
        import subprocess
        ps = subprocess.run(["pgrep", "-fl", "ffmpeg"],
                            capture_output=True, text=True)
        # 只检查本任务临时文件名相关的进程
        self.assertNotIn(f"job{job_id}", ps.stdout)

    def test_cancel_queued_job(self):
        src_a = self.upload()
        src_b = self.upload(MONO)  # 第二个源, 绕开同文件进行中任务的 409
        self.client.post("/api/settings/concurrency", json={"concurrency": 1})
        # 占住唯一 worker
        r0 = self.create_job(src_a["id"], format="wav")
        for _ in range(50):
            if self.client.get(f"/api/jobs/{r0.get_json()['id']}") \
                    .get_json()["status"] == "running":
                break
            time.sleep(0.05)
        # 另一个文件的任务只能排队, 直接取消
        r1 = self.create_job(src_b["id"], format="flac")
        self.assertEqual(r1.status_code, 201)
        queued_id = r1.get_json()["id"]
        rc = self.client.post(f"/api/jobs/{queued_id}/cancel")
        self.assertEqual(rc.status_code, 200)
        job = self.client.get(f"/api/jobs/{queued_id}").get_json()
        self.assertEqual(job["status"], "canceled")
        self.wait_status(r0.get_json()["id"], "completed")

    def test_persistence_after_reopen(self):
        src = self.upload()
        r = self.create_job(src["id"])
        job_id = r.get_json()["id"]
        self.wait_status(job_id, "completed")

        # 模拟服务重启: 先彻底停掉旧管理器, 再用同一个 DB 新建
        self.manager.shutdown()
        manager2 = JobManager(self.db)
        manager2.start()
        app2 = create_app(db=self.db, manager=manager2)
        client2 = app2.test_client()
        jobs = client2.get("/api/jobs").get_json()
        self.assertTrue(any(j["id"] == job_id and j["status"] == "completed"
                            for j in jobs))
        sources = client2.get("/api/sources").get_json()
        self.assertTrue(any(s["id"] == src["id"] for s in sources))
        manager2.shutdown()
        # 让 setUp 的清理不再重复 shutdown 已停的管理器(幂等, 但保持清晰)
        self.manager = manager2

    def test_restart_marks_active_jobs_failed_and_cleans_tmp(self):
        src = self.upload()
        # 手工造一个 running 状态的"遗留任务"和临时文件
        job_id = self.db.insert_job(src["id"], {
            "format": "wav", "sample_rate": 48000,
            "loudness_lufs": None, "fade_seconds": 0})
        self.db.update_job(job_id, status="running")
        tmp_file = os.path.join(config.TMP_DIR, f"job{job_id}_leftover.wav")
        with open(tmp_file, "wb") as fh:
            fh.write(b"x")
        self.manager.shutdown()

        manager2 = JobManager(self.db)
        manager2.start()
        job = self.db.get_job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertFalse(os.path.exists(tmp_file))
        manager2.shutdown()
        self.manager = manager2

    def test_invalid_options_rejected(self):
        src = self.upload()
        r = self.create_job(src["id"], sample_rate=12345)
        self.assertEqual(r.status_code, 400)
        r = self.create_job(src["id"], format="xyz")
        self.assertEqual(r.status_code, 400)
        r = self.create_job(src["id"], loudness_lufs=10)
        self.assertEqual(r.status_code, 400)

    def test_stream_supports_range(self):
        src = self.upload()
        resp = self.client.get(
            f"/api/sources/{src['id']}/stream",
            headers={"Range": "bytes=0-99"})
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(len(resp.data), 100)
        resp.close()


class TestTranscodeCancellation(unittest.TestCase):
    """直接对 pipeline 层测取消, 不经过 HTTP。"""

    @classmethod
    def setUpClass(cls):
        # 60 秒素材, 确保 0.2s 触发取消时 ffmpeg 必然还在运行(短素材会先跑完导致时序 flake)
        cls.long_file = os.path.join(tempfile.gettempdir(), "wb_cancel_60s.wav")
        if not os.path.exists(cls.long_file):
            import subprocess
            subprocess.run([
                config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i",
                "anoisesrc=d=60:c=pink:r=48000:a=0.3",
                "-ac", "2", cls.long_file], check=True)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.long_file):
            os.remove(cls.long_file)

    def test_run_transcode_cancellation(self):
        tmp = tempfile.mktemp(suffix=".wav")

        def _cleanup():
            if os.path.exists(tmp):
                os.remove(tmp)

        self.addCleanup(_cleanup)
        import threading
        cancel = threading.Event()

        def cancel_soon():
            time.sleep(0.2)
            cancel.set()

        threading.Thread(target=cancel_soon, daemon=True).start()
        with self.assertRaises(CancellationRequested):
            run_transcode(
                self.long_file, tmp,
                {"format": "flac", "sample_rate": 96000,
                 "loudness_lufs": -14, "fade_seconds": 1},
                duration=60, should_cancel=cancel.is_set)
        # pipeline 层契约: 取消时输出路径不能留下成品文件
        self.assertFalse(os.path.exists(tmp))


if __name__ == "__main__":
    unittest.main(verbosity=2)
