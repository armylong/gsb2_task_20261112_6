"""路径与运行参数配置。所有运行期数据都放在 DATA_DIR 下, 不污染仓库。"""
import os
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 允许通过环境变量把数据目录指到别处(测试用)
DATA_DIR = os.environ.get("WORKBENCH_DATA_DIR", os.path.join(BASE_DIR, "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
OUTPUT_DIR = os.path.join(DATA_DIR, "outputs")
TMP_DIR = os.path.join(OUTPUT_DIR, ".tmp")
DB_PATH = os.path.join(DATA_DIR, "workbench.db")

# ffmpeg / ffprobe 可执行文件: 优先环境变量, 其次 PATH
FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE_BIN = os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe") or "ffprobe"

# 最低 ffmpeg 版本要求(loudnorm / volumedetect / afade 在 4.4 均已稳定)
FFMPEG_MIN_VERSION = (4, 4)

ALLOWED_EXTENSIONS = {
    ".wav", ".mp3", ".flac", ".ogg", ".oga", ".m4a", ".aac",
    ".wma", ".aiff", ".aif", ".opus",
}

MAX_UPLOAD_MB = 500
DEFAULT_CONCURRENCY = 2
MAX_CONCURRENCY = 8

# 输出格式 -> (容器扩展名, ffmpeg 编码参数)
OUTPUT_FORMATS = {
    "wav": {"ext": "wav", "codec_args": ["-c:a", "pcm_s16le"]},
    "mp3": {"ext": "mp3", "codec_args": ["-c:a", "libmp3lame", "-q:a", "2"]},
    "flac": {"ext": "flac", "codec_args": ["-c:a", "flac"]},
    "ogg": {"ext": "ogg", "codec_args": ["-c:a", "libopus", "-b:a", "128k"]},
    "m4a": {"ext": "m4a", "codec_args": ["-c:a", "aac", "-b:a", "192k"]},
}

SAMPLE_RATES = [8000, 16000, 22050, 44100, 48000, 96000]


def ensure_dirs():
    for d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR, TMP_DIR):
        os.makedirs(d, exist_ok=True)
