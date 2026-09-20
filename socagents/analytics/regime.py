"""Online trending-vs-mean-reverting regime classifier.

Research-track MVP (see README's "Research Track: Continual Learning"): predicts whether a
symbol currently looks like it is trending or mean-reverting, learning incrementally from
outcomes that only become knowable in hindsight. Unlike the rest of ``analytics/``, this module
holds mutable learner state (a ``RegimeClassifier`` instance persisted to disk) rather than
being purely functional — that is a deliberate, single exception to the pure-function pattern
used by ``gex.py``/``flow.py``, not a precedent to copy elsewhere without reason.

The label heuristic below (``label_outcome``) is a placeholder threshold, not a validated
definition of "trending": it is what lets the classifier bootstrap itself from experience, not
a claim that it identifies real trading regimes.
"""

from __future__ import annotations

import math
import pickle
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from river import compose, linear_model, preprocessing

from socagents.analytics.flow import FlowEstimate
from socagents.analytics.gex import GexEstimate
from socagents.providers.base import Bar

Regime = Literal["trending", "mean_reverting"]
METHOD = "online-classifier-v1"

LOOKBACK_BARS = 20  # how far back to look when building features
HORIZON_BARS = 12  # how far forward to look before an outcome/label is knowable (1h on 5m bars)
REGIME_MODEL_FILENAME = "regime_model.pkl"


@dataclass
class PendingExample:
    features: dict[str, float]
    anchor_ts: datetime  # last bar's timestamp at the moment observe() was called


class RegimeClassifier:
    """A tiny online (river) classifier with a predict-now / learn-later loop.

    A regime label isn't observable at prediction time, only in hindsight, so each symbol keeps
    at most one pending example: predict now, and once enough new bars have arrived past that
    prediction's anchor timestamp, score the outcome and learn from it before predicting again.
    """

    def __init__(self) -> None:
        self._pipeline = compose.Pipeline(
            preprocessing.StandardScaler(), linear_model.LogisticRegression()
        )
        self._n_learned = 0
        self._pending: dict[str, PendingExample] = {}

    @property
    def is_fit(self) -> bool:
        return self._n_learned > 0

    def predict(self, features: dict[str, float]) -> tuple[Regime, float]:
        if not self.is_fit:
            return "mean_reverting", 0.5  # cold start: never touches the pipeline
        proba = self._pipeline.predict_proba_one(features)
        p_trend = proba.get(True, 0.5)
        regime: Regime = "trending" if p_trend >= 0.5 else "mean_reverting"
        return regime, max(p_trend, 1 - p_trend)

    def observe(self, symbol: str, features: dict[str, float], anchor_ts: datetime) -> None:
        """Register this call's features as the (sole) pending example for `symbol`.

        Overwrites any prior unresolved pending example for the same symbol. This is a
        deliberate simplification (one slot per symbol, not a queue) — acceptable for a
        research-track MVP; see the module-level limitation note in the implementation plan.
        """
        self._pending[symbol] = PendingExample(features=features, anchor_ts=anchor_ts)

    def maybe_learn(self, symbol: str, bars: list[Bar], horizon_bars: int = HORIZON_BARS) -> None:
        """Resolve `symbol`'s pending example if enough new bars have arrived, and learn from it."""
        pending = self._pending.get(symbol)
        if pending is None:
            return
        label = label_outcome(bars, pending.anchor_ts, horizon_bars)
        if label is None:
            return  # outcome not knowable yet — keep waiting
        # river's LogisticRegression is binary-only: learn/predict on bool, not the label string.
        self._pipeline.learn_one(pending.features, label == "trending")
        self._n_learned += 1
        del self._pending[symbol]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pickle.dumps(self))

    @classmethod
    def load(cls, path: Path) -> RegimeClassifier:
        if not path.is_file():
            return cls()
        try:
            obj = pickle.loads(path.read_bytes())
        except Exception:
            return cls()  # corrupt or foreign pickle -> fresh model, never raise
        return obj if isinstance(obj, cls) else cls()


