"""生成可合法自由使用的样例音频(全部由 ffmpeg lavfi 合成, 无第三方版权)。

用法:
  .venv/bin/python scripts/generate_samples.py

产物:
  samples/sample_stereo_48k.wav   立体声, 48 kHz, 双正弦叠加 + 颤音, 6 秒
  samples/sample_mono_44k.wav     单声道, 44.1 kHz, 正弦 + 颤音, 5 秒
  samples/sample_stereo_mp3.mp3   立体声, 44.1 kHz, MP3 压缩样例, 5 秒
"""
import os
import subprocess
import sys

# 允许从仓库任意位置执行
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "samples")

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")

# 每个样例: (文件名, ffmpeg 输入参数列表, filter_complex 或 None, 输出参数列表)
SAMPLES = [
    (
        "sample_stereo_48k.wav",
        [
            "-f", "lavfi", "-i",
            "sine=frequency=440:duration=6:sample_rate=48000",
            "-f", "lavfi", "-i",
            "sine=frequency=660:duration=6:sample_rate=48000",
        ],
        # 左右声道不同频率, 各自带缓慢颤音后 join 成立体声
        "[0:a]volume=0.55,tremolo=f=3:d=0.3[a0];"
        "[1:a]volume=0.45,tremolo=f=3:d=0.3[a1];"
        "[a0][a1]join=inputs=2:channel_layout=stereo[a]",
        ["-map", "[a]", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le"],
    ),
    (
        "sample_mono_44k.wav",
        ["-f", "lavfi", "-i",
         "sine=frequency=200:duration=5:sample_rate=44100"],
        "[0:a]vibrato=f=8:d=0.5[a]",
        ["-map", "[a]", "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le"],
    ),
    (
        "sample_stereo_mp3.mp3",
        ["-f", "lavfi", "-i",
         "sine=frequency=330:duration=5:sample_rate=44100"],
        # 单声道转立体声 + 颤音, 再编码为 MP3, 体现"格式乱七八糟"的输入
        "[0:a]tremolo=f=5:d=0.5,pan=stereo|c0=c0|c1=c0[a]",
        ["-map", "[a]", "-ar", "44100", "-ac", "2",
         "-c:a", "libmp3lame", "-q:a", "4"],
    ),
]


def main():
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f"找不到可用的 ffmpeg: {exc}")

    os.makedirs(OUT_DIR, exist_ok=True)
    for name, inputs, filter_graph, out_args in SAMPLES:
        dest = os.path.join(OUT_DIR, name)
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
        cmd += inputs
        if filter_graph:
            cmd += ["-filter_complex", filter_graph]
        cmd += out_args + [dest]
        print("生成", name, "...")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            sys.exit(f"生成 {name} 失败:\n{proc.stderr}")
        print(f"  -> {dest} ({os.path.getsize(dest)} bytes)")
    print("全部样例生成完成。")


if __name__ == "__main__":
    main()
