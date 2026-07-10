from services.cloud_node.core import infer_industrial, infer_traffic


def test_cloud_fuses_multiple_near_critical_signals():
    prediction, action, _ = infer_industrial(
        {"temperature": 86, "vibration": 7.2, "current": 16.5}
    )
    assert prediction == "critical"
    assert action == "shutdown"


def test_traffic_incident():
    prediction, action, _ = infer_traffic(
        {"vehicle_density": 0.9, "average_speed": 4, "queue_length": 60}
    )
    assert prediction == "incident"
    assert action == "close_lane"
