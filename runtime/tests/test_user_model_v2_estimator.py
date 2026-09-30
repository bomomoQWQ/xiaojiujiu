"""Numerical contract tests for the isolated user-model v2 estimator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from numpy.testing import assert_allclose
from scipy.optimize._numdiff import approx_derivative

from companion_runtime.user_model_v2_estimator import (
    GaussianPrior,
    fit_map_laplace,
    half_life_weights,
    objective_gradient_hessian,
    predict_laplace,
)


def standard_prior(dimension: int) -> GaussianPrior:
    return GaussianPrior(mean=np.zeros(dimension), precision=np.eye(dimension))


def test_objective_gradient_and_hessian_match_finite_differences() -> None:
    design = np.array(
        [[1.0, -0.7, 0.0], [1.0, 0.2, 1.1], [1.0, 1.3, -0.4], [1.0, 0.0, 0.8]]
    )
    labels = np.array([0.0, 1.0, 1.0, 0.0])
    weights = np.array([0.3, 1.0, 0.6, 0.8])
    prior = GaussianPrior(
        mean=np.array([0.2, -0.1, 0.3]),
        precision=np.array([[1.5, 0.1, 0.0], [0.1, 2.0, 0.2], [0.0, 0.2, 1.2]]),
    )
    beta = np.array([0.35, -0.45, 0.15])

    objective, gradient, hessian = objective_gradient_hessian(
        beta, design, labels, weights, prior
    )
    numerical_gradient = approx_derivative(
        lambda value: np.array(
            [objective_gradient_hessian(value, design, labels, weights, prior)[0]]
        ),
        beta,
        method="3-point",
    ).reshape(-1)
    numerical_hessian = approx_derivative(
        lambda value: objective_gradient_hessian(value, design, labels, weights, prior)[1],
        beta,
        method="3-point",
    )

    assert np.isfinite(objective)
    assert_allclose(gradient, numerical_gradient, rtol=2e-6, atol=2e-7)
    assert_allclose(hessian, numerical_hessian, rtol=2e-6, atol=2e-7)


def test_extreme_logits_are_stable_and_zero_feature_has_zero_likelihood_curvature() -> None:
    prior = standard_prior(2)
    beta = np.array([1000.0, -1000.0])
    design = np.array([[1.0, 0.0], [-1.0, 0.0]])
    objective, gradient, hessian = objective_gradient_hessian(
        beta, design, np.array([0.0, 1.0]), np.array([0.072, 0.072]), prior
    )

    assert np.isfinite(objective)
    assert np.all(np.isfinite(gradient))
    assert np.all(np.isfinite(hessian))
    likelihood_curvature = hessian - prior.precision
    assert likelihood_curvature[1, 1] == pytest.approx(0.0, abs=1e-15)
    assert likelihood_curvature[0, 1] == pytest.approx(0.0, abs=1e-15)

    _, _, at_half = objective_gradient_hessian(
        np.zeros(2), np.array([[1.0, 0.0]]), [1.0], [0.072], prior
    )
    assert (at_half - prior.precision)[0, 0] == pytest.approx(0.018)


def test_empty_data_returns_exact_prior_and_prior_only_support() -> None:
    prior = GaussianPrior(
        mean=np.array([0.4, -0.3]), precision=np.array([[2.0, 0.25], [0.25, 1.5]])
    )
    fit = fit_map_laplace(np.empty((0, 2)), [], [], prior)

    assert fit.support == "prior_only"
    assert fit.sample_count == 0
    assert fit.weight_sum == 0.0
    assert fit.iterations == 0
    assert_allclose(fit.map_parameters, prior.mean, rtol=0.0, atol=0.0)
    assert_allclose(fit.hessian, prior.precision, rtol=0.0, atol=0.0)
    assert_allclose(fit.covariance, np.linalg.inv(prior.precision), rtol=1e-14, atol=1e-14)
    assert_allclose(fit.covariance_cholesky @ fit.covariance_cholesky.T, fit.covariance)


def test_complete_separation_remains_finite_with_proper_prior() -> None:
    design = np.array([[1.0, -3.0], [1.0, -2.0], [1.0, 2.0], [1.0, 3.0]])
    labels = np.array([0.0, 0.0, 1.0, 1.0])
    fit = fit_map_laplace(design, labels, np.ones(4), standard_prior(2))

    assert fit.converged
    assert fit.support == "informative"
    assert np.all(np.isfinite(fit.map_parameters))
    assert np.all(np.isfinite(fit.covariance))
    assert np.all(np.linalg.eigvalsh(fit.covariance) > 0.0)


def test_full_covariance_prediction_includes_global_and_behaviour_class_blocks() -> None:
    # Caller supplies ξ directly: [global bias, global slope, class bias, class slope].
    design = np.array(
        [
            [1.0, -1.0, 1.0, -1.0],
            [1.0, 0.5, 1.0, 0.5],
            [1.0, 1.0, 0.0, 0.0],
            [1.0, -0.5, 0.0, 0.0],
        ]
    )
    fit = fit_map_laplace(design, [0.0, 1.0, 1.0, 0.0], np.ones(4), standard_prior(4))
    row = np.array([1.0, 0.25, 1.0, 0.25])
    prediction = predict_laplace(fit, row, interval_level=0.90)

    assert prediction.logit_mean == pytest.approx(float(row @ fit.map_parameters))
    assert prediction.logit_variance == pytest.approx(float(row @ fit.covariance @ row))
    assert 0.0 < prediction.probability_lower <= prediction.point_probability
    assert prediction.point_probability <= prediction.probability_upper < 1.0
    assert prediction.interval_level == 0.90
    assert prediction.prediction_method == "plug_in"


def test_half_life_weights_are_pure_and_fixed_to_one_fit_time() -> None:
    fit_time = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    stamps = [fit_time, fit_time - timedelta(hours=12), fit_time - timedelta(days=1)]

    first = half_life_weights(stamps, fit_time=fit_time, half_life_seconds=12 * 3600)
    second = half_life_weights(stamps, fit_time=fit_time, half_life_seconds=12 * 3600)

    assert_allclose(first, [1.0, 0.5, 0.25], rtol=1e-15, atol=0.0)
    assert_allclose(second, first, rtol=0.0, atol=0.0)
    with pytest.raises(ValueError, match="later than fit_time"):
        half_life_weights(
            [fit_time + timedelta(seconds=1)],
            fit_time=fit_time,
            half_life_seconds=3600,
        )


def test_batch_fit_is_independent_of_sample_order() -> None:
    rng = np.random.default_rng(20260930)
    design = np.column_stack((np.ones(40), rng.normal(size=(40, 3))))
    labels = rng.integers(0, 2, size=40).astype(float)
    weights = rng.uniform(0.05, 1.0, size=40)
    permutation = rng.permutation(40)
    prior = standard_prior(4)

    original = fit_map_laplace(design, labels, weights, prior)
    shuffled = fit_map_laplace(
        design[permutation], labels[permutation], weights[permutation], prior
    )

    assert_allclose(shuffled.map_parameters, original.map_parameters, rtol=0.0, atol=0.0)
    assert_allclose(shuffled.hessian, original.hessian, rtol=0.0, atol=0.0)
    assert_allclose(shuffled.covariance, original.covariance, rtol=0.0, atol=0.0)
    assert shuffled.objective == original.objective
