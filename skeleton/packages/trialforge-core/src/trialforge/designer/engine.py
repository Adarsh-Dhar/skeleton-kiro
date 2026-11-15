"""Reproducible experiment designs.

Methods: ``factorial``, ``ofat``, ``random``, ``fractional_factorial``,
``blocked_factorial``, ``central_composite``, ``box_behnken`` and sequential extension.

Design options (``ExperimentSpec.design_options``)
-------------------------------------------------
``replicates`` (int, default 1) - repeat every design point.
``continuous_levels`` (int >= 2, default 3) - levels per continuous factor for factorial/OFAT.
``center_points`` (int) - centre runs for fractional/CCD/BB (continuous factors only).
``generators`` (list[str]) - e.g. ``["D=ABC"]``; letters follow treatment-factor order.
``n_blocks`` / ``confound_blocks`` - synthetic blocks; see ``blocked_factorial``.
``alpha`` - CCD axial distance: 'face' (default), 'rotatable', 'orthogonal' or a number.
``min_resolution`` - reject fractional designs below this resolution.
"""

from __future__ import annotations

import itertools
import random
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from trialforge.designer import classical as cl
from trialforge.errors import DesignError
from trialforge.evaluation import evaluate_constraints, is_feasible
from trialforge.models.core import DESIGN_METHODS, DesignPlan, ExperimentResult, ExperimentSpec, Run

__all__ = ["DesignError", "ExperimentDesigner"]


