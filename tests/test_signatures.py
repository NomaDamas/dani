from dani.signatures import (
    build_signature,
    has_ignore_signature,
    is_opt_out_comment,
    parse_agent_signature,
    parse_signature,
)


def test_signature_round_trip() -> None:
    signature = build_signature(stage="review_round", job="job123", pr=7, round=2)
    assert parse_signature(signature) == {
        "stage": "review_round",
        "job": "job123",
        "pr": "7",
        "round": "2",
    }


def test_has_ignore_signature_detects_ignore_stage_across_multiple_markers() -> None:
    text = f"{build_signature(stage='review_round', job='job123', pr=7, round=2)}\n<!-- dani:stage=ignore -->"

    assert has_ignore_signature(text) is True


def test_is_opt_out_comment_accepts_dani_ignore_command() -> None:
    assert is_opt_out_comment("Heads up\n/dani ignore") is True


def test_parse_agent_signature_excludes_ignore_stage() -> None:
    assert parse_agent_signature("<!-- dani:stage=ignore -->") is None
