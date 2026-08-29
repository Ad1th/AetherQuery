from aqp_eval.policies.base import Policy
from aqp_eval.policies.sampling import run_sample


class GeometricPolicy(Policy):
    name = "geometric"

    def __init__(self, fractions=None):
        self.fractions = fractions or [
            0.01,
            0.02,
            0.04,
            0.08,
            0.16,
            0.32,
        ]

    def run(
        self,
        query,
        database,
        target=None,
        seed=42,
    ):
        last_result = None

        for iteration, fraction in enumerate(
            self.fractions,
            start=1,
        ):
            last_result = run_sample(
                query,
                database,
                fraction,
                seed,
            )

            if target is None:
                break

            # Baseline does not have a statistical estimator
            # for stopping, so continue until the maximum ladder
            # fraction. Error is evaluated by the harness.
            if fraction >= 0.32:
                break

        return {
            **last_result,
            "policy": self.name,
            "iterations": iteration,
            "stop_reason": "geometric_max_fraction",
            "target": target,
            "error": None,
        }
