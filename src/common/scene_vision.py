from __future__ import annotations

import time

from common.schemas import InferenceResult, TaskRequest
from common.vision import ClassicalVisionAdapter, VisionModelAdapter


class TrafficVisionMigrationAdapter:
    """Architecture probe for traffic images; deliberately not a traffic detector."""

    name = "traffic-vision-migration-probe"
    version = "v1"
    preprocess_version = "shared-image-contract-v1"

    def infer(self, task: TaskRequest, *, node_id: str) -> InferenceResult:
        started = time.perf_counter()
        if task.scene != "traffic":
            raise ValueError("traffic migration adapter only accepts scene=traffic")
        baseline = ClassicalVisionAdapter().infer(task, node_id=node_id)
        if baseline.image_quality is not None and not baseline.image_quality.passed:
            return baseline.model_copy(
                update={
                    "model_name": self.name,
                    "model_version": self.version,
                    "preprocess_version": self.preprocess_version,
                    "detections": [],
                }
            )
        return InferenceResult(
            prediction="traffic_visual_review_required",
            confidence=0.50,
            action="observe",
            reason="Traffic image contract validated for architecture migration only",
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            model_name=self.name,
            node_id=node_id,
            model_version=self.version,
            preprocess_version=self.preprocess_version,
            detections=[],
            image_quality=baseline.image_quality,
            scene_complexity=baseline.scene_complexity,
        )


class SceneVisionAdapter:
    """Select a scene adapter without branching inside model implementations."""

    name = "scene-vision-router"
    version = "v1"
    preprocess_version = "scene-router-v1"

    def __init__(self, *, industrial: VisionModelAdapter, traffic: VisionModelAdapter) -> None:
        self.industrial = industrial
        self.traffic = traffic

    def infer(self, task: TaskRequest, *, node_id: str) -> InferenceResult:
        adapter = self.traffic if task.scene == "traffic" else self.industrial
        return adapter.infer(task, node_id=node_id)
