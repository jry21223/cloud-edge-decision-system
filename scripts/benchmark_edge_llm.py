from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sysconfig
import threading
import time
from pathlib import Path
from typing import Any

import psutil

DEFAULT_PROMPT = """你是工业设备边缘诊断助手。根据以下局部数据给出一个简短结论：
温度：84℃；振动：7.2 mm/s；电流：16.2 A；日志：轴承出现间歇异响。
仅输出风险等级（normal/warning/critical）和建议动作。"""


def gpu_memory_bytes() -> int | None:
    """Return aggregate NVIDIA used-memory bytes when nvidia-smi is available."""
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None

    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    values = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    try:
        return sum(int(value) for value in values) * 1024 * 1024
    except ValueError:
        return None


def mb(value: int | None) -> float | None:
    return None if value is None else round(value / 1024 / 1024, 2)


def configure_cuda_dll_search() -> list[object]:
    """Expose pip-installed CUDA DLLs to Windows before importing llama-cpp."""
    if os.name != "nt":
        return []

    site_packages = Path(sysconfig.get_paths()["purelib"])
    dll_directories: list[object] = []
    for relative_path in (
        Path("nvidia/cuda_runtime/bin"),
        Path("nvidia/cublas/bin"),
    ):
        directory = site_packages / relative_path
        if directory.is_dir():
            dll_directories.append(os.add_dll_directory(str(directory)))
    return dll_directories


def preload_llama_libraries() -> list[object]:
    """Load llama.cpp's Windows DLL dependency chain in a deterministic order."""
    if os.name != "nt":
        return []

    library_dir = Path(sysconfig.get_paths()["purelib"]) / "llama_cpp" / "lib"
    handles: list[object] = []
    for filename in (
        "ggml-base.dll",
        "ggml-cpu.dll",
        "ggml.dll",
        "ggml-cuda.dll",
        "llama.dll",
    ):
        path = library_dir / filename
        if path.is_file():
            handles.append(ctypes.CDLL(str(path)))
    return handles


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark an edge GGUF model with consistent memory and TTFT metrics."
    )
    parser.add_argument("--model", type=Path, required=True, help="Path to a GGUF model file.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument(
        "--warmup-tokens",
        type=int,
        default=0,
        help="Generate this many tokens before measurement to isolate warm inference.",
    )
    parser.add_argument("--context", type=int, default=512)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument(
        "--gpu-layers",
        type=int,
        default=-1,
        help="-1 offloads all supported layers; 0 performs a CPU-only baseline.",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON result path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.model.is_file():
        raise SystemExit(f"Model file does not exist: {args.model}")

    try:
        cuda_dll_directories = configure_cuda_dll_search()
        llama_dll_handles = preload_llama_libraries()
        from llama_cpp import Llama
    except (ImportError, OSError, RuntimeError) as error:
        raise SystemExit(
            "Unable to load llama-cpp-python. Ensure its CPU/CUDA runtime "
            f"dependencies are installed. Details: {error}"
        ) from error

    # Keep the directory handles alive for the duration of the benchmark.
    _ = cuda_dll_directories, llama_dll_handles

    process = psutil.Process()
    baseline_rss = process.memory_info().rss
    baseline_gpu = gpu_memory_bytes()
    peak_rss = baseline_rss
    peak_gpu = baseline_gpu
    stop_sampler = threading.Event()

    def sample_resources() -> None:
        nonlocal peak_rss, peak_gpu
        # Polling nvidia-smi is relatively expensive; keep it infrequent so
        # resource monitoring does not materially distort TTFT or throughput.
        last_gpu_sample = 0.0
        while not stop_sampler.wait(0.05):
            peak_rss = max(peak_rss, process.memory_info().rss)
            now = time.perf_counter()
            if now - last_gpu_sample >= 0.5:
                current_gpu = gpu_memory_bytes()
                if current_gpu is not None:
                    peak_gpu = max(peak_gpu or 0, current_gpu)
                last_gpu_sample = now

    sampler = threading.Thread(target=sample_resources, daemon=True)
    sampler.start()
    try:
        load_started = time.perf_counter()
        model = Llama(
            model_path=str(args.model),
            n_ctx=args.context,
            n_threads=args.threads,
            n_gpu_layers=args.gpu_layers,
            verbose=False,
        )
        model_load_ms = (time.perf_counter() - load_started) * 1000

        prompt_tokens = len(model.tokenize(args.prompt.encode("utf-8"), add_bos=True))
        warmup_generation_ms: float | None = None
        if args.warmup_tokens:
            warmup_started = time.perf_counter()
            model.create_completion(
                args.prompt,
                max_tokens=args.warmup_tokens,
                temperature=0,
                stream=False,
            )
            warmup_generation_ms = (time.perf_counter() - warmup_started) * 1000

        generated_text = ""
        first_token_ms: float | None = None
        generation_started = time.perf_counter()
        for chunk in model.create_completion(
            args.prompt,
            max_tokens=args.max_tokens,
            temperature=0,
            stream=True,
        ):
            text = chunk["choices"][0]["text"]
            if text and first_token_ms is None:
                first_token_ms = (time.perf_counter() - generation_started) * 1000
            generated_text += text
        generation_ms = (time.perf_counter() - generation_started) * 1000
        generated_tokens = len(model.tokenize(generated_text.encode("utf-8"), add_bos=False))
    finally:
        stop_sampler.set()
        sampler.join(timeout=1)

    result: dict[str, Any] = {
        "model_path": str(args.model),
        "model_file_size_mb": round(args.model.stat().st_size / 1024 / 1024, 2),
        "context_tokens": args.context,
        "max_output_tokens": args.max_tokens,
        "warmup_tokens": args.warmup_tokens,
        "warmup_generation_ms": (
            None if warmup_generation_ms is None else round(warmup_generation_ms, 2)
        ),
        "gpu_layers": args.gpu_layers,
        "threads": args.threads,
        "model_load_ms": round(model_load_ms, 2),
        "prompt_tokens": prompt_tokens,
        "generated_tokens": generated_tokens,
        "ttft_ms": None if first_token_ms is None else round(first_token_ms, 2),
        "generation_ms": round(generation_ms, 2),
        "generated_tokens_per_second": (
            None if generation_ms == 0 else round(generated_tokens / (generation_ms / 1000), 3)
        ),
        "process_rss_before_mb": mb(baseline_rss),
        "process_rss_peak_mb": mb(peak_rss),
        "process_rss_after_mb": mb(process.memory_info().rss),
        "nvidia_used_memory_before_mb": mb(baseline_gpu),
        "nvidia_used_memory_peak_mb": mb(peak_gpu),
        "nvidia_used_memory_after_mb": mb(gpu_memory_bytes()),
        "output": generated_text,
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
        print(f"Wrote benchmark result: {args.output}")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
