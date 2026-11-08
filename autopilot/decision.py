"""The decision model: the fast model that plays inside a harness and reads a prompt's kind. Jev (TypeSafe) is the
default; AUTOPILOT_DECISION_PROVIDER chooses another.

Every provider has the interface of typesafe_sdk's client that the runtime, the sandbox and cli.classify use:

    client.system_one(state, questions, **options) -> answer
        state:     JSON facts (a dict or a str)
        questions: {id: typesafe_sdk.Choice | Noul | Score}
        options:   timeout= (seconds) and retry= (a typesafe_sdk.RetryPolicy); a provider may ignore them
    answer.model, answer.usage.input_tokens / .output_tokens, answer.raw (the API's answer, JSON-safe)
    answer.choices[id].choice / .probabilities / .confidence
    answer.nouls[id].noul (the probability of yes)
    answer.scores[id].score / .probabilities / .confidence

Providers:
    jev        TypeSafe's Jev (runtime.jev_client): calibrated probabilities from the model itself. The default.
    anthropic  A Claude model through the Messages API with a JSON schema (structured outputs). The model states
               the probabilities itself: they are not calibrated like Jev's, and a decision takes longer.
    clef       Cloudflare's Clef or Clef-flash on Workers AI: Jev's request shape, probabilities from the model.
    openai     OpenAI's Decisions API (gpt-6-luna): predicate/choice/score questions, probabilities from the model.

Aura's relay passes only TypeSafe's and Anthropic's traffic: clef and openai need the user's own keys.
"""

import json
import os
import threading
from dataclasses import dataclass, field

PROVIDER_ENV = "AUTOPILOT_DECISION_PROVIDER"
MODEL_ENV = "AUTOPILOT_DECISION_MODEL"
PROVIDERS = ("jev", "anthropic", "clef", "openai")
THROUGH_AURA = ("jev", "anthropic")  # (the providers whose traffic Aura's relay passes)
# A small, fast model: a decision must come back in about a second
ANTHROPIC_MODEL = "claude-haiku-5-5"
ANTHROPIC_TIMEOUT = 3.0  # seconds one call may take (the runtime forgets an answer after answer_timeout * 2.5)
ANTHROPIC_MAX_TOKENS = 1024
CLEF_MODEL = "clef"  # (or clef-flash: faster, smaller)
CLEF_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}"
OPENAI_MODEL = "gpt-6-luna"
OPENAI_URL = "https://api.openai.com/v1"
HTTP_TIMEOUT = 2.0
# What a model of each provider starts with: AUTOPILOT_DECISION_MODEL is one setting for every provider
MODEL_PREFIXES = {"anthropic": ("claude-",), "clef": ("clef",), "openai": ("gpt-",)}
DEFAULT_MODELS = {"anthropic": ANTHROPIC_MODEL, "clef": CLEF_MODEL, "openai": OPENAI_MODEL}
# The keys a provider needs: each group is one setting, and any name in a group gives it
KEYS = {"jev": [("TYPESAFE_API_KEY",)], "anthropic": [("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")],
        "clef": [("CLOUDFLARE_API_TOKEN",), ("CLOUDFLARE_ACCOUNT_ID",)], "openai": [("OPENAI_API_KEY",)]}


class ProviderError(ValueError):
    """A provider or model setting that Autopilot cannot use."""


def provider():
    """The decision model's provider by AUTOPILOT_DECISION_PROVIDER: jev (the default), anthropic, clef or openai."""
    name = (os.environ.get(PROVIDER_ENV) or "").strip().lower() or "jev"
    if name not in PROVIDERS:
        raise ProviderError(f"{PROVIDER_ENV}={name!r}: use one of {', '.join(PROVIDERS)}")
    return name


def model_of(name, default):
    """The model of provider name: AUTOPILOT_DECISION_MODEL when it is one of that provider's, else default.
    A model of another provider (left from an earlier choice) is an error."""
    chosen = (os.environ.get(MODEL_ENV) or "").strip()
    if not chosen:
        return default
    if not chosen.startswith(MODEL_PREFIXES[name]):
        raise ProviderError(f"{MODEL_ENV}={chosen!r} is not a model of {name} (its models start with "
                            f"{' or '.join(MODEL_PREFIXES[name])}): change or remove the line")
    return chosen


def check():
    """The provider, once its settings are usable (a ProviderError says what to change)."""
    name = provider()
    if name in DEFAULT_MODELS:
        model_of(name, DEFAULT_MODELS[name])
    return name


