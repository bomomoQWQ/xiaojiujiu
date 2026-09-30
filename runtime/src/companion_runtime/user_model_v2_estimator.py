"""Pure NumPy/SciPy reference estimator for user-model v2.

The module deliberately knows nothing about Runtime, persistence, targets, or feature
encoding.  Callers supply an already-expanded design matrix (for example, a global
feature block plus one active behaviour-class block), labels, and effective weights.
It fits one binary target at a time with a Gaussian prior and returns the full Laplace
covariance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.linalg import cho_factor, cho_solve
from scipy.optimize import OptimizeResult, minimize
from scipy.special import expit
from scipy.stats import norm

FloatArray = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class GaussianPrior:
    """A multivariate Gaussian prior parameterised by mean and precision."""

    mean: FloatArray
    precision: FloatArray

    def __post_init__(self) -> None:
        mean = _as_vector("prior mean", self.mean)
        precision = np.asarray(self.precision, dtype=np.float64)
        if precision.shape != (mean.size, mean.size):
            raise ValueError("prior precision must be square and match prior mean")
        if not np.all(np.isfinite(precision)):
            raise ValueError("prior precision must contain only finite values")
        if not np.allclose(precision, precision.T, rtol=1e-12, atol=1e-12):
            raise ValueError("prior precision must be symmetric")
        try:
            np.linalg.cholesky(precision)
        except np.linalg.LinAlgError as exc:
            raise ValueError("prior precision must be positive definite") from exc
        object.__setattr__(self, "mean", mean.copy())
        object.__setattr__(self, "precision", precision.copy())


@dataclass(frozen=True, slots=True)
class LaplaceFit:
    """MAP parameters and a full-covariance Gaussian (Laplace) approximation."""

    map_parameters: FloatArray
    hessian: FloatArray
    covariance: FloatArray
    precision_cholesky: FloatArray
    covariance_cholesky: FloatArray
    objective: float
    support: str
    sample_count: int
    weight_sum: float
    converged: bool
    optimizer_message: str
    iterations: int


@dataclass(frozen=True, slots=True)
class LogitPrediction:
    """Plug-in prediction and parameter-uncertainty interval for one design row."""

    logit_mean: float
    logit_variance: float
    point_probability: float
    probability_lower: float
    probability_upper: float
    interval_level: float
    prediction_method: str = "plug_in"
    interval_kind: str = "laplace_parameter_credible"


def half_life_weights(
    observed_at: Sequence[datetime],
    *,
    fit_time: datetime,
    half_life_seconds: float,
) -> FloatArray:
    """Return one-time evidence decay ``2 ** (-age / half_life)``.

    This pure function fixes all ages at the supplied ``fit_time``.  It performs no
    caching or mutation, and therefore cannot accidentally age evidence once per
    heartbeat or prediction.  Future observations are rejected instead of increasing
    their weight.
    """

    if not isinstance(fit_time, datetime) or fit_time.tzinfo is None:
        raise ValueError("fit_time must be a timezone-aware datetime")
    if not np.isfinite(half_life_seconds) or half_life_seconds <= 0.0:
        raise ValueError("half_life_seconds must be finite and positive")

    ages: list[float] = []
    for stamp in observed_at:
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            raise ValueError("observed_at values must be timezone-aware datetimes")
        age = (fit_time - stamp).total_seconds()
        if age < 0.0:
            raise ValueError("observed_at must not be later than fit_time")
        ages.append(age)
    return np.exp2(-np.asarray(ages, dtype=np.float64) / half_life_seconds)


def objective_gradient_hessian(
    beta: ArrayLike,
    design_matrix: ArrayLike,
    labels: ArrayLike,
    weights: ArrayLike,
    prior: GaussianPrior,
) -> tuple[float, FloatArray, FloatArray]:
    """Evaluate the stable weighted negative log posterior and its derivatives."""

    beta_array = _as_vector("beta", beta)
    x, y, w = _validated_observations(design_matrix, labels, weights, beta_array.size)
    if prior.mean.size != beta_array.size:
        raise ValueError("prior dimension must match beta")

    logits = x @ beta_array
    difference = beta_array - prior.mean
    # logaddexp(0, z) is stable softplus for both extreme signs.
    objective = 0.5 * float(difference @ prior.precision @ difference)
    objective += float(np.sum(w * (np.logaddexp(0.0, logits) - y * logits)))

    probabilities = expit(logits)
    gradient = prior.precision @ difference + x.T @ (w * (probabilities - y))
    curvature = w * probabilities * (1.0 - probabilities)
    hessian = prior.precision + x.T @ (curvature[:, None] * x)
    return objective, np.asarray(gradient), np.asarray(hessian)


def fit_map_laplace(
    design_matrix: ArrayLike,
    labels: ArrayLike,
    weights: ArrayLike,
    prior: GaussianPrior,
    *,
    max_iterations: int = 500,
    gradient_tolerance: float = 1e-9,
) -> LaplaceFit:
    """Fit weighted binary logistic regression and form a full Laplace covariance.

    Rows are canonically sorted before optimisation.  This removes input-order effects
    from floating-point reduction and makes snapshot rebuilds deterministic.  A proper
    positive-definite prior keeps the MAP and covariance finite under complete
    separation.
    """

    if max_iterations <= 0:
        raise ValueError("max_iterations must be positive")
    if not np.isfinite(gradient_tolerance) or gradient_tolerance <= 0.0:
        raise ValueError("gradient_tolerance must be finite and positive")

    x, y, w = _validated_observations(
        design_matrix, labels, weights, prior.mean.size
    )
    x, y, w = _canonical_rows(x, y, w)

    if x.shape[0] == 0 or not np.any(w > 0.0):
        hessian = prior.precision.copy()
        covariance, precision_cholesky, covariance_cholesky = _factor_covariance(hessian)
        return LaplaceFit(
            map_parameters=prior.mean.copy(),
            hessian=hessian,
            covariance=covariance,
            precision_cholesky=precision_cholesky,
            covariance_cholesky=covariance_cholesky,
            objective=0.0,
            support="prior_only",
            sample_count=int(x.shape[0]),
            weight_sum=float(np.sum(w)),
            converged=True,
            optimizer_message="no positive-weight observations; returned prior",
            iterations=0,
        )

    def evaluate(beta: FloatArray) -> tuple[float, FloatArray, FloatArray]:
        return objective_gradient_hessian(beta, x, y, w, prior)

    result: OptimizeResult = minimize(
        fun=lambda beta: evaluate(beta)[0],
        x0=prior.mean.copy(),
        jac=lambda beta: evaluate(beta)[1],
        hess=lambda beta: evaluate(beta)[2],
        method="trust-exact",
        options={"maxiter": int(max_iterations), "gtol": float(gradient_tolerance)},
    )
    beta_hat = np.asarray(result.x, dtype=np.float64)
    objective, gradient, hessian = evaluate(beta_hat)
    # SciPy can report a precision-loss termination when it is already at a very
    # accurate optimum.  Do not hide a genuine failure, but accept a verified gradient.
    gradient_small = np.linalg.norm(gradient, ord=np.inf) <= max(
        10.0 * gradient_tolerance, 1e-8
    )
    converged = bool(result.success or gradient_small)
    if not converged:
        raise RuntimeError(f"MAP optimisation failed: {result.message}")
    if not np.all(np.isfinite(beta_hat)):
        raise RuntimeError("MAP optimisation produced non-finite parameters")

    covariance, precision_cholesky, covariance_cholesky = _factor_covariance(hessian)
    return LaplaceFit(
        map_parameters=beta_hat,
        hessian=hessian,
        covariance=covariance,
        precision_cholesky=precision_cholesky,
        covariance_cholesky=covariance_cholesky,
        objective=float(objective),
        support="informative",
        sample_count=int(x.shape[0]),
        weight_sum=float(np.sum(w)),
        converged=converged,
        optimizer_message=str(result.message),
        iterations=int(getattr(result, "nit", 0)),
    )


def predict_laplace(
    fit: LaplaceFit,
    design_row: ArrayLike,
    *,
    interval_level: float = 0.90,
) -> LogitPrediction:
    """Predict logit moments and a two-sided interval transformed by sigmoid."""

    if not 0.0 < interval_level < 1.0:
        raise ValueError("interval_level must be strictly between zero and one")
    row = _as_vector("design_row", design_row)
    if row.size != fit.map_parameters.size:
        raise ValueError("design_row dimension must match fitted parameters")

    mean = float(row @ fit.map_parameters)
    variance = float(row @ fit.covariance @ row)
    # Round-off can create a tiny negative quadratic form, never meaningful variance.
    if variance < -1e-12:
        raise RuntimeError("posterior covariance produced a negative logit variance")
    variance = max(0.0, variance)
    critical = float(norm.ppf(0.5 + interval_level / 2.0))
    radius = critical * np.sqrt(variance)
    return LogitPrediction(
        logit_mean=mean,
        logit_variance=variance,
        point_probability=float(expit(mean)),
        probability_lower=float(expit(mean - radius)),
        probability_upper=float(expit(mean + radius)),
        interval_level=float(interval_level),
    )


def _as_vector(name: str, value: ArrayLike) -> FloatArray:
    array = np.asarray(value, dtype=np.float64)
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must contain only finite values")
    return array


def _validated_observations(
    design_matrix: ArrayLike,
    labels: ArrayLike,
    weights: ArrayLike,
    dimension: int,
) -> tuple[FloatArray, FloatArray, FloatArray]:
    x = np.asarray(design_matrix, dtype=np.float64)
    if x.ndim != 2 or x.shape[1] != dimension:
        raise ValueError("design_matrix must be 2-D with one column per parameter")
    y = _as_vector("labels", labels)
    w = _as_vector("weights", weights)
    if y.size != x.shape[0] or w.size != x.shape[0]:
        raise ValueError("design_matrix, labels, and weights must have equal row counts")
    if not np.all(np.isfinite(x)):
        raise ValueError("design_matrix must contain only finite values")
    if np.any((y < 0.0) | (y > 1.0)):
        raise ValueError("labels must lie in [0, 1]")
    if np.any(w < 0.0):
        raise ValueError("weights must be non-negative")
    return x, y, w


def _canonical_rows(
    x: FloatArray, y: FloatArray, w: FloatArray
) -> tuple[FloatArray, FloatArray, FloatArray]:
    if x.shape[0] < 2:
        return x, y, w
    combined = np.column_stack((x, y, w))
    keys = tuple(combined[:, index] for index in reversed(range(combined.shape[1])))
    order = np.lexsort(keys)
    return x[order], y[order], w[order]


def _factor_covariance(
    hessian: FloatArray,
) -> tuple[FloatArray, FloatArray, FloatArray]:
    try:
        factor, lower = cho_factor(hessian, lower=True, check_finite=True)
        covariance = cho_solve((factor, lower), np.eye(hessian.shape[0]), check_finite=True)
        covariance = 0.5 * (covariance + covariance.T)
        covariance_cholesky = np.linalg.cholesky(covariance)
    except (ValueError, np.linalg.LinAlgError) as exc:
        raise RuntimeError("posterior Hessian is not positive definite") from exc
    precision_cholesky = np.tril(factor).copy()
    return covariance, precision_cholesky, covariance_cholesky
