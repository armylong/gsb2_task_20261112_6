# 本地音频批量处理工作台

一个完全运行在本机的音频转码/响度归一工作台：导入录音后查看时长、采样率、声道、峰值等信息，
选择输出格式、采样率、响度目标和淡入淡出时长，由本机 **ffmpeg 子进程真实执行**转码，
完成后可在页面对比试听处理前后的声音并下载成品。无 Docker、无外部服务、无云依赖。

## 功能与需求对照

| 需求 | 实现方式 |
| --- | --- |
| 真实 ffmpeg 执行，不许假进度 | 每个任务 `subprocess.Popen` 启动本机 ffmpeg，进度解析 `-progress pipe:1` 的 `out_time_ms`，按真实输出时间换算百分比 |
| 排队/运行/完成/取消状态，刷新后仍在 | 五态（queued/running/completed/failed/canceled）全部写入 SQLite，前端轮询的所有数据来自服务端 |
| 取消要停进程、清临时文件 | ffmpeg 以独立进程组启动，取消时 `killpg` 整组终止（SIGTERM→SIGKILL），临时输出立即删除；重启时清理 `.tmp` 遗留 |
| 处理前后可试听，源文件信息保留 | 源文件复制进工作区且永不修改；源文件与成品均支持 HTTP Range 拖拽试听，对比弹窗同时展示两边 ffprobe 信息 |
| 多任务并行不互相覆盖 | 有界线程池（并发数可调，1~8），输出名强制带任务 id：`原名__job<id>.<ext>`，临时文件同样按任务 id 命名 |
| 重复提交行为明确 | 同一源文件存在 queued/running 任务时返回 **409** 并提示任务号；终态任务不阻止再次提交，绝不静默吞掉 |

另外：服务重启时，上一轮未完成的 queued/running 任务会被标记为 failed 并注明原因，
不会出现"永远卡在运行中"的任务。

## 环境要求

- **Python 3.10+**（开发与验证使用 3.12）
- **ffmpeg ≥ 4.4 且 ffprobe 可用**（用到 `loudnorm`、`volumedetect`、`afade`、`aresample`，
  这些滤镜在 4.4 已稳定；本机验证版本为 ffmpeg 9.0.2）
  - macOS：`brew install ffmpeg`
  - Ubuntu/Debian：`sudo apt install ffmpeg`（22.04 自带 4.4.x）
  - 也可用静态构建：https://ffmpeg.org/download.html
- 启动时会自动检查版本，不满足会直接报错退出；可用环境变量 `FFMPEG_BIN` / `FFPROBE_BIN`
  指定可执行文件路径

## 启动方法（不用 Docker）

```bash
# 1. 创建虚拟环境并安装依赖（只有 Flask 一个第三方依赖）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2.（可选）生成仓库自带样例音频：立体声 WAV、单声道 WAV、立体声 MP3
.venv/bin/python scripts/generate_samples.py

# 3. 启动，默认监听 127.0.0.1:8000（仅本机可访问）
.venv/bin/python run.py
# 或自定义端口
.venv/bin/python run.py --port 8011
```

浏览器打开 `http://127.0.0.1:8000` 即可。

## 使用流程

1. **导入**：拖拽/点选上传，或填写服务器本机路径导入（文件会复制进工作区，原文件不动）。
   导入后自动用 ffprobe + volumedetect 显示时长、采样率、声道数、峰值 dBFS、编码与码率。
2. **建任务**：选择输出格式（wav/mp3/flac/ogg/m4a）、采样率（8k~96k）、响度目标
   （EBU R128 LUFS，留空则不调整）、淡入/淡出秒数。
3. **观察队列**：状态徽章 + 真实进度条；可随时取消排队中或运行中的任务。
4. **对比与下载**：完成后点"前后对比"，两个播放器分别试听源文件与成品，并列出成品的真实
   探测参数；点"下载"获取成品文件。

### 处理滤镜说明

ffmpeg 滤镜链顺序为 `loudnorm → aresample → afade(in) → afade(out)`：

- 响度归一使用 `loudnorm=I=<目标>:TP=-1.5:LRA=11`（单遍动态归一，真峰值上限 -1.5 dBTP）
- 重采样使用 `aresample`，不指定 `-ac`，**源声道布局保持不变**（单声道不会被做成立体声）
- 淡入淡出时长之和必须小于音频总时长，提交前与 ffmpeg 执行前各有一道校验

## 数据存放位置

所有运行期数据都在仓库下的 `data/`（已加入 .gitignore）：

```
data/
├── workbench.db      # SQLite: 源文件信息、任务状态/参数/进度
├── uploads/          # 导入的源文件副本（只读，永不修改）
└── outputs/
    ├── .tmp/         # ffmpeg 临时输出，任务结束/重启即清理
    └── *.wav|mp3|... # 成品，文件名带任务 id
```

可用环境变量 `WORKBENCH_DATA_DIR` 把数据目录指到其他位置。

## 样例音频

`samples/` 下三个文件全部由 `scripts/generate_samples.py` 用 ffmpeg lavfi 合成，
不含任何第三方版权内容，可合法自由使用：

| 文件 | 声道 | 采样率 | 编码 | 时长 |
| --- | --- | --- | --- | --- |
| `sample_stereo_48k.wav` | 立体声 | 48 kHz | PCM 16-bit | 6 s |
| `sample_mono_44k.wav` | 单声道 | 44.1 kHz | PCM 16-bit | 5 s |
| `sample_stereo_mp3.mp3` | 立体声 | 44.1 kHz | MP3 | 5 s |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/sources` | multipart 上传音频 |
| POST | `/api/sources/import` | 导入本机路径 `{"path": "..."}` |
| GET | `/api/sources` | 源文件列表与探测信息 |
| GET | `/api/sources/<id>/stream` | 源文件试听（支持 Range） |
| POST | `/api/jobs` | 创建任务（同文件有进行中任务时 409） |
| GET | `/api/jobs` | 任务列表（状态/进度/错误） |
| POST | `/api/jobs/<id>/cancel` | 取消排队或运行中的任务 |
| GET | `/api/jobs/<id>/stream` | 成品试听（支持 Range） |
| GET | `/api/jobs/<id>/download` | 成品下载（attachment） |
| GET | `/api/jobs/<id>/output-info` | 成品 ffprobe 信息（前后对比用） |
| POST | `/api/settings/concurrency` | 调整并行任务数 |

## 测试

端到端集成测试会真实调用本机 ffmpeg（转码、取消、持久化、409 重复提交等 14 个用例）：

```bash
.venv/bin/python -I -m unittest discover -s tests -t .
```

## 目录结构

```
app/
├── config.py      # 路径、格式/采样率白名单、ffmpeg 版本要求
├── db.py          # SQLite 持久化层
├── pipeline.py    # ffprobe 探测、volumedetect 峰值、ffmpeg 转码与进程组取消
├── queue.py       # 调度线程、并发槽、取消、临时文件清理、唯一输出命名
├── web.py         # Flask 路由
└── static/        # 单页前端（原生 HTML/CSS/JS，无构建步骤）
run.py             # 入口（含 ffmpeg 版本检查与优雅退出）
scripts/generate_samples.py
tests/test_workbench.py
```
