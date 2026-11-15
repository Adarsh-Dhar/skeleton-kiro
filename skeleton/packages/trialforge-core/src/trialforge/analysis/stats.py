"""Numerical statistics used by the analysis engine (numpy/scipy only).

All inference is t-based. Functions return plain dicts and never raise on small samples:
they return ``None`` for quantities the data cannot support, so callers must report
"insufficient evidence" rather than a fabricated interval.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from scipy import stats as sps


def describe(x: Sequence[float]) -> dict[str, float | int | None]:
    a = np.asarray(x, dtype=float)
    n = a.size
    if n == 0:
        return {k: None for k in ("mean", "median", "min", "max", "range", "std_dev", "variance", "std_error")} | {"count": 0}
    var = float(a.var(ddof=1)) if n > 1 else None
    sd = math.sqrt(var) if var is not None else None
    return {
        "mean": float(a.mean()),
        "median": float(np.median(a)),
        "min": float(a.min()),
        "max": float(a.max()),
        "range": float(a.max() - a.min()),
        "std_dev": sd,
        "variance": var,
        "std_error": sd / math.sqrt(n) if sd is not None else None,
        "count": int(n),
    }


def welch(control: Sequence[float], treatment: Sequence[float], alpha: float = 0.05) -> dict | None:
    """Welch t comparison of treatment - control. None unless both groups have n >= 2."""
    a, b = np.asarray(control, float), np.asarray(treatment, float)
    if a.size < 2 or b.size < 2:
        return None
    va, vb = a.var(ddof=1) / a.size, b.var(ddof=1) / b.size
    diff = float(b.mean() - a.mean())
    se2 = va + vb
    if se2 == 0:
        # zero variance in both groups: difference is exact in the sample but inference undefined
        return {"diff": diff, "se": 0.0, "df": None, "t": None, "p": 1.0 if diff == 0 else None, "ci": None, "degenerate": True}
    se = math.sqrt(se2)
    df = se2**2 / (va**2 / (a.size - 1) + vb**2 / (b.size - 1))
    t = diff / se
    p = float(2 * sps.t.sf(abs(t), df))
    h = float(sps.t.ppf(1 - alpha / 2, df)) * se
    return {"diff": diff, "se": se, "df": float(df), "t": float(t), "p": p, "ci": (diff - h, diff + h), "degenerate": False}


def paired(control: Sequence[float], treatment: Sequence[float], alpha: float = 0.05) -> dict | None:
    """Paired t on d = treatment - control (aligned). None unless n >= 2 pairs."""
    a, b = np.asarray(control, float), np.asarray(treatment, float)
    if a.size != b.size or a.size < 2:
        return None
    d = b - a
    n = d.size
    diff = float(d.mean())
    sd = float(d.std(ddof=1))
    if sd == 0:
        return {
            "diff": diff,
            "se": 0.0,
            "df": n - 1,
            "t": None,
            "p": 1.0 if diff == 0 else None,
            "ci": None,
            "degenerate": True,
            "dz": None,
        }
    se = sd / math.sqrt(n)
    t = diff / se
    p = float(2 * sps.t.sf(abs(t), n - 1))
    h = float(sps.t.ppf(1 - alpha / 2, n - 1)) * se
    return {
        "diff": diff,
        "se": se,
        "df": float(n - 1),
        "t": float(t),
        "p": p,
        "ci": (diff - h, diff + h),
        "degenerate": False,
        "dz": diff / sd,
    }


def hedges_g(control: Sequence[float], treatment: Sequence[float]) -> float | None:
    """Bias-corrected standardised mean difference (treatment - control); None if undefined."""
    a, b = np.asarray(control, float), np.asarray(treatment, float)
    na, nb = a.size, b.size
    if na < 2 or nb < 2:
        return None
    sp2 = ((na - 1) * a.var(ddof=1) + (nb - 1) * b.var(ddof=1)) / (na + nb - 2)
    if sp2 <= 0:
        return None
    d = (b.mean() - a.mean()) / math.sqrt(sp2)
    j = 1 - 3 / (4 * (na + nb) - 9)
    return float(d * j)


def bootstrap_ci_diff(
    control: Sequence[float],
    treatment: Sequence[float],
    *,
    seed: int = 0,
    n_boot: int = 4000,
    alpha: float = 0.05,
    paired_data: bool = False,
) -> tuple[float, float] | None:
    """Percentile bootstrap CI for mean(treatment) - mean(control); needs n >= 3 per group."""
    a, b = np.asarray(control, float), np.asarray(treatment, float)
    if a.size < 3 or b.size < 3:
        return None
    rng = np.random.default_rng(seed)
    if paired_data:
        if a.size != b.size:
            return None
        d = b - a
        idx = rng.integers(0, d.size, size=(n_boot, d.size))
        boots = d[idx].mean(axis=1)
    else:
        ia = rng.integers(0, a.size, size=(n_boot, a.size))
        ib = rng.integers(0, b.size, size=(n_boot, b.size))
        boots = b[ib].mean(axis=1) - a[ia].mean(axis=1)
    lo, hi = np.quantile(boots, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def shapiro_p(x: Sequence[float]) -> float | None:
    a = np.asarray(x, float)
    if a.size < 3 or a.size > 5000 or np.ptp(a) == 0:
        return None
    return float(sps.shapiro(a).pvalue)


def levene_p(groups: Sequence[Sequence[float]]) -> float | None:
    gs = [np.asarray(g, float) for g in groups if len(g) >= 2]
    if len(gs) < 2:
        return None
    try:
        return float(sps.levene(*gs, center="median").pvalue)
    except ValueError:
        return None


def holm_adjust(pvalues: list[float | None]) -> list[float | None]:
    """Holm step-down adjusted p-values; None entries are skipped."""
    idx = [i for i, p in enumerate(pvalues) if p is not None]
    order = sorted(idx, key=lambda i: pvalues[i])
    m = len(order)
    out: list[float | None] = [None] * len(pvalues)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        out[i] = running
    return out


# ------------------------------------------------------------------ OLS
def independent_columns(X: np.ndarray) -> list[int]:
    """Greedy left-to-right selection of linearly independent columns (earlier columns win)."""
    keep: list[int] = []
    rank = 0
    for j in range(X.shape[1]):
        r = np.linalg.matrix_rank(X[:, keep + [j]], tol=1e-9 * max(1.0, np.abs(X).max()))
        if r > rank:
            keep.append(j)
            rank = r
    return keep


def rss_rank(X: np.ndarray, y: np.ndarray) -> tuple[float, int]:
    """Residual sum of squares and rank of the least-squares fit of y on X."""
    if X.shape[1] == 0:
        return float(((y - 0.0) ** 2).sum()), 0
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    r = y - X @ beta
    return float(r @ r), int(np.linalg.matrix_rank(X, tol=1e-9 * max(1.0, np.abs(X).max())))


def ols(X: np.ndarray, y: np.ndarray, alpha: float = 0.05) -> dict:
    """OLS on full-column-rank X (intercept expected in X). Returns inference where df > 0."""
    n, p = X.shape
    XtX_inv = np.linalg.pinv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    fitted = X @ beta
    resid = y - fitted
    rss = float(resid @ resid)
    df = n - p
    has_int = bool(np.allclose(X[:, 0], 1.0))
    tss = float(((y - y.mean()) ** 2).sum()) if has_int else float((y**2).sum())
    out: dict = {"beta": beta, "fitted": fitted, "resid": resid, "rss": rss, "df": df, "n": n, "p": p}
    hat = np.einsum("ij,jk,ik->i", X, XtX_inv, X)
    out["leverage"] = hat
    out["r2"] = 1 - rss / tss if tss > 0 else None
    if df > 0:
        s2 = rss / df
        se = np.sqrt(np.maximum(np.diag(XtX_inv) * s2, 0))
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.where(se > 0, beta / se, np.nan)
        pv = 2 * sps.t.sf(np.abs(t), df)
        crit = sps.t.ppf(1 - alpha / 2, df)
        out.update(s2=s2, sigma=math.sqrt(s2), se=se, t=t, pvalue=pv, ci=np.column_stack([beta - crit * se, beta + crit * se]))
        if out["r2"] is not None and has_int and p > 1 and tss > 0:
            out["adj_r2"] = 1 - (1 - out["r2"]) * (n - 1) / df
            msr = (tss - rss) / (p - 1)
            if s2 > 0:
                f = msr / s2
                out["f"], out["f_p"] = float(f), float(sps.f.sf(f, p - 1, df))
            out["f_df"] = (p - 1, df)
        cooks = (resid**2 / (p * s2)) * (hat / np.maximum((1 - hat) ** 2, 1e-12)) if s2 > 0 else np.zeros(n)
        out["cooks"] = cooks
        with np.errstate(divide="ignore", invalid="ignore"):
            out["student_resid"] = resid / (math.sqrt(s2) * np.sqrt(np.maximum(1 - hat, 1e-12))) if s2 > 0 else np.zeros(n)
    return out


def durbin_watson(resid: np.ndarray) -> float | None:
    d = float((resid**2).sum())
    return float((np.diff(resid) ** 2).sum() / d) if resid.size > 1 and d > 0 else None


def breusch_pagan_p(X: np.ndarray, resid: np.ndarray) -> float | None:
    """Koenker's studentised BP test: n*R^2 of squared residuals on X (needs df margin)."""
    n, p = X.shape
    if n - p < 3 or p < 2:
        return None
    u = resid**2
    rss, _ = rss_rank(X, u)
    tss = float(((u - u.mean()) ** 2).sum())
    if tss <= 0:
        return None
    lm = n * (1 - rss / tss)
    return float(sps.chi2.sf(max(lm, 0.0), p - 1))