class ExperimentDesigner:
    """Generate :class:`DesignPlan` objects from an :class:`ExperimentSpec`."""

    METHODS = set(DESIGN_METHODS)
    PLAN_METHODS = METHODS | {"sequential"}

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _feasible(spec: ExperimentSpec, cfg: dict[str, Any]) -> bool:
        ok = all(c.accepts(cfg[c.metric]) for c in spec.input_constraints() if c.function is None and c.metric in cfg)
        if ok and any(c.function for c in spec.input_constraints()):
            ok = is_feasible(evaluate_constraints(spec, cfg, None, kinds=("input",)))
        return ok

    @staticmethod
    def control_for(spec: ExperimentSpec) -> dict[str, Any]:
        """Control configuration for ``spec``: each factor's default, else its first candidate level."""
        out = {}
        for f in spec.factors:
            if f.default is not None:
                out[f.name] = f.default
            else:
                out[f.name] = f.candidates()[0]
        return out

    def check(self, spec: ExperimentSpec, method: str | None = None) -> list[str]:
        """Return a list of problems preventing ``method`` for ``spec`` (empty = compatible)."""
        method = method or spec.design_method
        issues: list[str] = []
        if method not in self.METHODS:
            return [f"unsupported design method '{method}'; supported: {sorted(self.METHODS)}"]
        tf = spec.treatment_factors
        opts = spec.design_options
        if opts.get("replicates", 1) < 1:
            issues.append("replicates must be >= 1")
        if method == "fractional_factorial":
            for f in tf:
                if f.factor_type != "continuous" and len(f.values) != 2 and f.factor_type == "categorical":
                    issues.append(f"categorical factor '{f.name}' needs exactly 2 levels for a 2-level design")
                if f.factor_type == "discrete" and len(f.values) < 2:
                    issues.append(f"factor '{f.name}' needs at least 2 levels")
            if len(tf) < 3:
                issues.append("fractional factorial needs at least 3 treatment factors")
            if len(tf) > 26:
                issues.append("at most 26 treatment factors are supported")
        if method in ("central_composite", "box_behnken"):
            bad = [f.name for f in tf if f.factor_type != "continuous"]
            if bad:
                issues.append(f"{method} requires continuous factors; not continuous: {bad}")
            if spec.block_factors:
                issues.append(f"{method} does not support block factors")
            if method == "box_behnken" and not 3 <= len(tf) <= 5:
                issues.append("box_behnken supports 3 to 5 factors")
            if method == "central_composite" and len(tf) < 2:
                issues.append("central_composite needs at least 2 factors")
        if method == "blocked_factorial":
            if not spec.block_factors and not opts.get("n_blocks"):
                issues.append("blocked_factorial needs a Factor(role='block') or design_options['n_blocks']")
            if spec.block_factors and opts.get("n_blocks"):
                issues.append("use either block factors or design_options['n_blocks'], not both")
        elif spec.block_factors and method not in ("factorial", "ofat", "random"):
            issues.append(
                f"block factors are only supported by blocked_factorial (and run as plain "
                f"factors for factorial/ofat/random); got '{method}'"
            )
        return issues

    # -------------------------------------------------------------- main
    def design(self, spec: ExperimentSpec, method: str | None = None) -> DesignPlan:
        """Generate a reproducible :class:`DesignPlan` using ``method`` (default ``spec.design_method``). Raises
        DesignError for incompatible methods, exceeded budgets or infeasible designs.
        """
        method = method or spec.design_method
        issues = self.check(spec, method)
        if issues:
            raise DesignError("; ".join(issues))
        rng = random.Random(spec.random_seed)
        reps = int(spec.design_options.get("replicates", 1))
        builder = {
            "factorial": self._factorial,
            "ofat": self._ofat,
            "random": self._random,
            "fractional_factorial": self._fractional,
            "blocked_factorial": self._blocked,
            "central_composite": self._ccd,
            "box_behnken": self._bb,
        }[method]
        points, info = builder(spec, rng)  # list of dicts: config, coded, block, meta
        if not points:
            raise DesignError("no feasible design configurations satisfy the declared factor constraints")
        info.setdefault("replicates", reps)
        total = len(points) * (reps if method != "random" else 1)
        if total > spec.run_budget:
            raise DesignError(
                f"{method} requires {total} runs ({len(points)} points x {info['replicates']} "
                f"replicate(s)), exceeds budget {spec.run_budget}"
            )
        expanded = []
        for rep in range(reps if method != "random" else 1):
            for p in points:
                expanded.append({**p, "replicate": rep})
        if method == "random":
            info["replicates"] = 1
        order, rand_info = self._randomise(
            expanded,
            rng,
            blocked=method == "blocked_factorial",
            randomize_blocks=bool(spec.design_options.get("randomize_blocks", False)),
        )
        runs = []
        for index, p in enumerate(order):
            rid = str(
                uuid5(
                    NAMESPACE_URL,
                    f"{spec.experiment_id}:{spec.random_seed}:{index}:"
                    f"{sorted(p['config'].items())}:{p['replicate']}:{sorted(p['block'].items())}",
                )
            )
            runs.append(
                Run(
                    run_id=rid,
                    experiment_id=spec.experiment_id,
                    configuration=p["config"],
                    order=index,
                    coded=p.get("coded", {}),
                    block=p["block"],
                    replicate=p["replicate"],
                    metadata={"standard_order": p["standard_order"], **p.get("meta", {})},
                )
            )
        control = self.control_for(spec)
        if not self._feasible(spec, control):
            control = points[0]["config"]
        return DesignPlan(
            experiment_id=spec.experiment_id,
            method=method,
            runs=runs,
            control_configuration=control,
            random_seed=spec.random_seed,
            block_variables=[f.name for f in spec.block_factors]
            + (["block"] if spec.design_options.get("n_blocks") and method == "blocked_factorial" else []),
            treatment_factors=[f.name for f in spec.treatment_factors],
            randomization=rand_info,
            design_info=info,
            metadata={
                "candidate_count": len(points),
                "randomized": True,
                "complete": bool(info.get("complete", method not in ("random",))),
            },
        )

    @staticmethod
    def _randomise(points: list[dict], rng: random.Random, *, blocked: bool, randomize_blocks: bool):
        for i, p in enumerate(points):
            p.setdefault("standard_order", i)
        before = [p["standard_order"] for p in points]
        if blocked:
            keys: list[tuple] = []
            for p in points:
                key = tuple(sorted(p["block"].items()))
                if key not in keys:
                    keys.append(key)
            if randomize_blocks:
                rng.shuffle(keys)
            out = []
            for key in keys:
                grp = [p for p in points if tuple(sorted(p["block"].items())) == key]
                rng.shuffle(grp)
                out.extend(grp)
            scheme = "restricted: runs shuffled within each block" + (
                "; block order shuffled" if randomize_blocks else "; block order fixed"
            )
        else:
            out = list(points)
            rng.shuffle(out)
            scheme = "complete randomisation of run order"
        return out, {
            "scheme": scheme,
            "generator": "random.Random",
            "standard_order_before": before,
            "run_order_standard_indices": [p["standard_order"] for p in out],
        }

    # ---------------------------------------------------------- designs
    def _levels(self, spec: ExperimentSpec, factors=None) -> list[list[Any]]:
        n = int(spec.design_options.get("continuous_levels", 3))
        return [f.candidates(n) for f in (factors or spec.factors)]

    def _coded(self, spec: ExperimentSpec, cfg: dict) -> dict[str, float]:
        return {
            f.name: f.encode(cfg[f.name])
            for f in spec.factors
            if f.is_numeric and cfg[f.name] is not None and f.low_high()[0] != f.low_high()[1]
        }

    def _factorial(self, spec: ExperimentSpec, rng: random.Random):
        names = [f.name for f in spec.factors]
        levels = self._levels(spec)
        full = [dict(zip(names, v)) for v in itertools.product(*levels)]
        feas = [c for c in full if self._feasible(spec, c)]
        pts = [{"config": c, "coded": self._coded(spec, c), "block": {}} for c in feas]
        n_center = int(spec.design_options.get("center_points", 0))
        info = {
            "type": "full_factorial",
            "levels": {n: list(lv) for n, lv in zip(names, levels)},
            "full_grid_size": len(full),
            "constraint_filtered": len(full) - len(feas),
            "complete": len(feas) == len(full),
            "limitations": []
            if len(feas) == len(full)
            else ["Input constraints removed some factor combinations; this is NOT a complete factorial."],
        }
        if n_center:
            cont = [f for f in spec.factors if f.factor_type == "continuous"]
            if cont:
                mid = {
                    f.name: (f.lower + f.upper) / 2 if f.factor_type == "continuous" else f.candidates()[0] for f in spec.factors
                }
                if self._feasible(spec, mid):
                    pts += [
                        {"config": dict(mid), "coded": self._coded(spec, mid), "block": {}, "meta": {"center_point": True}}
                        for _ in range(n_center)
                    ]
                    info["center_points"] = n_center
        return pts, info

    def _ofat(self, spec: ExperimentSpec, rng: random.Random):
        control = self.control_for(spec)
        feas_any = None
        if not self._feasible(spec, control):
            full = [dict(zip([f.name for f in spec.factors], v)) for v in itertools.product(*self._levels(spec))]
            feas_any = next((c for c in full if self._feasible(spec, c)), None)
            if feas_any is None:
                return [], {}
            control = feas_any
        configs = [dict(control)]
        levels = self._levels(spec)
        for f, lv in zip(spec.factors, levels):
            for level in lv:
                if level == control[f.name]:
                    continue
                cand = {**control, f.name: level}
                if self._feasible(spec, cand):
                    configs.append(cand)
        unique = list({tuple(sorted(c.items())): c for c in configs}.values())
        pts = [{"config": c, "coded": self._coded(spec, c), "block": {}, "meta": {"ofat_control": c == control}} for c in unique]
        return pts, {"type": "ofat", "control": control, "complete": True}

    def _random(self, spec: ExperimentSpec, rng: random.Random):
        n = int(spec.design_options.get("n_runs", spec.run_budget))
        if n > spec.run_budget:
            raise DesignError(f"random design n_runs {n} exceeds budget {spec.run_budget}")
        levels = self._levels(spec)
        cfgs, attempts = [], max(100, n * 100)
        for _ in range(attempts):
            c = {
                f.name: (rng.uniform(f.lower, f.upper) if f.factor_type == "continuous" else rng.choice(lv))
                for f, lv in zip(spec.factors, levels)
            }
            if self._feasible(spec, c):
                cfgs.append(c)
                if len(cfgs) == n:
                    break
        if not cfgs:
            raise DesignError("random design could not find a feasible configuration")
        if len(cfgs) < n:
            raise DesignError(f"random design found {len(cfgs)} feasible runs after {attempts} attempts; budget is {n}")
        return (
            [{"config": c, "coded": self._coded(spec, c), "block": {}} for c in cfgs],
            {"type": "random", "complete": False, "sampling": "uniform over feasible region (rejection)"},
        )

    # two-level machinery ---------------------------------------------------
    def _two_level_cfg(self, spec: ExperimentSpec, row: list[float], factors) -> dict[str, Any]:
        cfg: dict[str, Any] = {}
        for f, c in zip(factors, row):
            if f.factor_type == "continuous":
                cfg[f.name] = f.decode(c)
            else:
                low, high = f.low_high()
                cfg[f.name] = low if c < 0 else high
        return cfg

    def _fill_other(self, spec: ExperimentSpec, cfg: dict) -> dict:
        for f in spec.factors:
            if f.name not in cfg:
                cfg[f.name] = f.default if f.default is not None else f.candidates()[0]
        return cfg

    def _structured_points(self, spec, rows, factors, metas=None, name="design"):
        pts = []
        dropped = 0
        for i, row in enumerate(rows):
            cfg = self._fill_other(spec, self._two_level_cfg(spec, row, factors))
            if not self._feasible(spec, cfg):
                dropped += 1
                continue
            pts.append(
                {
                    "config": cfg,
                    "coded": {f.name: float(c) for f, c in zip(factors, row)},
                    "block": {},
                    "meta": metas[i] if metas else {},
                }
            )
        if dropped:
            raise DesignError(
                f"{dropped} {name} design point(s) violate input constraints; structured designs "
                "cannot be filtered without destroying their balance/alias structure. "
                "Adjust factor ranges or choose another method."
            )
        return pts

    def _fractional(self, spec: ExperimentSpec, rng: random.Random):
        tf = spec.treatment_factors
        k = len(tf)
        reps = int(spec.design_options.get("replicates", 1))
        n_center = int(spec.design_options.get("center_points", 0))
        if n_center and not any(f.factor_type == "continuous" for f in tf):
            raise DesignError("center_points require at least one continuous factor")
        gens = spec.design_options.get("generators")
        if gens is None:
            max_runs = (spec.run_budget - n_center) // reps
            if 2**k <= max_runs:
                gens = []
            else:
                gens = cl.choose_generators(k, max_runs)
        if gens:
            rows, finfo = cl.fractional_factorial(k, gens)
        else:
            rows = cl.full_factorial_2level(k)
            finfo = {
                "k": k,
                "p": 0,
                "runs": 2**k,
                "generators": [],
                "base_factors": list(cl.LETTERS[:k]),
                "defining_relation": [],
                "resolution": None,
                "alias_structure": {},
            }
        min_res = spec.design_options.get("min_resolution")
        if min_res and finfo["resolution"] is not None and finfo["resolution"] < min_res:
            raise DesignError(f"design resolution {finfo['resolution']} is below required {min_res}")
        rows_f: list[list[float]] = [[float(v) for v in r] for r in rows]
        metas: list[dict] = [{} for _ in rows_f]
        if n_center:
            rows_f += [[0.0 if f.factor_type == "continuous" else -1.0 for f in tf] for _ in range(n_center)]
            metas += [{"center_point": True} for _ in range(n_center)]
        pts = self._structured_points(spec, rows_f, tf, metas, "fractional factorial")
        # centre points only mean something for continuous factors; non-continuous held at low level
        finfo.update(
            {
                "type": "fractional_factorial",
                "factor_letters": {f.name: cl.LETTERS[i] for i, f in enumerate(tf)},
                "coding": "low=-1, high=+1; continuous factors decoded linearly onto [lower, upper]",
                "complete": finfo["p"] == 0,
                "center_points": n_center,
                "limitations": (
                    [] if finfo["p"] == 0 else [f"Effects are aliased (resolution {finfo['resolution']}); see alias_structure."]
                ),
            }
        )
        return pts, finfo

    def _blocked(self, spec: ExperimentSpec, rng: random.Random):
        tf = spec.treatment_factors
        names = [f.name for f in tf]
        t_levels = self._levels(spec, tf)
        treat = [dict(zip(names, v)) for v in itertools.product(*t_levels)]
        if spec.block_factors:
            b_levels = [f.candidates() for f in spec.block_factors]
            blocks = [dict(zip([f.name for f in spec.block_factors], v)) for v in itertools.product(*b_levels)]
            pts, dropped = [], 0
            for b in blocks:
                for t in treat:
                    cfg = self._fill_other(spec, {**t, **b})
                    if not self._feasible(spec, cfg):
                        dropped += 1
                        continue
                    pts.append({"config": cfg, "coded": self._coded(spec, cfg), "block": dict(b)})
            info = {
                "type": "randomized_complete_block",
                "block_variables": [f.name for f in spec.block_factors],
                "n_blocks": len(blocks),
                "treatment_factors": names,
                "runs_per_block": len(treat),
                "constraint_filtered": dropped,
                "complete": dropped == 0,
                "randomization": "treatment order randomised within each block",
            }
            if dropped:
                info["limitations"] = ["Input constraints removed runs: blocks are no longer complete."]
            return pts, info
        n_blocks = int(spec.design_options["n_blocks"])
        two_level = all(
            f.factor_type != "continuous"
            and len(f.values) == 2
            or f.factor_type == "continuous"
            and spec.design_options.get("continuous_levels", 3) == 2
            for f in tf
        )
        if spec.design_options.get("confound_blocks"):
            b = n_blocks.bit_length() - 1
            if 2**b != n_blocks:
                raise DesignError("confounded blocking requires n_blocks to be a power of 2")
            if not two_level:
                raise DesignError(
                    "confounded blocking requires 2-level treatment factors "
                    "(set design_options['continuous_levels']=2 for continuous factors)"
                )
            k = len(tf)
            rows = cl.full_factorial_2level(k)
            gens, confounded = cl.block_generators(k, b)
            assign = cl.block_assignment(rows, gens)
            pts = []
            for i, (row, blk) in enumerate(zip(rows, assign)):
                cfg = self._fill_other(
                    spec,
                    {
                        f.name: (
                            f.decode(c) if f.factor_type == "continuous" else (f.low_high()[0] if c < 0 else f.low_high()[1])
                        )
                        for f, c in zip(tf, row)
                    },
                )
                if not self._feasible(spec, cfg):
                    raise DesignError("confounded blocked design points violate input constraints")
                pts.append({"config": cfg, "coded": {f.name: float(c) for f, c in zip(tf, row)}, "block": {"block": blk}})
            letters = {f.name: cl.LETTERS[i] for i, f in enumerate(tf)}
            return pts, {
                "type": "confounded_blocks",
                "block_variables": ["block"],
                "n_blocks": n_blocks,
                "block_generators": gens,
                "confounded_effects": confounded,
                "factor_letters": letters,
                "complete": True,
                "limitations": ["Confounded effects cannot be separated from block differences: " + ", ".join(confounded)],
            }
        pts = []
        for blk in range(n_blocks):
            for t in treat:
                cfg = self._fill_other(spec, dict(t))
                if self._feasible(spec, cfg):
                    pts.append({"config": cfg, "coded": self._coded(spec, cfg), "block": {"block": blk}})
        return pts, {
            "type": "replicated_complete_blocks",
            "block_variables": ["block"],
            "n_blocks": n_blocks,
            "runs_per_block": len(treat),
            "complete": len(pts) == n_blocks * len(treat),
        }

    def _ccd(self, spec: ExperimentSpec, rng: random.Random):
        tf = spec.treatment_factors
        opts = spec.design_options
        rows, info = cl.central_composite(
            len(tf),
            alpha=opts.get("alpha", "face"),
            center_points=int(opts.get("center_points", 3)),
            fraction_generators=opts.get("generators"),
        )
        metas = [{"point_kind": k} for k in info["point_kinds"]]
        pts = self._structured_points(spec, rows, tf, metas, "central composite")
        info.update({"complete": True, "factor_letters": {f.name: cl.LETTERS[i] for i, f in enumerate(tf)}})
        return pts, info

    def _bb(self, spec: ExperimentSpec, rng: random.Random):
        tf = spec.treatment_factors
        rows, info = cl.box_behnken(len(tf), center_points=int(spec.design_options.get("center_points", 3)))
        metas = [{"point_kind": k} for k in info["point_kinds"]]
        pts = self._structured_points(spec, rows, tf, metas, "Box-Behnken")
        info.update({"complete": True})
        return pts, info

    # ------------------------------------------------------- sequential
    def sequential(
        self,
        spec: ExperimentSpec,
        result: ExperimentResult | None,
        configurations: list[dict[str, Any]],
        *,
        iteration: int = 1,
        source: str = "optimizer",
    ) -> DesignPlan:
        """Create a plan of *new* runs appended after the existing ones.

        The run budget counts existing runs; exceeding it raises DesignError. Each configuration is
        validated against factor domains and input constraints.
        """
        existing = len(result.runs) if result else 0
        if existing + len(configurations) > spec.run_budget:
            raise DesignError(
                f"sequential extension of {len(configurations)} run(s) exceeds remaining budget {spec.run_budget - existing}"
            )
        runs = []
        factors = {f.name: f for f in spec.factors}
        for i, cfg in enumerate(configurations):
            if set(cfg) != set(factors):
                raise DesignError(f"configuration must define exactly: {sorted(factors)}")
            if not all(factors[n].accepts(v) for n, v in cfg.items()) or not self._feasible(spec, cfg):
                raise DesignError(f"configuration {cfg} violates factor domain or input constraints")
            order = existing + i
            rid = str(uuid5(NAMESPACE_URL, f"{spec.experiment_id}:seq:{iteration}:{order}:{sorted(cfg.items())}"))
            runs.append(
                Run(
                    run_id=rid,
                    experiment_id=spec.experiment_id,
                    configuration=dict(cfg),
                    order=order,
                    coded=self._coded(spec, cfg),
                    metadata={"sequential_iteration": iteration, "source": source},
                )
            )
        control = result.plan.control_configuration if result else self.control_for(spec)
        return DesignPlan(
            experiment_id=spec.experiment_id,
            method="sequential",
            runs=runs,
            control_configuration=control,
            random_seed=spec.random_seed,
            treatment_factors=[f.name for f in spec.treatment_factors],
            randomization={"scheme": "none: order determined by optimiser"},
            design_info={
                "type": "sequential",
                "iteration": iteration,
                "source": source,
                "remaining_budget_after": spec.run_budget - existing - len(runs),
            },
        )
