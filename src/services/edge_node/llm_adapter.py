from __future__ import annotations

import ctypes
import os
import re
import sysconfig
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from common.schemas import TaskRequest


@dataclass(frozen=True)
class LlmDecision:
    prediction: str
    action: str
    confidence: float
    reason: str
    model_name: str


SCENE_OPTIONS = {
    "industrial": {
        "predictions": {"normal", "warning", "critical"},
        "actions": {"continue", "inspect", "shutdown"},
    },
    "traffic": {
        "predictions": {"normal", "congested", "incident"},
        "actions": {"keep_open", "divert", "close_lane"},
    },
}


ACTION_BY_PREDICTION = {
    "industrial": {"normal": "continue", "warning": "inspect", "critical": "shutdown"},
    "traffic": {"normal": "keep_open", "congested": "divert", "incident": "close_lane"},
}

_ADAPTER_INITIALIZATION_LOCK = threading.Lock()

def _prepare_windows_runtime() -> list[object]:
    """Load optional CUDA and llama.cpp DLLs in the order Windows requires."""
    if os.name != "nt":
        return []

    site_packages = Path(sysconfig.get_paths()["purelib"])
    handles: list[object] = []
    for relative_path in (
        Path("nvidia/cuda_runtime/bin"),
        Path("nvidia/cublas/bin"),
        Path("llama_cpp/lib"),
    ):
        directory = site_packages / relative_path
        if directory.is_dir():
            handles.append(os.add_dll_directory(str(directory)))

    library_dir = site_packages / "llama_cpp" / "lib"
    for filename in (
        "ggml-base.dll",
        "ggml-cpu.dll",
        "ggml.dll",
        "ggml-cuda.dll",
        "llama.dll",
    ):
        library_path = library_dir / filename
        if library_path.is_file():
            handles.append(ctypes.CDLL(str(library_path)))
    return handles


def _parse_label(text: str, scene: str, model_name: str) -> LlmDecision | None:
    options = SCENE_OPTIONS[scene]
    labels = "|".join(sorted(options["predictions"]))
    matches = re.findall(rf"\b({labels})\b", text.lower())
    if not matches:
        return None
    prediction = matches[-1]
    return LlmDecision(
        prediction=prediction,
        action=ACTION_BY_PREDICTION[scene][prediction],
        confidence=0.75,
        reason=f"llm_label={prediction}",
        model_name=model_name,
    )


class LlamaEdgeAdapter:
    """Optional local GGUF adapter; safety guards remain outside this adapter."""

    def __init__(self, model_path: Path, *, context: int, gpu_layers: int) -> None:
        self.model_path = model_path
        self.context = context
        self.gpu_layers = gpu_layers
        self._model: Any | None = None
        self._runtime_handles: list[object] = []
        self._inference_lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return f"llama-cpp:{self.model_path.stem}"

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        if not self.model_path.is_file():
            raise FileNotFoundError(f"EDGE_LLM_MODEL_PATH does not exist: {self.model_path}")

        self._runtime_handles = _prepare_windows_runtime()
        from llama_cpp import Llama

        self._model = Llama(
            model_path=str(self.model_path),
            n_ctx=self.context,
            n_gpu_layers=self.gpu_layers,
            verbose=False,
        )
        return self._model

    def _prompt(self, task: TaskRequest) -> str:
        if task.scene == "industrial":
            return (
                "你是工业设备边缘诊断助手。根据以下局部数据给出一个简短结论：\n"
                f"温度：{task.payload.get('temperature', 'unknown')}℃；"
                f"振动：{task.payload.get('vibration', 'unknown')} mm/s；"
                f"电流：{task.payload.get('current', 'unknown')} A；"
                f"日志：{task.payload.get('log', 'none')}。\n"
                "仅输出风险等级（normal/warning/critical）和建议动作。"
            )
        return (
            "你是交通边缘事件诊断助手。根据以下局部数据给出一个简短结论：\n"
            f"车流密度：{task.payload.get('vehicle_density', 'unknown')}；"
            f"平均速度：{task.payload.get('average_speed', 'unknown')} km/h；"
            f"排队长度：{task.payload.get('queue_length', 'unknown')}；"
            f"事故报告：{task.payload.get('accident_reported', False)}。\n"
            "仅输出状态（normal/congested/incident）和建议动作。"
        )

    def infer(self, task: TaskRequest) -> LlmDecision | None:
        # llama.cpp model instances are not assumed to be thread-safe. The
        # FastAPI layer offloads calls to worker threads, while this lock keeps
        # one shared model instance deterministic.
        with self._inference_lock:
            chunks = self._get_model().create_completion(
                self._prompt(task),
                max_tokens=32,
                temperature=0,
                stream=True,
            )
            text = "".join(str(chunk["choices"][0]["text"]) for chunk in chunks)
        return _parse_label(text, task.scene, self.model_name)

    def warmup(self) -> None:
        with self._inference_lock:
            model = self._get_model()
            model.create_completion("Return {}", max_tokens=8, temperature=0, stream=False)


@lru_cache(maxsize=1)
def _adapter_from_environment() -> LlamaEdgeAdapter:
    model_path = Path(os.environ["EDGE_LLM_MODEL_PATH"])
    context = int(os.getenv("EDGE_LLM_CONTEXT", "512"))
    gpu_layers = int(os.getenv("EDGE_LLM_GPU_LAYERS", "-1"))
    return LlamaEdgeAdapter(model_path, context=context, gpu_layers=gpu_layers)


def _shared_adapter() -> LlamaEdgeAdapter:
    """Serialize the first cached adapter construction across worker threads."""

    with _ADAPTER_INITIALIZATION_LOCK:
        return _adapter_from_environment()


def maybe_llm_inference(task: TaskRequest) -> LlmDecision | None:
    """Return an LLM decision only when explicitly enabled; otherwise stay on rules."""
    if os.getenv("EDGE_INFERENCE_BACKEND", "rule").lower() != "llm":
        return None
    try:
        return _shared_adapter().infer(task)
    except (ImportError, OSError, RuntimeError, ValueError, FileNotFoundError):
        return None


def warm_llm_if_enabled() -> bool:
    if os.getenv("EDGE_INFERENCE_BACKEND", "rule").lower() != "llm":
        return False
    _shared_adapter().warmup()
    return True
