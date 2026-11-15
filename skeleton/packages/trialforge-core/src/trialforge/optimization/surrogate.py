"""Gaussian-process surrogate (Matern-5/2, ARD) with analytic acquisition functions.

Implemented with numpy/scipy so the core has no heavy ML dependency. Hyper-parameters are fit by
maximising the log marginal likelihood (multi-start L-BFGS-B). Predictions are the GP posterior
under its modelling assumptions (smooth, stationary response, Gaussian noise); they are *not*
guaranteed calibrated if those assumptions fail, and the Recommendation says so.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import stats as sps
from scipy.linalg import cho_factor, cho_solve
from scipy.optimize import minimize


def _matern52(A: np.ndarray, B: np.ndarray, ls: np.ndarray, sf2: float) -> np.ndarray:
    d = (A[:, None, :] - B[None, :, :]) / ls
    r = np.sqrt(np.maximum((d**2).sum(-1), 0.0))
    s5r = math.sqrt(5.0) * r
    return sf2 * (1.0 + s5r + 5.0 * r**2 / 3.0) * np.exp(-s5r)


class GaussianProcess:
    """GP regression on inputs scaled to [0, 1]^d. Minimisation convention elsewhere."""

    def __init__(self, X: np.ndarray, y: np.ndarray, *, seed: int = 0, restarts: int = 4) -> None:
        self.X = np.atleast_2d(np.asarray(X, float))
        y = np.asarray(y, float)
        self.y_mean = float(y.mean())
        self.y_std = float(y.std()) if y.std() > 1e-12 else 1.0
        self.y = (y - self.y_mean) / self.y_std
        self.d = self.X.shape[1]
        self._fit(seed, restarts)

    def _unpack(self, theta: np.ndarray):
        return np.exp(theta[: self.d]), float(np.exp(theta[self.d])), float(np.exp(theta[self.d + 1]))

    def _nll(self, theta: np.ndarray) -> float:
        ls, sf2, sn2 = self._unpack(theta)
        n = len(self.y)
        K = _matern52(self.X, self.X, ls, sf2) + (sn2 + 1e-8) * np.eye(n)
        try:
            c = cho_factor(K, lower=True)
        except np.linalg.LinAlgError:
            return 1e10
        alpha = cho_solve(c, self.y)
        val = 0.5 * self.y @ alpha + np.log(np.diag(c[0])).sum() + 0.5 * n * math.log(2 * math.pi)
        # weak log-normal priors keep lengthscales/noise away from degenerate corners
        val += 0.5 * ((theta[: self.d] - math.log(0.5)) / 1.0) ** 2 @ np.ones(self.d) * 0.1
        return float(val)

    def _fit(self, seed: int, restarts: int) -> None:
        rng = np.random.default_rng(seed)
        bounds = [(math.log(0.05), math.log(5.0))] * self.d + [(math.log(0.05), math.log(10.0)), (math.log(1e-4), math.log(1.0))]
        best = None
        for i in range(restarts):
            x0 = np.array(
                [rng.uniform(math.log(0.1), math.log(2.0)) for _ in range(self.d)]
                + [math.log(1.0), math.log(10 ** rng.uniform(-3, -1))]
            )
            res = minimize(self._nll, x0, method="L-BFGS-B", bounds=bounds)
            if best is None or res.fun < best.fun:
                best = res
        self.theta = best.x
        self.ls, self.sf2, self.sn2 = self._unpack(self.theta)
        K = _matern52(self.X, self.X, self.ls, self.sf2) + (self.sn2 + 1e-8) * np.eye(len(self.y))
        self._chol = cho_factor(K, lower=True)
        self._alpha = cho_solve(self._chol, self.y)

    def predict(self, Xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Posterior mean and standard deviation of the latent function, original units."""
        Xs = np.atleast_2d(Xs)
        Ks = _matern52(Xs, self.X, self.ls, self.sf2)
        mu = Ks @ self._alpha
        v = cho_solve(self._chol, Ks.T)
        var = np.maximum(self.sf2 - np.einsum("ij,ji->i", Ks, v), 1e-12)
        return mu * self.y_std + self.y_mean, np.sqrt(var) * self.y_std


def expected_improvement(mu: np.ndarray, sd: np.ndarray, best: float, xi: float = 0.0) -> np.ndarray:
    """EI for *minimisation*: E[max(best - xi - f, 0)] with f ~ N(mu, sd^2)."""
    imp = best - xi - mu
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(sd > 0, imp / sd, 0.0)
    ei = imp * sps.norm.cdf(z) + sd * sps.norm.pdf(z)
    return np.where(sd > 1e-12, np.maximum(ei, 0.0), np.maximum(imp, 0.0))


def prob_in_interval(mu: np.ndarray, sd: np.ndarray, lo: float | None, hi: float | None) -> np.ndarray:
    """P(lo <= f <= hi) for f ~ N(mu, sd^2)."""
    sd = np.maximum(sd, 1e-12)
    upper = sps.norm.cdf((hi - mu) / sd) if hi is not None else 1.0
    lower = sps.norm.cdf((lo - mu) / sd) if lo is not None else 0.0
    return np.clip(upper - lower, 0.0, 1.0)
