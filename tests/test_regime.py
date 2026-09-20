from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from socagents.analytics.flow import FlowEstimate
from socagents.analytics.gex import GexEstimate
from socagents.analytics.regime import (
    HORIZON_BARS,
    RegimeClassifier,
    build_features,
    label_outcome,
)
from socagents.core.config import Settings
from socagents.db.store import Store
from socagents.model_gateway.types import ToolCall
from socagents.providers.base import Bar
from socagents.tools.gateway import ToolGateway
from socagents.tools.registry import ToolRegistry
from socagents.tools.research import REGIME_TOOL, RegimeIn, get_regime_estimate
from socagents.tools.sdk import Tool, ToolContext

AS_OF = datetime(2026, 9, 10, 17, 45, tzinfo=UTC)


def bars(closes: list[float], *, start: datetime | None = None, interval_min: int = 5) -> list[Bar]:
    start = start if start is not None else AS_OF - timedelta(minutes=interval_min * len(closes))
    return [
        Bar(
            ts=start + timedelta(minutes=interval_min * i),
            open=c,
            high=c + 0.5,
            low=c - 0.5,
            close=c,
            volume=100,
        )
        for i, c in enumerate(closes)
    ]


def gex_estimate(
    *, regime: str = "positive", call_wall: float | None = 110.0, put_wall: float | None = 90.0
) -> GexEstimate:
    return GexEstimate(
        symbol="TEST",
        spot=100.0,
        expirations_used=[date(2026, 9, 18)],
        contracts_used=1,
        net_gex=1.0 if regime == "positive" else -1.0,
        regime=regime,  # type: ignore[arg-type]
        call_wall=call_wall,
        put_wall=put_wall,
        zero_gamma=100.0,
        top_strikes=[],
    )


def flow_estimate(*, ratio: float | None = 1.5) -> FlowEstimate:
    return FlowEstimate(
        symbol="TEST",
        expirations_used=[date(2026, 9, 18)],
        call_volume=100,
        put_volume=100,
        call_premium_usd=1_000.0,
        put_premium_usd=1_000.0,
        call_put_premium_ratio=ratio,
        bias="balanced",
        unusual=[],
    )


# label_outcome


def test_label_outcome_trending_on_strong_monotonic_drift() -> None:
    anchor = AS_OF
    window = bars([100 + i * 2 for i in range(HORIZON_BARS)], start=anchor + timedelta(minutes=5))
    all_bars = bars([100.0], start=anchor) + window
    assert label_outcome(all_bars, anchor) == "trending"


def test_label_outcome_mean_reverting_on_flat_oscillation() -> None:
    anchor = AS_OF
    closes = [100 + (1 if i % 2 == 0 else -1) * 0.1 for i in range(HORIZON_BARS)]
    window = bars(closes, start=anchor + timedelta(minutes=5))
    all_bars = bars([100.0], start=anchor) + window
    assert label_outcome(all_bars, anchor) == "mean_reverting"


def test_label_outcome_returns_none_when_outcome_not_yet_knowable() -> None:
    anchor = AS_OF
    window = bars([100 + i for i in range(HORIZON_BARS - 1)], start=anchor + timedelta(minutes=5))
    all_bars = bars([100.0], start=anchor) + window
    assert label_outcome(all_bars, anchor) is None


# build_features


def test_build_features_guards_missing_gex_and_flow_fields() -> None:
    gex = gex_estimate(call_wall=None, put_wall=None)
    flow = flow_estimate(ratio=None)
    features = build_features(gex, flow, bars([100.0] * 5))
    assert features["dist_to_call_wall_pct"] == 0.0
    assert features["dist_to_put_wall_pct"] == 0.0
    assert features["call_put_premium_ratio_log"] == 0.0


def test_build_features_returns_finite_numbers_for_a_normal_series() -> None:
    series = bars([100 + i * 0.1 for i in range(30)])
    features = build_features(gex_estimate(), flow_estimate(), series)
    assert all(isinstance(v, float) for v in features.values())


# RegimeClassifier


def test_cold_start_prediction() -> None:
    assert RegimeClassifier().predict({"a": 1.0}) == ("mean_reverting", 0.5)


