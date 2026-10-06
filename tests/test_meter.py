import pytest

from verirun.config import ModelPrice
from verirun.llm.client import Usage
from verirun.llm.meter import CostMeter


def test_cost_meter_computes_cost_from_usage() -> None:
    meter = CostMeter({"m": ModelPrice(input=10.0, cached_input=1.0, output=50.0)})

    meter.add("m", Usage(prompt_tokens=1000, cached_tokens=400, completion_tokens=200))

    expected = (600 * 10.0 + 400 * 1.0 + 200 * 50.0) / 1_000_000
    assert meter.total_usd == pytest.approx(expected)
    assert meter.total_tokens == 1200


def test_cost_meter_accumulates_across_calls() -> None:
    meter = CostMeter({"m": ModelPrice(input=1.0, cached_input=0.0, output=1.0)})

    meter.add("m", Usage(prompt_tokens=1_000_000, completion_tokens=0))
    meter.add("m", Usage(prompt_tokens=0, completion_tokens=1_000_000))

    assert meter.total_usd == pytest.approx(2.0)


def test_cost_meter_flags_unknown_models() -> None:
    meter = CostMeter({})

    meter.add("mystery", Usage(prompt_tokens=10, completion_tokens=1))

    assert "mystery" in meter.unknown_models
