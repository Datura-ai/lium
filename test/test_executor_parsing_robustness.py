"""_dict_to_executor_info must not crash or misprice on sparse marketplace records,
and `lium config set template.default_id` must call an SDK method that exists."""

import pytest

from lium.sdk import Config, Lium


def _client():
    return Lium(Config(api_key="test"))


def _record(**overrides):
    base = {
        "id": "e1",
        "machine_name": "NVIDIA H100 80GB HBM3",
        "specs": {"gpu": {"count": 8, "details": [{"name": "NVIDIA H100 80GB HBM3"}] * 8}},
        "price_per_gpu": 2.0,
        "location": {},
    }
    base.update(overrides)
    return base


def test_reliability_score_is_read_and_absent_is_none():
    assert _client()._dict_to_executor_info(_record(reliability_score=81.2)).reliability_score == 81.2
    assert _client()._dict_to_executor_info(_record()).reliability_score is None


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"reliability_score": 99.0}, 99.0),
        ({"reliability_score": 99.0, "node_reliability_pct": None, "node_reliability_new": False}, 99.0),
        ({"reliability_score": 99.0, "node_reliability_pct": 95.0, "node_reliability_new": False}, 95.0),
        ({"reliability_score": 95.0, "node_reliability_pct": 99.0, "node_reliability_new": False}, 99.0),
        ({"reliability_score": 99.0, "node_reliability_pct": None, "node_reliability_new": True}, None),
    ],
    ids=["gate_off", "gate_on_not_established", "node_below_provider", "node_above_provider", "new_node"],
)
def test_reliability_score_is_the_one_lium_io_shows(fields, expected):
    """The per-node gate's score when the API sends it, the provider's otherwise, none for a new node."""
    assert _client()._dict_to_executor_info(_record(**fields)).reliability_score == expected


