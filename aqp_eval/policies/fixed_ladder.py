from aqp_eval.policies.base import Policy
from aqp_eval.policies.sampling import run_sample


class FixedLadderPolicy(Policy):
    name = "fixed_ladder"

    def __init__(self, fractions=None):
        self.fractions = fractions or [
            0.01,
            0.05,
            0.10,
            0.20,
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

        return {
            **last_result,
            "policy": self.name,
            "iterations": iteration,
            "stop_reason": "fixed_ladder",
            "target": target,
            "error": None,
        }