def label_outcome(
    bars: list[Bar], anchor_ts: datetime, horizon_bars: int = HORIZON_BARS
) -> Regime | None:
    """Heuristic placeholder, not a validated definition of "trending".

    Looks at the `horizon_bars` bars strictly after `anchor_ts`: if the cumulative forward move
    exceeds one volatility-scaled unit of the realized volatility over that same window, calls it
    "trending"; otherwise "mean_reverting". Returns None if the outcome isn't knowable yet (not
    enough bars past the anchor) or the window is degenerate (flat prices, no volatility).
    """
    window = [b for b in bars if b.ts > anchor_ts]
    if len(window) < horizon_bars:
        return None
    window = window[:horizon_bars]
    anchor_close = next((b.close for b in bars if b.ts <= anchor_ts), window[0].close)
    if anchor_close <= 0:
        return None
    forward_return = (window[-1].close - anchor_close) / anchor_close
    log_returns = [
        math.log(window[i].close / window[i - 1].close)
        for i in range(1, len(window))
        if window[i - 1].close > 0 and window[i].close > 0
    ]
    if not log_returns:
        return None
    mean = sum(log_returns) / len(log_returns)
    realized_vol = math.sqrt(sum((r - mean) ** 2 for r in log_returns) / len(log_returns))
    threshold = realized_vol * math.sqrt(horizon_bars)
    if threshold == 0:
        return None  # degenerate flat series; can't score
    return "trending" if abs(forward_return) > threshold else "mean_reverting"


def _realized_vol(closes: list[float]) -> float:
    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]
    if not log_returns:
        return 0.0
    mean = sum(log_returns) / len(log_returns)
    return math.sqrt(sum((r - mean) ** 2 for r in log_returns) / len(log_returns))


def build_features(gex: GexEstimate, flow: FlowEstimate, bars: list[Bar]) -> dict[str, float]:
    """Feature vector for `RegimeClassifier`, built from already-computed GEX/flow estimates
    plus recent bars. All fields are guarded against the `None`/zero-division cases that
    `GexEstimate`/`FlowEstimate` can legitimately return."""
    net_gex_sign = 1.0 if gex.regime == "positive" else -1.0

    ratio = flow.call_put_premium_ratio
    call_put_premium_ratio_log = math.log(ratio) if ratio and ratio > 0 else 0.0

    window = bars[-LOOKBACK_BARS:]
    closes = [b.close for b in window]
    realized_vol = _realized_vol(closes) if len(closes) >= 2 else 0.0

    if len(closes) >= 2 and closes[0] > 0:
        trailing_return = (closes[-1] - closes[0]) / closes[0]
    else:
        trailing_return = 0.0
    return_z = trailing_return / (realized_vol + 1e-9)

    vol_window = 5
    rolling_vols = [
        _realized_vol(closes[i : i + vol_window])
        for i in range(0, max(len(closes) - vol_window + 1, 0))
    ]
    if len(rolling_vols) >= 2:
        vv_mean = sum(rolling_vols) / len(rolling_vols)
        vol_of_vol = math.sqrt(sum((v - vv_mean) ** 2 for v in rolling_vols) / len(rolling_vols))
    else:
        vol_of_vol = 0.0

    dist_to_call_wall_pct = (
        (gex.call_wall - gex.spot) / gex.spot if gex.call_wall and gex.spot > 0 else 0.0
    )
    dist_to_put_wall_pct = (
        (gex.spot - gex.put_wall) / gex.spot if gex.put_wall and gex.spot > 0 else 0.0
    )

    return {
        "net_gex_sign": net_gex_sign,
        "call_put_premium_ratio_log": call_put_premium_ratio_log,
        "realized_vol": realized_vol,
        "return_z": return_z,
        "vol_of_vol": vol_of_vol,
        "dist_to_call_wall_pct": dist_to_call_wall_pct,
        "dist_to_put_wall_pct": dist_to_put_wall_pct,
    }
