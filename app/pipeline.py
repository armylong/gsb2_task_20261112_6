"""ffmpeg / ffprobe 封装: 媒体探测、峰值测量与真实转码。

所有处理都通过子进程执行本机安装的 ffmpeg, 不存在预制文件或模拟进度。
"""
import json
import logging
import os
import re
import subprocess

from . import config

log = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    pass


class FFmpegVersionError(FFmpegError):
    pass


# ---- 版本检查 ----
_VERSION_RE = re.compile(r"ffmpeg version (\d+)\.(\d+)")


def check_ffmpeg_version():
    """启动时调用。返回 (主版本, 次版本), 不满足要求或不可用则抛异常。"""
    try:
        out = subprocess.run(
            [config.FFMPEG_BIN, "-version"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise FFmpegVersionError(
            f"无法执行 ffmpeg ({config.FFMPEG_BIN}), 请先安装并加入 PATH") from exc

    match = _VERSION_RE.search(out.splitlines()[0])
    if not match:
        raise FFmpegVersionError("无法解析 ffmpeg 版本号")
    version = tuple(int(x) for x in match.groups())
    if version < config.FFMPEG_MIN_VERSION:
        raise FFmpegVersionError(
            f"ffmpeg 版本过低: 当前 {version[0]}.{version[1]}, "
            f"要求 >= {config.FFMPEG_MIN_VERSION[0]}.{config.FFMPEG_MIN_VERSION[1]}")
    # ffprobe 也要可用
    try:
        subprocess.run([config.FFPROBE_BIN, "-version"],
                       capture_output=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FFmpegVersionError(
            f"无法执行 ffprobe ({config.FFPROBE_BIN})") from exc
    return version


# ---- 媒体探测 ----
def probe(path):
    """用 ffprobe 读取时长/采样率/声道/编码/码率。失败抛 FFmpegError。"""
    cmd = [
        config.FFPROBE_BIN, "-v", "error",
        "-show_format", "-show_streams", "-of", "json", path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError("ffprobe 探测超时") from exc
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe 失败: {proc.stderr.strip()[:500]}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FFmpegError("ffprobe 输出无法解析") from exc

    audio = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise FFmpegError("文件中没有音频流")

    def _to_float(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    info = {
        "duration": _to_float(data.get("format", {}).get("duration")),
        "sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
        "channels": audio.get("channels"),
        "codec_name": audio.get("codec_name"),
        "bit_rate": None,
    }
    bit_rate = audio.get("bit_rate") or data.get("format", {}).get("bit_rate")
    if bit_rate:
        try:
            info["bit_rate"] = int(bit_rate)
        except (TypeError, ValueError):
            info["bit_rate"] = None
    return info


_PEAK_RE = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")
_RMS_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")


def measure_peak(path):
    """volumedetect 测量峰值 dBFS(全刻度为 0 dB)。同时返回均方响度作参考。"""
    cmd = [
        config.FFMPEG_BIN, "-hide_banner", "-nostats", "-i", path,
        "-af", "volumedetect", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise FFmpegError(f"峰值测量失败: {proc.stderr.strip()[:500]}")
    peak_match = _PEAK_RE.search(proc.stderr)
    rms_match = _RMS_RE.search(proc.stderr)
    return {
        "peak_db": float(peak_match.group(1)) if peak_match else None,
        "rms_db": float(rms_match.group(1)) if rms_match else None,
    }


def probe_full(path):
    """探测 + 峰值, 供导入时一次性使用。"""
    info = probe(path)
    try:
        info.update(measure_peak(path))
    except FFmpegError as exc:
        log.warning("峰值测量失败 %s: %s", path, exc)
        info["peak_db"] = None
        info["rms_db"] = None
    return info


# ---- 转码命令 ----
def build_filter_chain(options, duration):
    """构造音频滤镜链: 响度归一 -> 重采样 -> 淡入淡出。

    顺序说明: 先 loudnorm 做 EBU R128 响度归一并限制真峰值,
    再 aresample 重采样(让响度处理在原始采样率上完成), 最后 afade。
    """
    chain = []
    target_lufs = options.get("loudness_lufs")
    if target_lufs is not None:
        chain.append(
            f"loudnorm=I={float(target_lufs):.1f}:TP=-1.5:LRA=11")
    chain.append(f"aresample={int(options['sample_rate'])}")
    fade = float(options.get("fade_seconds") or 0)
    if fade > 0 and duration:
        # 音频短到放不下两段淡变时, ffmpeg 会报错, 这里提前拦截
        if fade * 2 >= duration:
            raise FFmpegError(
                f"淡入淡出时长 {fade:g}s 过长: 音频总时长 {duration:.2f}s, "
                f"两段淡变之和必须小于总时长")
        chain.append(f"afade=t=in:st=0:d={fade:g}")
        chain.append(f"afade=t=out:st={duration - fade:.3f}:d={fade:g}")
    return chain


def build_command(source_path, tmp_path, options, duration):
    fmt = config.OUTPUT_FORMATS[options["format"]]
    cmd = [
        config.FFMPEG_BIN, "-hide_banner", "-nostats",
        "-y",                       # 覆盖的是本任务独占的临时文件
        "-i", source_path,
    ]
    chain = build_filter_chain(options, duration)
    if chain:
        cmd += ["-af", ",".join(chain)]
    cmd += ["-ar", str(int(options["sample_rate"]))]
    # 不指定 -ac: 保留源声道布局(单声道不会被悄悄做成立体声)
    cmd += fmt["codec_args"]
    cmd += [tmp_path]
    return cmd


# ---- 进度解析 ----
_TIME_RE = re.compile(r"out_time_ms=(\d+)")
_PROGRESS_RE = re.compile(r"progress=(.+)")


def run_transcode(source_path, tmp_path, options, duration,
                  on_progress=None, should_cancel=None, on_start=None):
    """执行转码。

    on_progress(percent: float) 解析 ffmpeg `-progress pipe:1` 的真实输出;
    on_start(proc) 在进程启动后回调, 供外部登记进程并实现整组 kill;
    should_cancel() 返回 True 时终止整个进程组并抛出 CancellationRequested;
    正常结束返回 None, ffmpeg 非零退出抛 FFmpegError。
    """
    cmd = build_command(source_path, tmp_path, options, duration)
    cmd += ["-progress", "pipe:1", "-vn"]
    log.info("启动转码: %s", " ".join(cmd))

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
        # 独立进程组, 取消时整组杀掉, 避免留下编码器子进程
        start_new_session=True,
    )
    if on_start is not None:
        on_start(proc)

    last_percent = 0.0
    canceled = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if should_cancel is not None and should_cancel():
                _terminate_process_group(proc)
                canceled = True
                raise CancellationRequested()
            m = _TIME_RE.search(line)
            if m and duration and on_progress is not None:
                out_seconds = int(m.group(1)) / 1_000_000
                percent = min(99.0, max(0.0, out_seconds / duration * 100))
                if percent - last_percent >= 0.5 or percent >= 99:
                    last_percent = percent
                    on_progress(percent)
            if _PROGRESS_RE.search(line):
                if on_progress is not None and last_percent < 99:
                    on_progress(99.0)
    except CancellationRequested:
        pass
    except Exception as exc:  # 读管道异常等
        _terminate_process_group(proc)
        raise FFmpegError(f"转码进程异常: {exc}") from exc

    # stdout 读完后 ffmpeg 可能还在收尾(写文件尾、编码器 flush)。
    # 用轮询而不是阻塞 wait(), 使这段时间内的取消请求依然能立即生效。
    while True:
        try:
            returncode = proc.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if should_cancel is not None and should_cancel():
                _terminate_process_group(proc)
                canceled = True
                break

    if canceled:
        # 取消语义: 杀掉进程后清掉它可能已经写出的部分文件
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            log.warning("取消后临时文件删除失败: %s", tmp_path)
        raise CancellationRequested()

    stderr_tail = ""
    if proc.stderr is not None:
        stderr_tail = proc.stderr.read()[-2000:]

    if should_cancel is not None and should_cancel():
        raise CancellationRequested()
    if returncode != 0:
        raise FFmpegError(
            f"ffmpeg 退出码 {returncode}: {stderr_tail.strip()[:500]}")
    if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        raise FFmpegError("ffmpeg 未生成有效输出文件")
    if on_progress is not None:
        on_progress(100.0)


class CancellationRequested(Exception):
    """任务被用户取消。"""


def _terminate_process_group(proc, timeout=5):
    """先 SIGTERM 整个进程组, 超时再 SIGKILL。"""
    import signal
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait(timeout=5)
