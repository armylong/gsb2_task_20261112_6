"""开发/本地使用的启动入口: python run.py [--host 127.0.0.1] [--port 8000]

生产也可使用 flask 命令:
  FLASK_APP=run:app flask run --host 127.0.0.1 --port 8000
"""
import argparse
import logging
import os
import signal

from app import config
from app.db import Database
from app.pipeline import FFmpegVersionError, check_ffmpeg_version
from app.queue import JobManager
from app.web import create_app


def main():
    parser = argparse.ArgumentParser(description="本地音频批量处理工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    log = logging.getLogger("run")

    try:
        version = check_ffmpeg_version()
    except FFmpegVersionError as exc:
        raise SystemExit(f"[启动失败] {exc}")
    log.info("ffmpeg 版本检查通过: %s.%s (要求 >= %s.%s)",
             version[0], version[1],
             config.FFMPEG_MIN_VERSION[0], config.FFMPEG_MIN_VERSION[1])

    config.ensure_dirs()
    db = Database()
    manager = JobManager(db)
    app = create_app(db=db, manager=manager)
    manager.start()

    # 优雅退出: 停掉所有在跑的 ffmpeg
    def _shutdown(*_):
        log.info("正在关闭, 终止运行中的任务...")
        manager.shutdown()
        db.close()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("数据目录: %s", os.path.abspath(config.DATA_DIR))
    log.info("监听 http://%s:%s (仅本机访问)", args.host, args.port)
    app.run(host=args.host, port=args.port, threaded=True,
            use_reloader=False)


if __name__ == "__main__":
    main()
