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
"""

import json
import os
import time
from dataclasses import dataclass, field

PROVIDER_ENV = "AUTOPILOT_DECISION_PROVIDER"
MODEL_ENV = "AUTOPILOT_DECISION_MODEL"
PROVIDERS = ("jev", "anthropic")
# A small, fast model: a decision must come back in about a second
ANTHROPIC_MODEL = "claude-haiku-5-5"
ANTHROPIC_TIMEOUT = 3.0  # seconds one call may take (the runtime forgets an answer after answer_timeout * 2.5)
ANTHROPIC_MAX_TOKENS = 1024
# The keys a provider needs when a run does not go through Aura (Aura's relay sets its own)
KEYS = {"jev": ("TYPESAFE_API_KEY",), "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}


def provider():
    """The decision model's provider by AUTOPILOT_DECISION_PROVIDER: jev (the default) or anthropic."""
    name = (os.environ.get(PROVIDER_ENV) or "").strip().lower() or "jev"
    if name not in PROVIDERS:
        raise ValueError(f"{PROVIDER_ENV}={name!r}: use one of {', '.join(PROVIDERS)}")
    return name


def key_names(name=None):
    """The environment variables that hold the key of the provider (by default, the one chosen)."""
    return KEYS[name or provider()]


def client(warm=True):
    """The decision model's client, by provider(). warm: for Jev, keep its connection and server warm."""
    name = provider()
    if name == "anthropic":
        return AnthropicDecider()
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
    seconds: float = 0.0


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
            raise ValueError(f"question {qid}: the anthropic decision model can't answer a {kind} question")
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
        self.model = model or (os.environ.get(MODEL_ENV) or "").strip() or ANTHROPIC_MODEL
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
        start = time.monotonic()
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
                      raw=raw, seconds=time.monotonic() - start)
