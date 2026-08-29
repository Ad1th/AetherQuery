from aqp_eval.policies.base import Policy
from aqp_eval.policies.sampling import run_sample


class OnlineAggPolicy(Policy):
    name = "online_agg"

    def __init__(self, fraction=0.10):
        self.fraction = fraction

    def run(
        self,
        query,
        database,
        target=None,
        seed=42,
    ):
        result = run_sample(
            query,
            database,
            self.fraction,
            seed,
        )

        return {
            **result,
            "policy": self.name,
            "iterations": 1,
            "stop_reason": "online_single_pass",
            "target": target,
            "error": None,
        }
