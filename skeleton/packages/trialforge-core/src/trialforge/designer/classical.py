"""Pure-Python classical design matrices (coded units, no domain knowledge).

All matrices are lists of rows with entries in {-1, +1} (or real coded values for
response-surface designs). Aliasing, defining relations and resolution are computed
from the generators, never read from a lookup of claimed properties.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence

from trialforge.errors import DesignError

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Recommended generators for highest resolution; the actual resolution is *recomputed*.
STANDARD_GENERATORS: dict[tuple[int, int], list[str]] = {
    (3, 1): ["C=AB"],
    (4, 1): ["D=ABC"],
    (5, 1): ["E=ABCD"],
    (5, 2): ["D=AB", "E=AC"],
    (6, 1): ["F=ABCDE"],
    (6, 2): ["E=ABC", "F=BCD"],
    (6, 3): ["D=AB", "E=AC", "F=BC"],
    (7, 1): ["G=ABCDEF"],
    (7, 2): ["F=ABCD", "G=ABDE"],
    (7, 3): ["E=ABC", "F=BCD", "G=ACD"],
    (7, 4): ["D=AB", "E=AC", "F=BC", "G=ABC"],
    (8, 2): ["G=ABCD", "H=ABEF"],
    (8, 3): ["F=ABC", "G=ABD", "H=BCDE"],
    (8, 4): ["E=BCD", "F=ACD", "G=ABC", "H=ABD"],
}


def full_factorial_2level(k: int) -> list[list[int]]:
    """Standard (Yates) order, first factor varies fastest."""
    return [[-1 if (i >> j) & 1 == 0 else 1 for j in range(k)] for i in range(2**k)]


def parse_generator(text: str) -> tuple[str, str]:
    """``'D=ABC'`` -> ``('D', 'ABC')``; ``*`` separators are ignored."""
    try:
        left, right = text.replace("*", "").replace(" ", "").split("=")
    except ValueError as exc:
        raise DesignError(f"invalid generator '{text}'; use e.g. 'D=ABC'") from exc
    if len(left) != 1 or not right or not set(left + right) <= set(LETTERS):
        raise DesignError(f"invalid generator '{text}'")
    return left, right


def _xor(a: str, b: str) -> str:
    return "".join(sorted(set(a) ^ set(b)))


def defining_relation(generators: Sequence[str]) -> list[str]:
    """All words in the defining contrast subgroup (without the identity)."""
    words = []
    for g in generators:
        left, right = parse_generator(g)
        words.append("".join(sorted(set(left + right))) if left not in right else "")
    if any(not w for w in words):
        raise DesignError("a generator cannot use its own factor on the right-hand side")
    group: set[str] = set()
    for r in range(1, len(words) + 1):
        for combo in itertools.combinations(words, r):
            w = ""
            for c in combo:
                w = _xor(w, c)
            if not w:
                raise DesignError("generators are dependent (defining relation contains identity)")
            group.add(w)
    return sorted(group, key=lambda w: (len(w), w))


def resolution(words: Sequence[str]) -> int | None:
    return min((len(w) for w in words), default=None)


def alias_structure(words: Sequence[str], k: int, max_order: int = 2) -> dict[str, list[str]]:
    """Map each effect up to ``max_order`` to the other effects it is aliased with."""
    letters = LETTERS[:k]
    effects = ["".join(c) for r in range(1, max_order + 1) for c in itertools.combinations(letters, r)]
    out: dict[str, list[str]] = {}
    seen: set[str] = set()
    for e in effects:
        if e in seen:
            continue
        al = sorted({_xor(e, w) for w in words}, key=lambda x: (len(x), x))
        out[e] = al
        seen.add(e)
        seen.update(a for a in al if len(a) <= max_order)
    return out


def fractional_factorial(k: int, generators: Sequence[str]) -> tuple[list[list[int]], dict]:
    """Return coded matrix (2^(k-p) rows) and info dict for the given generators."""
    p = len(generators)
    if p >= k:
        raise DesignError("number of generators must be smaller than the number of factors")
    letters = LETTERS[:k]
    parsed = [parse_generator(g) for g in generators]
    generated = [lhs for lhs, _ in parsed]
    if len(set(generated)) != p or any(x not in letters for x in generated):
        raise DesignError(f"generators must define {p} distinct factors among {letters}")
    base = [x for x in letters if x not in generated]
    for _, rhs in parsed:
        if any(c not in letters for c in rhs):
            raise DesignError(f"generator uses unknown factor in '{rhs}'")
        if any(c in generated for c in rhs):
            raise DesignError("generators may only be defined from base (non-generated) factors")
    base_matrix = full_factorial_2level(len(base))
    pos = {x: i for i, x in enumerate(base)}
    rows = []
    for brow in base_matrix:
        row = {x: brow[pos[x]] for x in base}
        for lhs, rhs in parsed:
            row[lhs] = math.prod(row[c] for c in rhs)
        rows.append([row[x] for x in letters])
    words = defining_relation(generators)
    info = {
        "k": k,
        "p": p,
        "runs": 2 ** (k - p),
        "generators": list(generators),
        "base_factors": base,
        "defining_relation": ["I=" + w for w in words],
        "resolution": resolution(words),
        "alias_structure": alias_structure(words, k),
    }
    return rows, info


def choose_generators(k: int, max_runs: int) -> list[str]:
    """Smallest p (largest fraction) such that 2^(k-p) <= max_runs, from the standard table."""
    for p in range(1, k - 1):
        if 2 ** (k - p) <= max_runs:
            gens = STANDARD_GENERATORS.get((k, p))
            if gens is None:
                raise DesignError(f"no built-in generators for k={k}, p={p}; supply design_options['generators']")
            return gens
    raise DesignError(f"budget of {max_runs} runs is too small for any fractional factorial of {k} factors")


def block_generators(k: int, b: int) -> tuple[list[str], list[str]]:
    """Choose ``b`` independent interaction words to confound with blocks.

    Maximises the minimum order of the confounded effects. Returns (generators, all confounded words).
    """
    letters = LETTERS[:k]
    if b < 1 or b >= k:
        raise DesignError("block count must be 2^b with 1 <= b < number of factors")
    words = ["".join(c) for r in range(2, k + 1) for c in itertools.combinations(letters, r)]
    best: tuple[int, list[str], list[str]] | None = None
    for combo in itertools.combinations(words, b):
        group: set[str] = set()
        ok = True
        for r in range(1, b + 1):
            for sub in itertools.combinations(combo, r):
                w = ""
                for s in sub:
                    w = _xor(w, s)
                if not w:
                    ok = False
                    break
                group.add(w)
            if not ok:
                break
        if not ok:
            continue
        score = min(len(w) for w in group)
        if best is None or score > best[0]:
            best = (score, list(combo), sorted(group, key=lambda w: (len(w), w)))
    if best is None:
        raise DesignError("could not find independent block generators")
    return best[1], best[2]


def block_assignment(matrix: list[list[int]], generators: Sequence[str]) -> list[int]:
    """Block index per row from the signs of the confounded interaction contrasts."""
    out = []
    for row in matrix:
        idx = 0
        for bit, g in enumerate(generators):
            sign = math.prod(row[LETTERS.index(c)] for c in g)
            idx |= (1 if sign > 0 else 0) << bit
        out.append(idx)
    return out


def central_composite(
    k: int, *, alpha: float | str = "face", center_points: int = 3, fraction_generators: Sequence[str] | None = None
) -> tuple[list[list[float]], dict]:
    """Return a CCD coded so that *all points lie within [-1, 1]* (inscribed when alpha > 1).

    ``alpha`` may be 'face' (1.0), 'rotatable' ((2^(k-p))^(1/4)), 'orthogonal', or a number >= 1.
    Because factor bounds are hard limits, factorial points are scaled by 1/alpha when alpha > 1
    so the axial points sit exactly on the bounds (a *circumscribed* CCD would leave the range).
    """
    if k < 2:
        raise DesignError("central composite design needs at least 2 continuous factors")
    if fraction_generators:
        fact, finfo = fractional_factorial(k, fraction_generators)
        nf = len(fact)
        if (finfo["resolution"] or 0) < 5:
            raise DesignError("CCD factorial portion must be resolution V or higher to estimate quadratic response surfaces")
    else:
        if k > 5:
            raise DesignError("CCD with k>5 requires design_options['generators'] for a resolution-V fraction")
        fact, finfo = full_factorial_2level(k), None
        nf = len(fact)
    nax = 2 * k
    if alpha == "face":
        a = 1.0
    elif alpha == "rotatable":
        a = nf**0.25
    elif alpha == "orthogonal":
        n_total = nf + nax + center_points
        a = ((nf * (math.sqrt(n_total) - math.sqrt(nf)) ** 2) / 4) ** 0.25
    else:
        a = float(alpha)
    if a < 1.0 or not math.isfinite(a):
        raise DesignError("alpha must be >= 1")
    scale = 1.0 / a
    rows: list[list[float]] = [[v * scale for v in r] for r in fact]
    for i in range(k):
        for s in (-1.0, 1.0):
            r = [0.0] * k
            r[i] = s
            rows.append(r)
    rows += [[0.0] * k for _ in range(center_points)]
    kinds = ["factorial"] * nf + ["axial"] * nax + ["center"] * center_points
    info = {
        "type": "central_composite",
        "k": k,
        "alpha": a,
        "alpha_spec": alpha,
        "inscribed": a > 1.0,
        "factorial_runs": nf,
        "axial_runs": nax,
        "center_points": center_points,
        "factorial_fraction": finfo,
        "point_kinds": kinds,
        "note": "Coded within [-1,1] bounds; axial points are on the factor bounds.",
    }
    return rows, info


def box_behnken(k: int, *, center_points: int = 3) -> tuple[list[list[float]], dict]:
    if k < 3 or k > 5:
        raise DesignError("Box-Behnken is supported for 3 to 5 continuous factors")
    rows: list[list[float]] = []
    for i, j in itertools.combinations(range(k), 2):
        for a, b in itertools.product((-1.0, 1.0), repeat=2):
            r = [0.0] * k
            r[i], r[j] = a, b
            rows.append(r)
    n_edge = len(rows)
    rows += [[0.0] * k for _ in range(center_points)]
    kinds = ["edge"] * n_edge + ["center"] * center_points
    return rows, {
        "type": "box_behnken",
        "k": k,
        "center_points": center_points,
        "point_kinds": kinds,
        "note": "No points at factorial corners; all points inside the cube.",
    }
