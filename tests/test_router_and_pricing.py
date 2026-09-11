from __future__ import annotations

import json

import pytest

from socagents.core.config import KillSwitches, Settings
from socagents.core.errors import ConfigError
from socagents.model_gateway.openai_compat import OpenAICompatibleModel
from socagents.model_gateway.pricing import Price, cost_usd, load_prices
from socagents.model_gateway.router import (
    ModelSpec,
    create_model,
    parse_model_args,
    parse_model_spec,
)
from socagents.model_gateway.scripted import ScriptedModel
from socagents.model_gateway.types import Usage


def test_parse_model_spec() -> None:
    assert parse_model_spec("ollama/qwen3:8b") == ModelSpec("ollama", "qwen3:8b")
    assert str(parse_model_spec("anthropic/some-model")) == "anthropic/some-model"


@pytest.mark.parametrize("spec", ["anthropic", "/model", "anthropic/", "foo/bar"])
def test_parse_model_spec_rejects_bad_input(spec: str) -> None:
    with pytest.raises(ConfigError):
        parse_model_spec(spec)


def test_parse_model_args_supports_roles() -> None:
    selection = parse_model_args(["ollama/llama", "desk_lead=anthropic/big"])
    assert selection.default == ModelSpec("ollama", "llama")
    assert selection.for_role("desk_lead") == ModelSpec("anthropic", "big")
    assert selection.for_role("technical") == ModelSpec("ollama", "llama")


def test_parse_model_args_rejects_bad_role() -> None:
    with pytest.raises(ConfigError):
        parse_model_args(["Bad-Role=ollama/x"])


def test_create_fixture_model(settings: Settings) -> None:
    assert isinstance(create_model(ModelSpec("fixture", "scripted"), settings), ScriptedModel)
    with pytest.raises(ConfigError):
        create_model(ModelSpec("fixture", "other"), settings)


async def test_create_ollama_model_uses_settings(tmp_path) -> None:
    settings = Settings(
        home=tmp_path, kill_switches=KillSwitches(), ollama_base_url="http://gpu-box:11434/v1"
    )
    model = create_model(ModelSpec("ollama", "llama"), settings)
    assert isinstance(model, OpenAICompatibleModel)
    assert model.provider == "ollama"
    await model.aclose()


def test_cost_accounting(tmp_path) -> None:
    (tmp_path / "pricing.json").write_text(
        json.dumps({"anthropic/m": {"input_per_mtok": 3, "output_per_mtok": 15}})
    )
    prices = load_prices(tmp_path)
    assert prices == {"anthropic/m": Price(3.0, 15.0)}
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000)
    assert cost_usd(prices, "anthropic", "m", usage) == pytest.approx(4.5)
    assert cost_usd(prices, "anthropic", "unknown", usage) is None
    assert cost_usd(prices, "ollama", "llama", usage) == 0.0


def test_missing_pricing_file_means_no_prices(tmp_path) -> None:
    assert load_prices(tmp_path) == {}


def test_invalid_pricing_file(tmp_path) -> None:
    (tmp_path / "pricing.json").write_text('{"anthropic/m": {"input_per_mtok": 3}}')
    with pytest.raises(ConfigError):
        load_prices(tmp_path)
