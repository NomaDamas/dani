from dani.signatures import build_signature, parse_signature


def test_signature_round_trip() -> None:
    signature = build_signature(stage="review_round", job="job123", pr=7, round=2)
    assert parse_signature(signature) == {
        "stage": "review_round",
        "job": "job123",
        "pr": "7",
        "round": "2",
    }
