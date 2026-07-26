"""Server-side inference: random forest classification and risk scoring."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

from app.config import get_settings

logger = logging.getLogger(__name__)

LABELS: tuple[str, ...] = ("SAFE", "WARNING", "FIRE")

#: Must match ml/generate_dataset.py FEATURE_COLUMNS exactly.
FEATURE_COLUMNS: tuple[str, ...] = (
    "temperature_c",
    "humidity_pct",
    "smoke_ppm",
    "air_quality_ppm",
    "flame_analog_volts",
    "flame_detected",
    "temp_rate_c_per_min",
    "smoke_rate_ppm_per_min",
    "heat_index_c",
)


def compute_heat_index(temp_c: float, rh_pct: float) -> float:
    """NOAA Rothfusz heat index in Celsius. Mirrors ml/generate_dataset.py."""
    t_f = temp_c * 9.0 / 5.0 + 32.0
    if t_f >= 80.0:
        hi_f = (
            -42.379
            + 2.04901523 * t_f
            + 10.14333127 * rh_pct
            - 0.22475541 * t_f * rh_pct
            - 6.83783e-3 * t_f**2
            - 5.481717e-2 * rh_pct**2
            + 1.22874e-3 * t_f**2 * rh_pct
            + 8.5282e-4 * t_f * rh_pct**2
            - 1.99e-6 * t_f**2 * rh_pct**2
        )
    else:
        hi_f = 0.5 * (t_f + 61.0 + (t_f - 68.0) * 1.2 + rh_pct * 0.094)
    return (hi_f - 32.0) * 5.0 / 9.0


class FireClassifier:
    """Wraps the trained random forest.

    Thread-safe: the MQTT ingest thread and HTTP request handlers both call
    ``predict``. scikit-learn's ``predict_proba`` is read-only on a fitted
    estimator, but model *loading* is guarded so a reload cannot be observed
    half-complete.
    """

    def __init__(self, model_dir: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._model: Any | None = None
        self._feature_columns: tuple[str, ...] = FEATURE_COLUMNS
        self._model_dir = model_dir or get_settings().model_dir

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._model is not None

    def load(self) -> bool:
        """Load the random forest from disk. Returns True on success.

        A missing model is not fatal: the service degrades to threshold-only
        classification so the pipeline still runs (and still alarms) on a fresh
        clone where ``ml/train.py`` has not been executed yet.
        """
        path = Path(self._model_dir) / "random_forest.joblib"
        columns_path = Path(self._model_dir) / "feature_columns.json"
        if not path.exists():
            logger.warning(
                "random forest not found at %s - falling back to threshold rules; "
                "run `python ml/train.py` to enable model inference",
                path,
            )
            return False
        try:
            import joblib

            model = joblib.load(path)

            # The forest was fitted with n_jobs=-1, which joblib persists. At
            # serving time we classify ONE row at a time, and spinning up a
            # worker pool per call costs ~85 ms against ~1 ms of actual work.
            # Forcing single-threaded inference is ~50x faster here and leaves
            # the CPU free for the rest of the request.
            if hasattr(model, "n_jobs"):
                model.n_jobs = 1

            columns = self._feature_columns
            if columns_path.exists():
                columns = tuple(json.loads(columns_path.read_text(encoding="utf-8")))
            if len(columns) != getattr(model, "n_features_in_", len(columns)):
                raise ValueError(
                    f"model expects {model.n_features_in_} features but "
                    f"feature_columns.json lists {len(columns)}"
                )
        except Exception:
            logger.exception("failed to load random forest from %s", path)
            return False

        with self._lock:
            self._model = model
            self._feature_columns = columns
        logger.info("loaded random forest from %s (%d features)", path, len(columns))
        return True

    def build_features(self, values: dict[str, float]) -> np.ndarray:
        """Assemble a feature vector in the canonical order."""
        row = [float(values.get(name, 0.0)) for name in self._feature_columns]
        return np.asarray([row], dtype=np.float64)

    def predict(self, values: dict[str, float]) -> tuple[str, dict[str, float]]:
        """Classify one sample.

        Returns ``(status, probabilities)``. Falls back to deterministic
        threshold rules when no model is loaded, so callers never have to
        special-case an unloaded classifier.
        """
        with self._lock:
            model = self._model

        if model is None:
            return _threshold_fallback(values)

        try:
            features = self.build_features(values)
            proba = model.predict_proba(features)[0]
            classes = [int(c) for c in model.classes_]
        except Exception:
            logger.exception("inference failed; falling back to threshold rules")
            return _threshold_fallback(values)

        probabilities = dict.fromkeys(LABELS, 0.0)
        for class_index, p in zip(classes, proba, strict=True):
            if 0 <= class_index < len(LABELS):
                probabilities[LABELS[class_index]] = float(p)

        status = max(probabilities, key=lambda key: probabilities[key])
        return status, probabilities


def _threshold_fallback(values: dict[str, float]) -> tuple[str, dict[str, float]]:
    """Deterministic rules mirroring the dataset's labelling logic.

    Used when the trained model is unavailable. Intentionally conservative:
    it is biased toward raising FIRE, matching the project's stance that false
    negatives are unacceptable and false positives are tolerable.
    """
    temp = values.get("temperature_c", 0.0)
    smoke = values.get("smoke_ppm", 0.0)
    air = values.get("air_quality_ppm", 0.0)
    flame_v = values.get("flame_analog_volts", 0.0)

    flame = flame_v > 1.15
    hot = temp > 45.0
    very_hot = temp > 58.0
    heavy_smoke = smoke > 900.0
    severe_smoke = smoke > 2200.0
    bad_air = air > 300.0

    if (flame and (hot or heavy_smoke)) or (very_hot and heavy_smoke) or severe_smoke:
        return "FIRE", {"SAFE": 0.0, "WARNING": 0.1, "FIRE": 0.9}
    if hot or heavy_smoke or bad_air or flame:
        return "WARNING", {"SAFE": 0.2, "WARNING": 0.7, "FIRE": 0.1}
    return "SAFE", {"SAFE": 0.9, "WARNING": 0.1, "FIRE": 0.0}


def compute_risk_score(probabilities: dict[str, float]) -> float:
    """Collapse the class distribution into a single 0-100 risk figure.

    WARNING contributes at half weight, FIRE at full. This gives the dashboard
    gauge a continuous signal that rises through a developing event instead of
    jumping between three discrete states.
    """
    warning = probabilities.get("WARNING", 0.0)
    fire = probabilities.get("FIRE", 0.0)
    return round(min(100.0, max(0.0, (0.5 * warning + fire) * 100.0)), 2)


_classifier: FireClassifier | None = None


def get_classifier() -> FireClassifier:
    """Process-wide classifier singleton."""
    global _classifier
    if _classifier is None:
        _classifier = FireClassifier()
        _classifier.load()
    return _classifier


def reset_classifier() -> None:
    """Drop the singleton. Used by tests."""
    global _classifier
    _classifier = None
