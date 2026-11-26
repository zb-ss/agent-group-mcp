from __future__ import annotations

import json
import unittest

from agent_bus.dispatch import decision_model as dm
from agent_bus.dispatch.routing import Route, Signals

CFG = {
    "enabled": True, "mode": "shadow", "endpoint": "http://localhost:11434/v1/x",
    "model": "example-decision-model", "timeout_seconds": 5, "max_state_chars": 2000,
}
SIGNALS = Signals("refinement", "org/repo", "org/repo", "Fix\tthe\nthing", ("Todo",))
ROUTING = {"clients": {"alpha": {"models": [{"min_complexity": 0, "model": "m1"}]}, "beta": {}}}


def reply(choice="alpha", score=2.6):
    return json.dumps({"answers": {
        "client": {"type": "choice", "choice": choice},
        "complexity": {"type": "score", "score": score},
    }}).encode()


class Recorder:
    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, json.loads(body), timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def consult(post, cfg=CFG, clients=("alpha", "beta")):
    return dm.consult(cfg, SIGNALS, clients, complexity_max=10, http_post=post)


class EndpointPolicyTest(unittest.TestCase):
    def test_remote_endpoint_refused_without_a_request(self):
        for cfg in (
            {**CFG, "endpoint": "https://models.example.test/x"},
            {**CFG, "endpoint": "http://models.example.test/x", "allow_remote": True},
        ):
            post = Recorder(reply())
            self.assertIsNotNone(consult(post, cfg=cfg).error)
            self.assertEqual(post.calls, [])

    def test_https_remote_allowed_when_enabled(self):
        post = Recorder(reply())
        cfg = {**CFG, "endpoint": "https://models.example.test/x", "allow_remote": True}
        self.assertIsNone(consult(post, cfg=cfg).error)
        self.assertEqual(len(post.calls), 1)


class ConsultTest(unittest.TestCase):
    def test_valid_answer_and_body(self):
        post = Recorder(reply())
        answer = consult(post)
        self.assertEqual((answer.client, answer.complexity, answer.error), ("alpha", 5, None))
        url, body, timeout = post.calls[0]
        self.assertEqual(url, CFG["endpoint"])
        self.assertEqual(timeout, 5)
        self.assertEqual(body["model"], "example-decision-model")
        self.assertEqual(list(body["questions"]["client"]["criteria"]), ["alpha", "beta"])
        self.assertEqual(len(body["questions"]["complexity"]["criteria"]), 6)
        self.assertNotIn("\t", body["state"])

    def test_failures_become_error_answers(self):
        for result in (
            dm.DecisionModelError("HTTP 500 from host"), TimeoutError("slow"),
            b"not json", b"{}", reply(choice="gamma"),
        ):
            answer = consult(Recorder(result))
            self.assertIsNotNone(answer.error, result)
        self.assertIsNone(consult(Recorder(reply(choice="gamma"))).client)

    def test_state_truncated(self):
        post = Recorder(reply())
        consult(post, cfg={**CFG, "max_state_chars": 20})
        self.assertEqual(len(post.calls[0][1]["state"]), 20)

    def test_option_limits_make_no_call(self):
        post = Recorder(reply())
        self.assertIsNotNone(consult(post, clients=()).error)
        self.assertIsNotNone(consult(post, clients=tuple(f"c{i}" for i in range(27))).error)
        self.assertEqual(post.calls, [])

    def test_to_dict(self):
        self.assertEqual(dm.ShadowAnswer("a", 1).to_dict()["client"], "a")


class ApplyTest(unittest.TestCase):
    answer = dm.ShadowAnswer("beta", 4)

    def apply(self, mode="active", by="policy", answer=None, available=lambda c: True):
        route = Route("alpha", "m1", by, 1)
        return dm.apply(
            {**CFG, "mode": mode}, route, answer or self.answer,
            routing_cfg=ROUTING, available=available,
        )

    def test_shadow_unchanged(self):
        self.assertEqual(self.apply(mode="shadow").client, "alpha")

    def test_active_switches_for_policy_and_static(self):
        for by in ("policy", "static"):
            route = self.apply(by=by)
            self.assertEqual((route.client, route.by), ("beta", "decision_model"))

    def test_active_never_over_override_or_dispatcher(self):
        for by in ("override", "dispatcher"):
            self.assertEqual(self.apply(by=by).client, "alpha")

    def test_unknown_or_unavailable_client_ignored(self):
        self.assertEqual(self.apply(answer=dm.ShadowAnswer("gamma", 1)).client, "alpha")
        self.assertEqual(self.apply(answer=dm.ShadowAnswer(None, 1)).client, "alpha")
        self.assertEqual(self.apply(available=lambda c: False).client, "alpha")

    def test_model_follows_complexity(self):
        route = self.apply(answer=dm.ShadowAnswer("alpha", 3))
        self.assertEqual(route.model, "m1")


if __name__ == "__main__":
    unittest.main()