def test_observe_then_maybe_learn_waits_for_enough_new_bars() -> None:
    model = RegimeClassifier()
    anchor = AS_OF
    model.observe("SPY", {"a": 1.0}, anchor, "mean_reverting")
    not_enough = bars([100.0], start=anchor) + bars(
        [100 + i for i in range(HORIZON_BARS - 1)], start=anchor + timedelta(minutes=5)
    )
    model.maybe_learn("SPY", not_enough)
    assert model.is_fit is False
    assert model.pending_symbols == ["SPY"]
    assert model.accuracy is None


def test_observe_then_maybe_learn_learns_once_outcome_is_knowable() -> None:
    model = RegimeClassifier()
    anchor = AS_OF
    model.observe("SPY", {"a": 1.0}, anchor, "mean_reverting")
    enough = bars([100.0], start=anchor) + bars(
        [100 + i * 2 for i in range(HORIZON_BARS)], start=anchor + timedelta(minutes=5)
    )
    model.maybe_learn("SPY", enough)
    assert model.is_fit is True
    assert model.pending_symbols == []


def test_maybe_learn_records_whether_the_prior_prediction_was_correct() -> None:
    model = RegimeClassifier()
    anchor = AS_OF
    enough = bars([100.0], start=anchor) + bars(
        [100 + i * 2 for i in range(HORIZON_BARS)], start=anchor + timedelta(minutes=5)
    )  # this window resolves to "trending" (see label_outcome tests above)

    model.observe("SPY", {"a": 1.0}, anchor, "trending")  # correct guess
    model.maybe_learn("SPY", enough)
    assert model.accuracy == 1.0
    assert model.recent_outcomes[-1].correct is True

    model.observe("QQQ", {"a": 1.0}, anchor, "mean_reverting")  # wrong guess
    model.maybe_learn("QQQ", enough)
    assert model.n_learned == 2
    assert model.accuracy == 0.5
    assert [o.correct for o in model.recent_outcomes] == [True, False]


def test_save_load_roundtrip_preserves_learned_state(tmp_path: Path) -> None:
    model = RegimeClassifier()
    anchor = AS_OF
    features = {"net_gex_sign": 1.0, "return_z": 2.0}
    model.observe("SPY", features, anchor, "mean_reverting")
    enough = bars([100.0], start=anchor) + bars(
        [100 + i * 2 for i in range(HORIZON_BARS)], start=anchor + timedelta(minutes=5)
    )
    model.maybe_learn("SPY", enough)
    assert model.is_fit is True

    path = tmp_path / "regime_model.pkl"
    model.save(path)
    reloaded = RegimeClassifier.load(path)
    assert reloaded.is_fit is True
    assert reloaded.predict(features) == model.predict(features)
    assert reloaded.accuracy == model.accuracy


def test_load_missing_file_returns_fresh_classifier(tmp_path: Path) -> None:
    model = RegimeClassifier.load(tmp_path / "missing.pkl")
    assert model.is_fit is False


def test_load_corrupt_file_returns_fresh_classifier(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.pkl"
    path.write_bytes(b"not a pickle")
    model = RegimeClassifier.load(path)
    assert model.is_fit is False


# tool handler


async def test_get_regime_estimate_cold_start(ctx: ToolContext) -> None:
    result = await get_regime_estimate(RegimeIn(symbol="SPY"), ctx)
    assert result.regime == "mean_reverting"
    assert result.confidence == 0.5
    assert result.gamma_regime in ("positive", "negative")
    model_path = Settings.from_env().home / "regime_model.pkl"
    assert model_path.is_file()


async def test_get_regime_estimate_is_stable_against_static_fixture_bars(ctx: ToolContext) -> None:
    first = await get_regime_estimate(RegimeIn(symbol="SPY"), ctx)
    second = await get_regime_estimate(RegimeIn(symbol="SPY"), ctx)
    assert first.regime == second.regime == "mean_reverting"
    assert first.confidence == second.confidence == 0.5


def _gateway(store: Store, settings: Settings, *tools: Tool[Any, Any]) -> ToolGateway:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolGateway(registry, store, settings)


async def test_regime_tool_snapshot_via_gateway(
    store: Store, settings: Settings, ctx: ToolContext
) -> None:
    gw = _gateway(store, settings, REGIME_TOOL)
    result = await gw.call(
        ctx, ToolCall(id="r1", name="get_regime_estimate", arguments={"symbol": "SPY"})
    )
    assert result.ok
    assert result.snapshot is not None
    assert result.content["data"]["regime"] in ("trending", "mean_reverting")
