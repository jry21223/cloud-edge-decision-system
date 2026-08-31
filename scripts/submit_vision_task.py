from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image, ImageDraw, UnidentifiedImageError

_MAX_IMAGE_BYTES = 25_000_000
_FORMAT_TO_MIME = {
    "BMP": "image/bmp",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
}


def synthetic_demo_png_bytes() -> bytes:
    """Create a synthetic pipeline demo image; it is not real industrial data."""

    image = Image.new("RGB", (320, 240), (128, 128, 128))
    drawing = ImageDraw.Draw(image)
    for y in range(0, image.height, 16):
        drawing.rectangle(
            (0, y, image.width - 1, min(y + 6, image.height - 1)),
            fill=(118, 118, 118),
        )
    drawing.rectangle((112, 74, 206, 156), fill=(248, 248, 248))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def inspect_image(raw: bytes, *, declared_name: str) -> tuple[int, int, str]:
    if not raw:
        raise ValueError(f"image is empty: {declared_name}")
    if len(raw) > _MAX_IMAGE_BYTES:
        raise ValueError(
            f"image exceeds the {_MAX_IMAGE_BYTES}-byte API limit: {declared_name}"
        )
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.verify()
        with Image.open(io.BytesIO(raw)) as opened:
            width, height = opened.size
            image_format = (opened.format or "").upper()
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise ValueError(f"cannot decode image: {declared_name}") from error
    try:
        mime_type = _FORMAT_TO_MIME[image_format]
    except KeyError as error:
        raise ValueError(
            f"unsupported image format {image_format or 'unknown'}; use JPEG, PNG, or BMP"
        ) from error
    return width, height, mime_type


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Submit one image to the Edge vision API. The built-in --synthetic source is "
            "a generated integration-demo fixture, not real industrial data or accuracy evidence."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", type=Path, help="user-supplied image; provenance is unverified")
    source.add_argument(
        "--synthetic",
        action="store_true",
        help="generate an explicitly synthetic demo image in memory",
    )
    parser.add_argument("--edge-url", default="http://localhost:8001")
    parser.add_argument("--task-id")
    parser.add_argument("--trace-id")
    parser.add_argument("--workpiece-id")
    parser.add_argument(
        "--workpiece-type",
        choices=("machined-metal-bracket",),
        default="machined-metal-bracket",
        help="frozen MVP metal workpiece profile",
    )
    parser.add_argument("--station-id", default="demo-station")
    parser.add_argument("--batch-id", default="demo-batch")
    parser.add_argument("--frame-id")
    parser.add_argument(
        "--risk-level",
        choices=("low", "medium", "high", "critical"),
        default="medium",
    )
    parser.add_argument("--deadline-ms", type=int, default=2_000)
    parser.add_argument(
        "--allow-raw-upload",
        action="store_true",
        help="allow RAW only when no localized ROI is available",
    )
    parser.add_argument(
        "--force-confidence",
        type=float,
        help=(
            "test-only confidence override; Edge ignores it unless "
            "ALLOW_TEST_CONTROLS=true"
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    return parser


def build_request(args: argparse.Namespace) -> tuple[dict[str, object], str]:
    if args.synthetic:
        raw = synthetic_demo_png_bytes()
        source_name = "generated-synthetic-demo.png"
        provenance_kind = "synthetic_demo"
        provenance_note = (
            "Programmatically generated integration fixture; not real industrial data, "
            "not ground truth, and not accuracy evidence."
        )
    else:
        assert args.image is not None
        raw = args.image.read_bytes()
        source_name = args.image.name
        provenance_kind = "user_supplied_unverified"
        provenance_note = (
            "User-supplied file; this client does not verify collection source, license, "
            "industrial representativeness, or ground truth."
        )

    width, height, mime_type = inspect_image(raw, declared_name=source_name)
    task_id = args.task_id or str(uuid4())
    trace_id = args.trace_id or str(uuid4())
    workpiece_id = args.workpiece_id or (
        f"synthetic-workpiece-{task_id}" if args.synthetic else f"unverified-workpiece-{task_id}"
    )
    frame_id = args.frame_id or (
        f"synthetic-frame-{task_id}" if args.synthetic else f"unverified-frame-{task_id}"
    )
    metadata: dict[str, object] = {
        "allow_raw_upload": args.allow_raw_upload,
        "data_provenance": {
            "kind": provenance_kind,
            "image_format": mime_type,
            "ground_truth_status": "unverified",
            "note": provenance_note,
        },
    }
    if args.force_confidence is not None:
        if not 0.0 <= args.force_confidence <= 1.0:
            raise ValueError("--force-confidence must be between 0 and 1")
        metadata["force_confidence"] = args.force_confidence

    request: dict[str, object] = {
        "task_id": task_id,
        "trace_id": trace_id,
        "scene": "industrial",
        "workpiece_id": workpiece_id,
        "station_id": args.station_id,
        "batch_id": args.batch_id,
        "captured_at": datetime.now(UTC).isoformat(),
        "payload": {},
        "context": {
            "data_provenance": provenance_kind,
            "material": "metal",
            "workpiece_type": args.workpiece_type,
        },
        "risk_level": args.risk_level,
        "deadline_ms": args.deadline_ms,
        "metadata": metadata,
        "image": {
            "frame_id": frame_id,
            "width": width,
            "height": height,
            "mime_type": mime_type,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_size": len(raw),
            "data_base64": base64.b64encode(raw).decode("ascii"),
        },
    }
    return request, provenance_kind


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        request, provenance_kind = build_request(args)
        endpoint = f"{args.edge_url.rstrip('/')}/v1/tasks"
        print(
            (
                "NOTICE: submitting an explicitly synthetic demo fixture; this is not real "
                "industrial data or performance evidence."
                if provenance_kind == "synthetic_demo"
                else "NOTICE: submitting a user-supplied file with unverified provenance and ground truth."
            ),
            file=sys.stderr,
        )
        with httpx.Client(timeout=args.timeout_seconds) as client:
            response = client.post(endpoint, json=request)
            response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, httpx.HTTPError, json.JSONDecodeError) as error:
        print(f"vision task submission failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
