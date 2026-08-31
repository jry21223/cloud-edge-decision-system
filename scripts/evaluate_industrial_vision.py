from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from scripts.benchmark_system import percentile

from common.industrial_vision import AGREED_DEFECT_LABELS, IndustrialDefectLabel

EVALUATION_LABELS = tuple(
    ["normal", *(label.value for label in sorted(AGREED_DEFECT_LABELS, key=str))]
)
ALLOWED_PREDICTIONS = set(EVALUATION_LABELS) | {IndustrialDefectLabel.UNKNOWN_ANOMALY.value}
SERIOUS_DEFECTS = {
    IndustrialDefectLabel.CRACK.value,
    IndustrialDefectLabel.MISSING_OR_ASSEMBLY.value,
}
SAFE_DEFECT_ACTIONS = {"reject", "quarantine", "alert", "isolate", "shutdown"}


class EvaluationRecord(BaseModel):
    truth: str
    prediction: str
    action: str
    latency_ms: float = Field(ge=0.0)
    route: str
    uploaded_bytes: int = Field(ge=0)
    deadline_met: bool


def _safe_ratio(numerator: int | float, denominator: int | float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def evaluate_records(
    records: Iterable[dict[str, Any] | EvaluationRecord],
) -> dict[str, Any]:
    rows = [EvaluationRecord.model_validate(record) for record in records]
    if not rows:
        raise ValueError("evaluation requires at least one record")
    for row in rows:
        truth = row.truth
        prediction = row.prediction
        if truth not in EVALUATION_LABELS:
            raise ValueError(f"unsupported truth label: {truth}")
        if prediction not in ALLOWED_PREDICTIONS:
            raise ValueError(f"unsupported prediction label: {prediction}")

    per_class: dict[str, dict[str, int | float]] = {}
    for label in EVALUATION_LABELS:
        true_positive = sum(
            row.truth == label and row.prediction == label for row in rows
        )
        false_positive = sum(
            row.truth != label and row.prediction == label for row in rows
        )
        false_negative = sum(
            row.truth == label and row.prediction != label for row in rows
        )
        precision = _safe_ratio(true_positive, true_positive + false_positive)
        recall = _safe_ratio(true_positive, true_positive + false_negative)
        f1 = _safe_ratio(2 * precision * recall, precision + recall)
        per_class[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": true_positive + false_negative,
        }

    supported_f1 = [
        float(metrics["f1"])
        for metrics in per_class.values()
        if int(metrics["support"]) > 0
    ]
    serious_rows = [row for row in rows if row.truth in SERIOUS_DEFECTS]
    serious_misses = sum(row.action not in SAFE_DEFECT_ACTIONS for row in serious_rows)
    latencies = [row.latency_ms for row in rows]
    uploaded_bytes = [row.uploaded_bytes for row in rows]
    correct = sum(row.truth == row.prediction for row in rows)

    return {
        "sample_count": len(rows),
        "visual": {
            "accuracy": _safe_ratio(correct, len(rows)),
            "macro_f1": _safe_ratio(sum(supported_f1), len(supported_f1)),
            "serious_defect_miss_rate": _safe_ratio(serious_misses, len(serious_rows)),
            "per_class": per_class,
        },
        "system": {
            "mean_latency_ms": sum(latencies) / len(latencies),
            "p95_latency_ms": percentile(latencies, 95),
            "deadline_met_rate": _safe_ratio(
                sum(row.deadline_met for row in rows), len(rows)
            ),
            "cloud_route_rate": _safe_ratio(
                sum(row.route == "CLOUD" for row in rows), len(rows)
            ),
            "uploaded_bytes_total": sum(uploaded_bytes),
            "uploaded_bytes_mean": sum(uploaded_bytes) / len(uploaded_bytes),
        },
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON on line {line_number}") from error
        if not isinstance(value, dict):
            raise ValueError(f"line {line_number} must contain a JSON object")
        records.append(value)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate frozen industrial-vision labels and cloud-edge system metrics."
    )
    parser.add_argument("input", type=Path, help="JSONL prediction and ground-truth records")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = evaluate_records(_read_jsonl(args.input))
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output is None:
        print(rendered)
    else:
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
