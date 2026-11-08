"""The decision model's providers (autopilot/decision.py): Jev by default, a Claude model on request."""

import json
import types

import pytest
from typesafe_sdk import Choice, Noul, Score

from autopilot import cli, decision, runtime, sandbox, telemetry


class FakeSDK:
    """Stands in for anthropic.Anthropic: records each request and answers with the given JSON."""

    def __init__(self, answer, stop_reason="end_turn", model="claude-haiku-5-5"):
        self.answer, self.stop_reason, self.model = answer, stop_reason, model
        self.requests, self.timeouts = [], []
        self.messages = self

    def with_options(self, timeout=None):
        self.timeouts.append(timeout)
        return self

    def create(self, **req):
        self.requests.append(req)
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        r = types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], model=self.model,
                                  stop_reason=self.stop_reason,
                                  usage=types.SimpleNamespace(input_tokens=321, output_tokens=17))
        r.model_dump = lambda mode=None: {"model": self.model, "content": [{"type": "text", "text": text}]}
        return r


def pick(options):
    return {"pick": Choice(instructions="Choose the option to do now.", criteria=options)}


# -- choosing the provider ---------------------------------------------------------------------------------------------

def test_jev_is_the_default(monkeypatch):
    monkeypatch.delenv(decision.PROVIDER_ENV, raising=False)
    assert decision.provider() == "jev"
    asked = []
    monkeypatch.setattr(runtime, "jev_client", lambda warm=True: asked.append(warm) or "jev")
    assert decision.client() == "jev" and decision.client(warm=False) == "jev"
    assert asked == [True, False]


def test_the_provider_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(decision.PROVIDER_ENV, " Anthropic ")
    assert decision.provider() == "anthropic"
    monkeypatch.setenv(decision.PROVIDER_ENV, "cloudflare")
    with pytest.raises(ValueError, match="jev, anthropic, clef, openai"):
        decision.provider()


def test_the_anthropic_provider_makes_an_anthropic_decider(monkeypatch):
    monkeypatch.setenv(decision.PROVIDER_ENV, "anthropic")
    monkeypatch.setenv(decision.MODEL_ENV, "claude-sonnet-5-5")
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", lambda **k: FakeSDK({}))
    c = decision.client()
    assert isinstance(c, decision.AnthropicDecider) and c.model == "claude-sonnet-5-5"


