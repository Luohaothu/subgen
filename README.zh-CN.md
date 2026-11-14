# subgen

[English](README.md) | [简体中文](README.zh-CN.md)

面向本地视频语料、可断点续跑的中日字幕流水线。

subgen 会扫描视频目录，抽取日语音轨，用 FunASR 转写，做强制对齐，翻译并复核成简体中文，最后在视频同目录发布 `<video-stem>.zh.srt`。翻译可以接任意 OpenAI 兼容 API。

每个阶段把每视频记录写入本地工作目录。重跑会复用已完成的记录，发布失败也不会在语料库里留下写了一半的字幕。

## 环境要求

- Linux 或 macOS。
- Python 3.12 或更高。
- 安装和开发需要 `uv`。

## 安装

```bash
uv sync
```

`uv sync` 会创建 `.venv`，安装项目、开发工具，并把所有包锁定到 `uv.lock`。媒体探测和音频抽取使用 PyAV，不需要系统安装 `ffmpeg` 或 `ffprobe`。

运行下面的命令前先激活环境，或者在命令前加 `uv run`。

在跑本地模型的机器上安装模型运行时：

```bash
uv sync --extra models
```

Apple Silicon 再加装 MLX 翻译服务：

```bash
uv sync --extra models --extra mlx
```

## 配置

在仓库根目录写 `subgen.toml`，或用 `SUBGEN_CONFIG` 指向配置文件，也可以用环境变量覆盖。

```toml
[paths]
source_root = "/Volumes/media/videos"
work_root = "/Users/me/.local/share/subgen"
model_root = "/Users/me/.local/share/subgen/models"
asr_model = "/Users/me/.local/share/subgen/models/Fun-ASR-Nano-2512"

[runtime]
device = "auto"
asr_engine = "auto"

[translation]
endpoint = "http://127.0.0.1:8123/v1"
model = "vendor/primary"
revision = "2026-11-01"
review_model = "vendor/reviewer"
review_revision = "2026-11-01"
api_key_env = "OPENAI_API_KEY"
```

| 变量 | 含义 |
| --- | --- |
| `SUBGEN_CONFIG` | 配置文件路径 |
| `SUBGEN_SOURCE_ROOT` | 只读视频语料 |
| `SUBGEN_WORK_ROOT` | 本地工作目录 |
| `SUBGEN_MODEL_ROOT` | 模型权重目录 |
| `SUBGEN_ASR_MODEL` | ASR 模型目录 |
| `SUBGEN_DEVICE` | `auto`、`cpu`、`cuda` 或 `mps` |
| `SUBGEN_ASR_ENGINE` | `auto`、`vllm` 或 `funasr` |
| `SUBGEN_TRANSLATION_ENDPOINT` | OpenAI 兼容服务地址 |
| `SUBGEN_TRANSLATION_MODEL` | 主翻译模型 ID |
| `SUBGEN_TRANSLATION_REVISION` | 主翻译模型版本 |
| `SUBGEN_TRANSLATION_REVIEW_MODEL` | 复核模型 ID |
| `SUBGEN_TRANSLATION_REVIEW_REVISION` | 复核模型版本 |
| `SUBGEN_API_KEY_ENV` | 存放 API key 的环境变量名 |

`model_root` 按下载脚本使用的目录名存放 VAD、ASR、对齐和翻译权重。运行时会在 `state/model_revisions.json` 存在时读取它，否则从每个权重的配置文件推导版本。接远程翻译服务时，翻译版本来自 provider 配置，本地翻译权重可以没有。

默认根路径是 `/home/leo/nas/Other/abc` 和
`/home/leo/.local/share/abc-subtitles-recovery-6`。`src/subtitle_pipeline/config.py`
里的 `PipelineConfig.production()` 仍然描述原主机。新主机用 `subgen.toml` 或 `SUBGEN_*` 变量。

## 运行流水线

先启动本地翻译服务，或者把 `[translation]` 指向已有的服务。

```bash
subgen serve --model-path /path/to/translation/model --model vendor/primary
```

`subgen serve` 在 CUDA 上选择 vLLM，在装了 `mlx-lm` 的 Apple Silicon 上选择 MLX 服务。没有本地 GPU 引擎时它会打印指引，不会启动 CPU 服务。CPU 主机请接 llama.cpp、Ollama 或远程服务。

然后运行完整流水线：

```bash
subgen --dry-run
subgen
```

`subgen` 依次执行 inventory、抽取、ASR、对齐、两遍翻译和发布。它支持 `--limit`、`--video-id`、`--skip-inventory` 和 `--skip-extract`。它不执行 Linux 上的审计和清理步骤。

## 命令

| 命令 | 用途 |
| --- | --- |
| `subgen` | 运行完整语料流水线 |
| `subgen serve` | 启动 vLLM 或 MLX 翻译服务，或打印指引 |

## 运行矩阵

| 主机 | ASR | 对齐 | 翻译 |
| --- | --- | --- | --- |
| Linux + CUDA | FunASR 走 vLLM | transformers，bf16，CUDA | vLLM 或任意 OpenAI 兼容 API |
| Apple Silicon | FunASR 走 CPU | transformers，float16，MPS | MLX 服务或任意 OpenAI 兼容 API |
| 纯 CPU | FunASR 走 CPU | transformers，float32，CPU | 外部 OpenAI 兼容 API |

`device = "auto"` 依次选择 CUDA、MPS、CPU。`asr_engine = "auto"` 在 CUDA 上选 vLLM，其余选 FunASR。显式值会覆盖自动选择。

## 工作目录布局

| 路径 | 内容 |
| --- | --- |
| `audio/<video_id>.flac` | 从源视频抽取的 16 kHz 单声道音频 |
| `asr/corpus/<video_id>/` | `vad.json`、`segments.json`、`asr.json`、`alignment.json` 和 trace 文件 |
| `translation/corpus/<video_id>/` | 草稿与复核后的翻译产物 |
| `state/` | inventory 快照、模型版本、各阶段汇总、进度日志 |
| `models/` | 本地权重 |
| `logs/`、`scratch/` | 阶段日志和单次运行的临时文件 |

## 开发

```bash
uv sync                              # 按 uv.lock 创建 .venv
uv run pytest                        # 测试
uv run ruff check .                  # lint
uv run ruff format .                 # 格式化
uv run ruff check . && uv run ruff format --check . && uv run pytest
uv run pre-commit install            # 可选，安装 git hook
```

改依赖后运行 `uv lock`，并把 `uv.lock` 一起提交。

## 文档

- `docs/architecture.md` 介绍阶段流程、语料假设、状态和运行矩阵。
- `docs/development.md` 介绍环境、测试、lint、版本和 CI。
- `docs/operations.md` 介绍部署、监控、恢复和清理。
- `docs/design/2026-11-13-portable-runtime-design.md` 记录平台、引擎、媒体后端和 provider 的取舍。

## 许可证

MIT，见 [LICENSE](LICENSE)。