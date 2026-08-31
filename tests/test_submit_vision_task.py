from scripts.submit_vision_task import build_parser, build_request


def test_submission_client_marks_the_frozen_metal_workpiece_scope():
    args = build_parser().parse_args(["--synthetic"])

    request, provenance = build_request(args)

    assert provenance == "synthetic_demo"
    assert request["context"] == {
        "data_provenance": "synthetic_demo",
        "material": "metal",
        "workpiece_type": "machined-metal-bracket",
    }


def test_submission_client_accepts_an_explicit_supported_workpiece_type():
    args = build_parser().parse_args(
        ["--synthetic", "--workpiece-type", "machined-metal-bracket"]
    )

    request, _ = build_request(args)

    assert request["context"]["workpiece_type"] == "machined-metal-bracket"