def test_the_key_check_names_the_providers_key(monkeypatch):
    monkeypatch.setattr(cli.aura, "enabled", lambda: False)
    for k in ("TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv(decision.PROVIDER_ENV, raising=False)
    assert "TYPESAFE_API_KEY" in cli.decision_key_missing()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert "TYPESAFE_API_KEY" in cli.decision_key_missing()  # (Jev's key is the one Jev needs)
    monkeypatch.setenv(decision.PROVIDER_ENV, "anthropic")
    assert cli.decision_key_missing() is None
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert "ANTHROPIC_API_KEY" in cli.decision_key_missing()
    monkeypatch.setattr(cli.aura, "enabled", lambda: True)
    assert cli.decision_key_missing() is None


def test_clef_needs_its_token_and_account_even_through_aura(monkeypatch):
    for k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv(decision.PROVIDER_ENV, "clef")
    monkeypatch.setattr(cli.aura, "enabled", lambda: True)
    why = cli.decision_key_missing()
    assert "CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID" in why and "relay" in why
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    assert decision.missing_keys() == ["CLOUDFLARE_ACCOUNT_ID"]
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    assert cli.decision_key_missing() is None
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert decision.missing_keys("openai", aura=True) == ["OPENAI_API_KEY"]
    assert decision.missing_keys("anthropic", aura=True) == []


# -- the Anthropic decider -------------------------------------------------------------------------------------------

def test_a_choice_gets_probabilities_over_every_option():
    sdk = FakeSDK({"pick": {"attack": 3, "flee": 1, "heal": 0}})
    r = decision.AnthropicDecider(sdk=sdk).system_one(state={"hp": 10}, questions=pick(
        {"attack": "Hit the nearest enemy", "flee": "Run to cover", "heal": "Drink a potion"}))
    a = r.choices["pick"]
    assert a.choice == "attack" and a.probabilities == {"attack": 0.75, "flee": 0.25, "heal": 0.0}
    assert a.confidence == 0.75
    assert r.usage.input_tokens == 321 and r.usage.output_tokens == 17 and r.model == "claude-haiku-5-5"
    assert r.raw["model"] == "claude-haiku-5-5"


def test_the_request_is_a_json_schema_over_the_options_and_shows_the_state():
    sdk = FakeSDK({"pick": {"a": 1, "b": 0}, "hurt": 0.2, "danger": {"0": 0.5, "1": 0.5}})
    qs = pick({"a": "Do A", "b": None})
    qs["hurt"] = Noul(instructions="Is the player hurt?", criteria={"true": "Health under half"})
    qs["danger"] = Score(instructions="How dangerous is it?", criteria=["Safe", "Deadly"])
    decision.AnthropicDecider(sdk=sdk).system_one(state={"hp": 3}, questions=qs)
    req = sdk.requests[0]
    assert req["model"] == "claude-haiku-5-5" and req["thinking"] == {"type": "disabled"}
    schema = req["output_config"]["format"]["schema"]
    assert schema["required"] == ["pick", "hurt", "danger"] and schema["additionalProperties"] is False
    assert schema["properties"]["pick"]["required"] == ["a", "b"]
    assert schema["properties"]["hurt"] == {"type": "number"}
    assert schema["properties"]["danger"]["required"] == ["0", "1"]
    text = req["messages"][0]["content"]
    assert '"hp": 3' in text and "- a: Do A" in text and "- b: b" in text and "True means: Health under half" in text
    assert "1: Deadly" in text


def test_a_larger_model_keeps_its_thinking_setting():
    sdk = FakeSDK({"pick": {"a": 1}})
    decision.AnthropicDecider(model="claude-opus-5-5", sdk=sdk).system_one(state={}, questions=pick({"a": "A"}))
    assert "thinking" not in sdk.requests[0]  # (Opus 5.5 refuses thinking disabled: low effort only)
    assert sdk.requests[0]["output_config"]["effort"] == "low"


def test_nouls_and_scores_are_typed_like_jevs():
    sdk = FakeSDK({"hurt": 1.4, "danger": {"0": 1, "1": 1, "2": 2}})
    qs = {"hurt": Noul(instructions="Hurt?"), "danger": Score(instructions="Danger?", criteria=["lo", "mid", "hi"])}
    r = decision.AnthropicDecider(sdk=sdk).system_one(state="a str state", questions=qs)
    assert r.nouls["hurt"].noul == 1.0  # (clamped to 0..1)
    s = r.scores["danger"]
    assert s.probabilities == {"0": 0.25, "1": 0.25, "2": 0.5} and s.score == 1.25 and s.confidence == 0.5


def test_bad_numbers_fall_back_to_equal_probabilities():
    sdk = FakeSDK({"pick": {"a": -1, "b": "x"}})
    r = decision.AnthropicDecider(sdk=sdk).system_one(state={}, questions=pick({"a": "A", "b": "B"}))
    assert r.choices["pick"].probabilities == {"a": 0.5, "b": 0.5}


def test_a_refusal_or_bad_json_raises():
    with pytest.raises(RuntimeError, match="refused"):
        refusing = decision.AnthropicDecider(sdk=FakeSDK({}, stop_reason="refusal"))
        refusing.system_one(state={}, questions=pick({"a": "A"}))
    with pytest.raises(RuntimeError, match="not JSON"):
        decision.AnthropicDecider(sdk=FakeSDK("nope")).system_one(state={}, questions=pick({"a": "A"}))


def test_classify_options_are_taken(no_jev_kinds):
    """cli.classify passes Jev's timeout= and retry=: the decider uses the timeout and ignores the retry."""
    sdk = FakeSDK({"kind": {"verify": 0, "goal": 0, "explore": 0, "test": 1}})
    classify = no_jev_kinds  # (the real cli.classify: tests/conftest.py)
    assert classify("Reach the test chamber", client=decision.AnthropicDecider(sdk=sdk)) == ("explore", True)
    assert sdk.timeouts == [cli.KIND_TIMEOUT]


# -- the rest of Autopilot takes it as it takes Jev ------------------------------------------------------------------

def test_counting_and_the_runtime_limit_pass_through():
    u = telemetry.Usage()
    c = telemetry.CountingJev(decision.AnthropicDecider(sdk=FakeSDK({"pick": {"a": 1}})), u)
    c.system_one(state={}, questions=pick({"a": "A"}))
    assert u.totals()["decision"]["input"] == 321
    assert c.answer_timeout == decision.ANTHROPIC_TIMEOUT


def test_the_sandbox_parent_serves_its_answers():
    """The sandbox's parent asks for the child (sandbox.Sandbox._jev) and sends back what RemoteJev reads."""
    sent = []
    parent = types.SimpleNamespace(client=decision.AnthropicDecider(sdk=FakeSDK({"pick": {"a": 1, "b": 3}})),
                                   _write=sent.append)
    dumps = {k: q.model_dump(mode="json", exclude_none=True) for k, q in pick({"a": "A", "b": "B"}).items()}
    sandbox.Sandbox._jev(parent, {"rid": 1, "state": {}, "questions": dumps})
    res = sent[0]["result"]
    assert sent[0]["ok"] and res["choices"]["pick"]["choice"] == "b"
    assert res["choices"]["pick"]["probabilities"] == {"a": 0.25, "b": 0.75}


# -- the decision APIs over HTTP --------------------------------------------------------------------------------------

class FakeSession:
    """Stands in for requests.Session: records each post and answers with the given JSON and status."""

    def __init__(self, body, status=200):
        self.body, self.status, self.posts = body, status, []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        body = self.body
        return types.SimpleNamespace(status_code=self.status, json=lambda: body, text=str(body))


CLEF_ANSWER = {"model": "clef", "usage": {"input_tokens": 90, "output_tokens": 3}, "answers": {
    "pick": {"type": "choice", "choice": "b", "confidence": 0.8, "probabilities": {"a": 0.2, "b": 0.8}},
    "hurt": {"type": "noul", "noul": 0.9},
    "danger": {"type": "score", "score": 0.4, "confidence": 0.6, "legend": {}, "probabilities": {"0": 0.6, "1": 0.4}}}}


def questions3():
    qs = pick({"a": "Do A", "b": "Do B"})
    qs["hurt"] = Noul(instructions="Hurt?", criteria={"true": "Health under half"})
    qs["danger"] = Score(instructions="Danger?", criteria=["Safe", "Deadly"])
    return qs


def test_clef_sends_jevs_request_to_workers_ai(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.delenv(decision.MODEL_ENV, raising=False)
    s = FakeSession({"success": True, "errors": [], "result": CLEF_ANSWER})  # (Cloudflare's envelope)
    r = decision.ClefDecider(session=s).system_one(state={"hp": 3}, questions=questions3())
    post = s.posts[0]
    assert post["url"] == "https://api.cloudflare.com/client/v4/accounts/acct/ai/run/@cf/cloudflare/clef"
    assert post["headers"] == {"Authorization": "Bearer tok"} and post["timeout"] == decision.HTTP_TIMEOUT
    assert post["json"]["model"] == "clef" and post["json"]["state"] == {"hp": 3}
    assert post["json"]["questions"]["pick"] == {"type": "choice", "instructions": "Choose the option to do now.",
                                                 "criteria": {"a": "Do A", "b": "Do B"}}
    assert r.choices["pick"].choice == "b" and r.choices["pick"].probabilities == {"a": 0.2, "b": 0.8}
    assert r.nouls["hurt"].noul == 0.9 and r.scores["danger"].score == 0.4
    assert r.usage.input_tokens == 90 and r.model == "clef"


def test_clef_flash_and_no_envelope(monkeypatch):
    monkeypatch.setenv(decision.MODEL_ENV, "clef-flash")
    s = FakeSession(CLEF_ANSWER)
    r = decision.ClefDecider(session=s).system_one(state="s", questions=questions3(), timeout=8.0)
    assert s.posts[0]["url"].endswith("/@cf/cloudflare/clef-flash") and s.posts[0]["json"]["model"] == "clef-flash"
    assert s.posts[0]["timeout"] == 8.0 and r.choices["pick"].choice == "b"


def test_an_http_error_names_the_status_but_not_the_key(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "secret-token")
    s = FakeSession({"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}, status=401)
    with pytest.raises(RuntimeError) as e:
        decision.ClefDecider(session=s).system_one(state={}, questions=pick({"a": "A"}))
    assert "HTTP 401" in str(e.value) and "Authentication error" in str(e.value)
    assert "secret-token" not in str(e.value)


OPENAI_ANSWER = {"model": "gpt-6-luna", "usage": {"input_tokens": 120}, "answers": [
    {"type": "choice", "name": "pick", "choice": "a", "confidence": 0.7,
     "probabilities": [{"value": "a", "probability": 0.7}, {"value": "b", "probability": 0.3}]},
    {"type": "predicate", "name": "hurt", "probability": 0.25},
    {"type": "score", "name": "danger", "score": 0.9, "confidence": 0.9,
     "probabilities": [{"value": 0, "label": "0", "probability": 0.1},
                       {"value": 1, "label": "1", "probability": 0.9}]}]}


def test_openai_asks_the_decisions_api_in_its_own_words(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ok")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv(decision.MODEL_ENV, raising=False)
    s = FakeSession(OPENAI_ANSWER)
    r = decision.OpenAIDecider(session=s).system_one(state={"hp": 3}, questions=questions3())
    post = s.posts[0]
    assert post["url"] == "https://api.openai.com/v1/decisions"
    assert post["headers"] == {"Authorization": "Bearer ok"}
    body = post["json"]
    assert body["model"] == "gpt-6-luna" and json.loads(body["input"]) == {"hp": 3}
    assert body["questions"][0] == {"type": "choice", "name": "pick", "instructions": "Choose the option to do now.",
                                    "choices": [{"value": "a", "description": "Do A"},
                                                {"value": "b", "description": "Do B"}]}
    assert body["questions"][1] == {"type": "predicate", "name": "hurt",
                                    "instructions": "Hurt? True means: Health under half."}
    assert body["questions"][2]["levels"] == [{"label": "0", "description": "Safe"},
                                              {"label": "1", "description": "Deadly"}]
    assert r.choices["pick"].choice == "a" and r.choices["pick"].probabilities == {"a": 0.7, "b": 0.3}
    assert r.nouls["hurt"].noul == 0.25
    assert r.scores["danger"].probabilities == {"0": 0.1, "1": 0.9} and r.scores["danger"].score == 0.9
    assert r.usage.input_tokens == 120 and r.usage.output_tokens == 0


def test_openai_refusal_raises():
    s = FakeSession({"answers": [{"type": "refusal", "name": "pick"}]})
    with pytest.raises(RuntimeError, match="refused"):
        decision.OpenAIDecider(session=s).system_one(state={}, questions=pick({"a": "A"}))


@pytest.mark.parametrize("name, cls", [("clef", "ClefDecider"), ("openai", "OpenAIDecider")])
def test_the_http_providers_are_chosen_by_name(monkeypatch, name, cls):
    monkeypatch.setenv(decision.PROVIDER_ENV, name)
    c = decision.client()
    assert type(c).__name__ == cls and c.answer_timeout == decision.HTTP_TIMEOUT