def missing_keys(name=None, aura=False):
    """The settings the provider (by default, the one chosen) still needs, by their first names. aura: the run goes
    through Aura, whose relay gives Jev's and Anthropic's keys."""
    name = name or provider()
    if aura and name in THROUGH_AURA:
        return []
    return [group[0] for group in KEYS[name] if not any((os.environ.get(k) or "").strip() for k in group)]


def client(warm=True):
    """The decision model's client, by provider(). warm: for Jev, keep its connection and server warm."""
    name = provider()
    if name == "anthropic":
        return AnthropicDecider()
    if name == "clef":
        return ClefDecider()
    if name == "openai":
        return OpenAIDecider()
    from . import runtime

    # (two calls, not jev_client(warm=warm): tests stand in a jev_client that takes no argument)
    return runtime.jev_client() if warm else runtime.jev_client(warm=False)


# -- the answer, in typesafe_sdk's shape ------------------------------------------------------------------------------

@dataclass
class ChoiceAnswer:
    choice: str
    probabilities: dict
    confidence: float


@dataclass
class NoulAnswer:
    noul: float


@dataclass
class ScoreAnswer:
    score: float
    probabilities: dict
    confidence: float


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Answer:
    model: str
    choices: dict = field(default_factory=dict)
    nouls: dict = field(default_factory=dict)
    scores: dict = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    raw: object = None


# -- a language model as the decision model ---------------------------------------------------------------------------

SYSTEM = (
    "You are a fast decision model. You get `state` (JSON facts) and typed questions about it. Answer each question "
    "with the JSON the schema asks for, and nothing else.\n"
    "- A choice question: give a probability from 0 to 1 for each option, by how well it fits the instructions now. "
    "The probabilities must add up to 1. Put most of the probability on the best option.\n"
    "- A yes/no question: give the probability, from 0 to 1, that the answer is yes.\n"
    "- A score question: give a probability from 0 to 1 for each level (keys are the level numbers). They must add "
    "up to 1."
)


def _text(content):
    """A question's instructions or criterion as text for the prompt."""
    if content is None:
        return ""
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def _kind(q):
    return getattr(q, "type", None) or "choice"


