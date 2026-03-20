"""
Gradient boosted probability model trained on historical Polymarket outcomes.
Predicts P(YES wins) for a given market state.
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
from loguru import logger

try:
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.model_selection import cross_val_score
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

from polymarket_index.ml.features import FeatureExtractor, MarketFeatures

MODEL_PATH = Path("models/probability_model.pkl")
METRICS_PATH = Path("models/model_metrics.json")


class ProbabilityModel:
    """
    Gradient boosted classifier that predicts P(YES outcome)
    for Polymarket markets.
    """

    def __init__(self) -> None:
        if not HAS_SKLEARN:
            raise ImportError("scikit-learn is required: pip install scikit-learn")

        self._model: GradientBoostingClassifier | None = None
        self._calibrated: CalibratedClassifierCV | None = None
        self._is_trained = False
        self._feature_names = MarketFeatures.feature_names()

    def train(
        self,
        X: np.ndarray,
        y: np.ndarray,
        calibrate: bool = True,
    ) -> dict:
        """
        Train the model on feature matrix X and binary labels y.
        Returns training metrics.
        """
        logger.info("Training model on {} samples, {} features", X.shape[0], X.shape[1])

        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        self._model = GradientBoostingClassifier(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            min_samples_leaf=20,
            random_state=42,
        )

        scores = cross_val_score(self._model, X, y, cv=5, scoring="roc_auc")
        logger.info("Cross-val AUC: {:.4f} (+/- {:.4f})", scores.mean(), scores.std())

        accuracy_scores = cross_val_score(self._model, X, y, cv=5, scoring="accuracy")
        brier_scores = cross_val_score(self._model, X, y, cv=5, scoring="neg_brier_score")

        self._model.fit(X, y)

        if calibrate and X.shape[0] > 200:
            self._calibrated = CalibratedClassifierCV(self._model, cv=3, method="isotonic")
            self._calibrated.fit(X, y)
            logger.info("Model calibrated with isotonic regression")

        self._is_trained = True

        importances = dict(zip(self._feature_names, self._model.feature_importances_))
        top_features = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:10]

        metrics = {
            "samples": int(X.shape[0]),
            "features": int(X.shape[1]),
            "auc_mean": round(float(scores.mean()), 4),
            "auc_std": round(float(scores.std()), 4),
            "accuracy_mean": round(float(accuracy_scores.mean()), 4),
            "brier_mean": round(float(-brier_scores.mean()), 4),
            "top_features": {k: round(float(v), 4) for k, v in top_features},
            "positive_rate": round(float(y.mean()), 4),
        }

        logger.info("Model trained — AUC={:.4f} Accuracy={:.4f} Brier={:.4f}",
                     metrics["auc_mean"], metrics["accuracy_mean"], metrics["brier_mean"])

        return metrics

    def predict_proba(self, features: MarketFeatures) -> float:
        """Predict P(YES wins) for a given market state."""
        if not self._is_trained:
            return features.yes_price  # fallback to market price

        X = features.to_array().reshape(1, -1)
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        predictor = self._calibrated if self._calibrated else self._model
        proba = predictor.predict_proba(X)[0]

        # proba[1] = P(YES), proba[0] = P(NO)
        return float(proba[1]) if len(proba) > 1 else float(proba[0])

    def predict_batch(self, feature_list: list[MarketFeatures]) -> list[float]:
        if not self._is_trained:
            return [f.yes_price for f in feature_list]

        X = np.array([f.to_array() for f in feature_list])
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        predictor = self._calibrated if self._calibrated else self._model
        probas = predictor.predict_proba(X)
        return [float(p[1]) if len(p) > 1 else float(p[0]) for p in probas]

    def save(self, path: Path | None = None) -> None:
        path = path or MODEL_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "model": self._model,
                "calibrated": self._calibrated,
                "feature_names": self._feature_names,
            }, f)
        logger.info("Model saved to {}", path)

    def load(self, path: Path | None = None) -> bool:
        path = path or MODEL_PATH
        if not path.exists():
            logger.debug("No model file at {}", path)
            return False
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            self._model = data["model"]
            self._calibrated = data.get("calibrated")
            self._feature_names = data.get("feature_names", MarketFeatures.feature_names())
            self._is_trained = True
            logger.info("Model loaded from {}", path)
            return True
        except Exception as exc:
            logger.warning("Failed to load model: {}", exc)
            return False

    @property
    def is_trained(self) -> bool:
        return self._is_trained


def train_from_cached_data() -> dict:
    """
    Train the model using cached backtest data.
    Looks for trade history JSON files in backtest_data/wallets/.
    """
    data_dir = Path("backtest_data/wallets")
    if not data_dir.exists():
        logger.warning("No cached data found at {}", data_dir)
        return {}

    extractor = FeatureExtractor()
    X_list: list[np.ndarray] = []
    y_list: list[float] = []

    for f in data_dir.glob("*.json"):
        data = json.loads(f.read_text())
        for trade in data.get("trades", []):
            result = extractor.extract_training_sample(trade)
            if result:
                features, label = result
                X_list.append(features)
                y_list.append(label)

    if len(X_list) < 100:
        logger.warning("Not enough training data ({} samples, need 100+)", len(X_list))
        return {}

    X = np.array(X_list)
    y = np.array(y_list)
    logger.info("Training set: {} samples, {:.1%} positive rate", len(y), y.mean())

    model = ProbabilityModel()
    metrics = model.train(X, y)
    model.save()

    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    METRICS_PATH.write_text(json.dumps(metrics, indent=2))

    return metrics
