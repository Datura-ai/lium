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


def test_tesla_v100_is_typed_v100_not_gpu():
    from lium.sdk.utils import extract_gpu_type, gpu_short_matches

    v100 = "NVIDIA Tesla V100 Tensor Core GPU"
    assert extract_gpu_type(v100) == "V100"
    assert _client()._dict_to_executor_info(_record(machine_name=v100, specs={})).gpu_type == "V100"
    assert gpu_short_matches("v100", extract_gpu_type(v100))
    # the names that already resolved keep their type
    for name, want in [("NVIDIA H100 80GB HBM3", "H100"), ("NVIDIA RTX PRO 6000 Blackwell Server Edition", "RTXPRO6000"),
                       ("NVIDIA GeForce RTX 4090", "RTX4090"), ("NVIDIA A100-SXM4-80GB", "A100"), ("Tesla T4", "T4")]:
        assert extract_gpu_type(name) == want
