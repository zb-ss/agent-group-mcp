from __future__ import annotations

import unittest

from agent_bus.dispatch import routing
from agent_bus.dispatch.routing import Signals, choose


def cfg(**extra):
    base = {
        "enabled": True,
        "clients": {
            "alpha": {"weight": 3, "types": {}, "per_complexity": 0, "models": []},
            "beta": {"weight": 2, "types": {}, "per_complexity": 0, "models": []},
        },
        "complexity": {"max": 5, "signals": []},
        "overrides": [],
    }
    base.update(extra)
    return base


def sig(**kw):
    values = dict(type="refinement", repo="org/repo", repo_id="org/repo", title="Fix it")
    values.update(kw)
    return Signals(**values)


def run(config, signals=None, available=lambda c: True, **kw):
    return choose(
        config, signals or sig(), static_client="beta", static_model="m-static",
        available=available, **kw,
    )


class RoutingTest(unittest.TestCase):
    def test_disabled_gives_static(self):
        route = run(cfg(enabled=False))
        self.assertEqual((route.client, route.model, route.by), ("beta", "m-static", "static"))

    def test_override_by_repo_and_type_first_match_wins(self):
        config = cfg(overrides=[
            {"repo": "other/*", "client": "beta"},
            {"repo": "org/*", "type": "follow-up", "client": "beta"},
            {"repo": "org/*", "client": "alpha", "model": "m-x"},
            {"client": "beta"},
        ])
        route = run(config)
        self.assertEqual((route.client, route.model, route.by), ("alpha", "m-x", "override"))
        self.assertEqual(run(config, sig(type="follow-up")).client, "beta")

    def test_override_uses_repo_when_id_missing(self):
        config = cfg(overrides=[{"repo": "org/repo", "client": "beta"}])
        self.assertEqual(run(config, sig(repo_id=None)).by, "override")
        self.assertEqual(run(config, sig(repo_id=None, repo="x")).by, "policy")

    def test_override_model_absent_or_null_uses_thresholds(self):
        models = [{"min_complexity": 0, "model": "m-low"}]
        config = cfg(overrides=[{"client": "alpha", "model": None}])
        config["clients"]["alpha"]["models"] = models
        self.assertEqual(run(config).model, "m-low")

    def test_scoring_weights_types_and_complexity(self):
        self.assertEqual(run(cfg()).client, "alpha")
        config = cfg()
        config["clients"]["beta"]["types"] = {"refinement": 5}
        self.assertEqual(run(config).client, "beta")
        config = cfg(complexity={"max": 5, "signals": [{"score": 3}]})
        config["clients"]["beta"]["per_complexity"] = 1
        route = run(config)
        self.assertEqual(route.complexity, 3)
        self.assertEqual(route.scores, {"alpha": 3.0, "beta": 5.0})
        self.assertEqual(route.client, "beta")
        self.assertEqual(route.by, "policy")

    def test_unavailable_dropped_and_all_unavailable_static(self):
        self.assertEqual(run(cfg(), available=lambda c: c != "alpha").client, "beta")
        route = run(cfg(), available=lambda c: False)
        self.assertEqual((route.client, route.by), ("beta", "static"))

    def test_tie_goes_to_earlier_key(self):
        config = cfg()
        config["clients"]["beta"]["weight"] = 3
        self.assertEqual(run(config).client, "alpha")

    def test_quota_changes_winner(self):
        route = run(cfg(), quota=lambda c: 0.1 if c == "alpha" else 1.0)
        self.assertEqual(route.client, "beta")
        self.assertEqual(routing.no_quota("alpha"), 1.0)

    def test_complexity_signals_and_clamping(self):
        config = cfg(complexity={"max": 4, "signals": [
            {"type": ["refinement"], "score": 1},
            {"title": ["(?i)migrat", "zzz"], "score": 2},
            {"section": ["^Big"], "score": 2},
            {"type": ["other"], "score": 9},
        ]})
        self.assertEqual(routing.complexity(config, sig()), 1)
        self.assertEqual(routing.complexity(config, sig(title="Migrate db")), 3)
        both = sig(title="Migrate", sections=("Big stuff",))
        self.assertEqual(routing.complexity(config, both), 4)
        neg = cfg(complexity={"max": 4, "signals": [{"score": -3}]})
        self.assertEqual(routing.complexity(neg, sig()), 0)

    def test_model_thresholds(self):
        config = cfg()
        config["clients"]["alpha"]["models"] = [
            {"min_complexity": 3, "model": "m-high"},
            {"min_complexity": 0, "model": "m-low"},
            {"min_complexity": 5, "model": None},
        ]
        self.assertEqual(routing.model_for(config, "alpha", 0, "fb"), "m-low")
        self.assertEqual(routing.model_for(config, "alpha", 4, "fb"), "m-high")
        self.assertIsNone(routing.model_for(config, "alpha", 5, "fb"))
        self.assertEqual(routing.model_for(config, "beta", 5, "fb"), "fb")

    def test_static_model_kept_when_static_client_wins(self):
        config = cfg()
        config["clients"]["beta"]["weight"] = 9
        self.assertEqual(run(config).model, "m-static")
        self.assertIsNone(run(cfg()).model)

    def test_to_dict(self):
        self.assertEqual(run(cfg()).to_dict()["by"], "policy")


if __name__ == "__main__":
    unittest.main()