def prompt_and_schema(state, questions):
    """The user message and the JSON schema of the answer, for a Messages API call."""
    lines, props = [], {}
    for qid, q in questions.items():
        kind = _kind(q)
        if kind == "choice":
            names = list(q.criteria)
            opts = "\n".join(f"  - {n}: {_text(d) or n}" for n, d in q.criteria.items())
            lines.append(f"Question `{qid}` (choice): {_text(q.instructions)}\n  Options:\n{opts}")
            props[qid] = _probabilities(names)
        elif kind == "noul":
            crit = q.criteria or {}
            extra = "".join(f"\n  {k.capitalize()} means: {_text(crit.get(k))}" for k in ("true", "false")
                            if crit.get(k))
            lines.append(f"Question `{qid}` (yes/no): {_text(q.instructions)}{extra}")
            props[qid] = {"type": "number"}
        elif kind == "score":
            levels = "\n".join(f"  {i}: {_text(c)}" for i, c in enumerate(q.criteria))
            lines.append(f"Question `{qid}` (score): {_text(q.instructions)}\n  Levels:\n{levels}")
            props[qid] = _probabilities([str(i) for i in range(len(q.criteria))])
        else:
            raise ValueError(f"question {qid}: the anthropic decision model cannot answer a {kind} question")
    schema = {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    return f"state:\n{state_text}\n\n" + "\n\n".join(lines), schema


def _probabilities(names):
    return {"type": "object", "properties": {n: {"type": "number"} for n in names}, "required": list(names),
            "additionalProperties": False}


def normalized(values, names):
    """values ({name: number}) as probabilities over names: negatives and non-numbers as 0, summing to 1 (all
    equal when every one is 0)."""
    clean = {}
    for n in names:
        v = values.get(n) if isinstance(values, dict) else None
        clean[n] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0.0
    total = sum(clean.values())
    if total <= 0:
        return {n: 1.0 / len(names) for n in names} if names else {}
    return {n: v / total for n, v in clean.items()}


def answers_from(data, questions):
    """The typed answers to questions from the model's JSON (data)."""
    choices, nouls, scores = {}, {}, {}
    for qid, q in questions.items():
        kind = _kind(q)
        got = data.get(qid) if isinstance(data, dict) else None
        if kind == "choice":
            probs = normalized(got, list(q.criteria))
            best = max(probs, key=probs.get)
            choices[qid] = ChoiceAnswer(choice=best, probabilities=probs, confidence=probs[best])
        elif kind == "noul":
            p = float(got) if isinstance(got, (int, float)) and not isinstance(got, bool) else 0.5
            nouls[qid] = NoulAnswer(noul=min(1.0, max(0.0, p)))
        elif kind == "score":
            levels = [str(i) for i in range(len(q.criteria))]
            probs = normalized(got, levels)
            scores[qid] = ScoreAnswer(score=sum(int(k) * v for k, v in probs.items()), probabilities=probs,
                                      confidence=max(probs.values()))
    return choices, nouls, scores


class AnthropicDecider:
    """The decision model as a Claude model (AUTOPILOT_DECISION_MODEL, by default ANTHROPIC_MODEL), asked through
    the Messages API with a JSON schema. Through Aura, ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY point at its relay."""

    answer_timeout = ANTHROPIC_TIMEOUT  # (the runtime's limit for an answer: runtime.Runtime.answers)

    def __init__(self, model=None, sdk=None):
        self.model = model or model_of("anthropic", ANTHROPIC_MODEL)
        if sdk is None:
            import anthropic

            sdk = anthropic.Anthropic(timeout=ANTHROPIC_TIMEOUT, max_retries=1)
        self.sdk = sdk

    def request(self, state, questions):
        """The Messages API request for one decision."""
        text, schema = prompt_and_schema(state, questions)
        req = {"model": self.model, "max_tokens": ANTHROPIC_MAX_TOKENS, "system": SYSTEM,
               "messages": [{"role": "user", "content": text}],
               "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}}}
        if self.model.startswith("claude-haiku"):
            req["thinking"] = {"type": "disabled"}  # (a decision is a fast judgment: no thinking, less latency)
        return req

    def system_one(self, state, questions, timeout=None, retry=None, **_):
        sdk = self.sdk.with_options(timeout=timeout) if timeout else self.sdk
        r = sdk.messages.create(**self.request(state, questions))
        if getattr(r, "stop_reason", None) == "refusal":
            raise RuntimeError("the decision model refused to answer")
        text = next((b.text for b in r.content if getattr(b, "type", None) == "text"), "")
        try:
            data = json.loads(text)
        except ValueError as e:
            raise RuntimeError(f"the decision model's answer is not JSON ({e}): {text[:200]!r}") from None
        choices, nouls, scores = answers_from(data, questions)
        u = getattr(r, "usage", None)
        try:
            raw = r.model_dump(mode="json")
        except Exception:
            raw = None
        return Answer(model=getattr(r, "model", None) or self.model, choices=choices, nouls=nouls, scores=scores,
                      usage=Usage(getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0),
                      raw=raw)


# -- decision APIs over HTTP ------------------------------------------------------------------------------------------

def _number(v, default=0.0):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _answer(model, out, raw, questions, choices, nouls, scores):
    """The Answer from a decision API's answers; an error when a question has no answer (the runtime counts it)."""
    unanswered = [q for q in questions if q not in choices and q not in nouls and q not in scores]
    if unanswered:
        raise RuntimeError(f"the decision model gave no answer to {', '.join(unanswered)}: {str(raw)[:300]}")
    u = out.get("usage") or {}
    return Answer(model=out.get("model") or model, choices=choices, nouls=nouls, scores=scores,
                  usage=Usage(int(_number(u.get("input_tokens"))), int(_number(u.get("output_tokens")))), raw=raw)


class HTTPDecider:
    """A decision API reached with requests: one kept-open session per thread (the runtime and the sandbox ask from
    several threads, and requests does not promise that a session is safe to share between them)."""

    name = None  # (the provider: in errors, and for its model setting)
    answer_timeout = HTTP_TIMEOUT

    def __init__(self, model=None, session=None):
        self.model = model or model_of(self.name, self.default_model)
        self._session = session
        self._local = threading.local()

    @property
    def session(self):
        if self._session is not None:
            return self._session
        if getattr(self._local, "session", None) is None:
            import requests

            self._local.session = requests.Session()
        return self._local.session

    def post(self, url, headers, body, timeout=None):
        r = self.session.post(url, headers=headers, json=body, timeout=timeout or self.answer_timeout)
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code >= 400 or not isinstance(data, dict):
            why = (data.get("errors") or data.get("error")) if isinstance(data, dict) else (r.text or "")[:200]
            raise RuntimeError(f"{self.name}: HTTP {r.status_code}: {str(why)[:300]}")
        return data


class ClefDecider(HTTPDecider):
    """Cloudflare's Clef (CLEF_MODEL; AUTOPILOT_DECISION_MODEL=clef-flash for the smaller one) on Workers AI. It takes
    Jev's request (model, state, questions) and gives Jev's answers. Keys: CLOUDFLARE_API_TOKEN (a Workers AI token)
    and CLOUDFLARE_ACCOUNT_ID."""

    name = "clef"
    default_model = CLEF_MODEL

    def system_one(self, state, questions, timeout=None, retry=None, **_):
        account = (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
        token = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
        body = {"model": self.model, "state": state,
                "questions": {k: q.model_dump(mode="json", exclude_none=True) for k, q in questions.items()}}
        data = self.post(CLEF_URL.format(account=account, model=self.model),
                         {"Authorization": f"Bearer {token}"}, body, timeout)
        out = data["result"] if isinstance(data.get("result"), dict) else data  # (Cloudflare's envelope, if any)
        choices, nouls, scores = {}, {}, {}
        for qid, a in (out.get("answers") or {}).items():
            kind = a.get("type") or _kind(questions.get(qid))
            probs, confidence = dict(a.get("probabilities") or {}), _number(a.get("confidence"))
            if kind == "choice":
                choices[qid] = ChoiceAnswer(choice=a.get("choice"), probabilities=probs, confidence=confidence)
            elif kind == "noul":
                nouls[qid] = NoulAnswer(noul=_number(a.get("noul"), 0.5))
            elif kind == "score":
                scores[qid] = ScoreAnswer(score=_number(a.get("score")), probabilities=probs, confidence=confidence)
        return _answer(self.model, out, data, questions, choices, nouls, scores)


class OpenAIDecider(HTTPDecider):
    """OpenAI's Decisions API (POST /v1/decisions, OPENAI_MODEL). A Noul is asked as a predicate, a Choice's criteria
    as choices, a Score's levels as levels; the answers come back in Jev's shape. Key: OPENAI_API_KEY (OPENAI_BASE_URL
    changes the API root)."""

    name = "openai"
    default_model = OPENAI_MODEL

    @staticmethod
    def question(qid, q):
        """A typesafe_sdk question as a Decisions API question."""
        kind = _kind(q)
        text = _text(q.instructions) or qid
        if kind == "choice":
            return {"type": "choice", "name": qid, "instructions": text,
                    "choices": [{"value": n, "description": _text(d) or n} for n, d in q.criteria.items()]}
        if kind == "noul":
            crit = q.criteria or {}
            extra = "".join(f" {k.capitalize()} means: {_text(crit.get(k))}." for k in ("true", "false") if crit.get(k))
            return {"type": "predicate", "name": qid, "instructions": text + extra}
        if kind == "score":
            return {"type": "score", "name": qid, "instructions": text,
                    "levels": [{"label": str(i), "description": _text(c) or str(i)} for i, c in enumerate(q.criteria)]}
        raise ValueError(f"question {qid}: the openai decision model cannot answer a {kind} question")

    def system_one(self, state, questions, timeout=None, retry=None, **_):
        root = ((os.environ.get("OPENAI_BASE_URL") or "").strip() or OPENAI_URL).rstrip("/")
        token = (os.environ.get("OPENAI_API_KEY") or "").strip()
        body = {"model": self.model,
                "input": state if isinstance(state, str) else json.dumps(state, ensure_ascii=False),
                "questions": [self.question(k, q) for k, q in questions.items()]}
        data = self.post(f"{root}/decisions", {"Authorization": f"Bearer {token}"}, body, timeout)
        choices, nouls, scores = {}, {}, {}
        for a in data.get("answers") or []:
            qid, kind = a.get("name"), a.get("type")
            if kind == "refusal":
                raise RuntimeError(f"the decision model refused to answer {qid}")
            if qid not in questions:
                continue
            listed = a.get("probabilities") or []
            confidence = _number(a.get("confidence"))
            if kind == "predicate":
                nouls[qid] = NoulAnswer(noul=_number(a.get("probability"), 0.5))
            elif kind == "choice":
                probs = {p.get("value"): _number(p.get("probability")) for p in listed}
                choices[qid] = ChoiceAnswer(choice=a.get("choice"), probabilities=probs, confidence=confidence)
            elif kind == "score":
                # (a level by its label, which is its index here; else its value; else its place in the list)
                probs = {str(p.get("label", p.get("value", i))): _number(p.get("probability"))
                         for i, p in enumerate(listed)}
                scores[qid] = ScoreAnswer(score=_number(a.get("score")), probabilities=probs, confidence=confidence)
        return _answer(self.model, data, data, questions, choices, nouls, scores)
