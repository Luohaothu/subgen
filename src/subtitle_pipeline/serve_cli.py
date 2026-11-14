"""Start a local OpenAI-compatible translation server for the current platform.

CUDA hosts use vLLM. Apple Silicon hosts with ``mlx-lm`` use the MLX server.
Every other host points the pipeline at an external endpoint instead.
"""

from __future__ import annotations

import argparse
import importlib.util
import platform
import subprocess
import sys
from collections.abc import Sequence

DEFAULT_PORT = 8123
DEFAULT_MODEL = "Qwen/Qwen3.5-35B-A3B-FP8"


def select_engine(
    *,
    cuda: bool,
    system: str,
    machine: str,
    mlx: bool,
) -> str:
    if cuda:
        return "vllm"
    if system == "darwin" and machine in {"arm64", "aarch64"} and mlx:
        return "mlx"
    return "external"


def _cuda_available() -> bool:
    try:
        import torch
    except ImportError:
        return False
    try:
        return bool(torch.cuda.is_available())
    except (AttributeError, RuntimeError):
        return False


def build_server_command(
    engine: str,
    *,
    python: str,
    model_path: str,
    model: str,
    host: str,
    port: int,
    gpu_memory_utilization: float,
    max_model_len: int,
) -> list[str]:
    if engine == "vllm":
        return [
            python,
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--model",
            model_path,
            "--served-model-name",
            model,
            "--host",
            host,
            "--port",
            str(port),
            "--gpu-memory-utilization",
            str(gpu_memory_utilization),
            "--max-model-len",
            str(max_model_len),
        ]
    if engine == "mlx":
        return [
            python,
            "-m",
            "mlx_lm.server",
            "--model",
            model_path,
            "--host",
            host,
            "--port",
            str(port),
        ]
    raise ValueError(f"no local server for engine: {engine}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subgen serve",
        description="Start a local translation server or print the API guidance",
    )
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.92)
    parser.add_argument("--max-model-len", type=int, default=147456)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--print-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine = select_engine(
        cuda=_cuda_available(),
        system=platform.system().lower(),
        machine=platform.machine().lower(),
        mlx=importlib.util.find_spec("mlx_lm") is not None,
    )
    if engine == "external":
        print(
            "No local GPU engine was detected. Start an OpenAI-compatible "
            "server (for example llama.cpp, Ollama, or a remote provider) and "
            "set [translation] in subgen.toml.",
            file=sys.stderr,
            flush=True,
        )
        return 2
    command = build_server_command(
        engine,
        python=args.python,
        model_path=args.model_path,
        model=args.model,
        host=args.host,
        port=args.port,
        gpu_memory_utilization=args.gpu_memory_utilization,
        max_model_len=args.max_model_len,
    )
    print(f"engine={engine}", flush=True)
    print(" ".join(command), flush=True)
    if args.print_only:
        return 0
    try:
        subprocess.run(command, check=True)
    except KeyboardInterrupt:
        return 130
    except subprocess.CalledProcessError as exc:
        return exc.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
