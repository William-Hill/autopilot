"""The brain's own model calls, for reading the game's code at scale: `llm` and `llm_map` in probes (the agent's
questions over files it doesn't want in its own context) and the background researcher (agents/researcher.py), which
reads what the export brings and what each episode raises while the agent plays.

    Model       one route to a text model: the agent's provider's (Aura's relay in a run through Aura), as
                describe_frames has (vision.route), with the run's usage counted. ask() a question; turn() one step
                of a conversation with tools (the researcher's).
    Jobs        llm_map's background work: one question over many files, each file's answer kept whole in a results
                file of the run, the agent collecting them when it likes (Job.result()).

The model is the provider's cheaper one (HELPER) unless AUTOPILOT_HELPER_MODEL names another; one the provider (or
Aura's relay) refuses gives way to the agent's own for the rest of the run.
"""

import concurrent.futures as futures
import json
import os
import threading
import time

import requests

from . import vision

# The cheaper model of each provider's family that reads code well enough to answer about it (None: the agent's own)
HELPER = {"anthropic": "claude-sonnet-5-5", "opencode": "claude-sonnet-5-5",
          "openrouter": "anthropic/claude-sonnet-5.5", "openai": None, "google": None}
MAX_TOKENS = 8000  # one answer's tokens, at most (what a model writes about a few files)
TIMEOUT = 180  # seconds one call may take
WORKERS = 8  # llm_map's calls at once
MAP_FILE_BYTES = 400_000  # one file's text in one llm_map call, at most (a model's context): a bigger file is read
                          # in parts of this size, each answered, and the answers kept together


def helper_model(provider, agent_model=None):
    """The reading model for a provider's run: AUTOPILOT_HELPER_MODEL, else HELPER's, else the agent's own."""
    return (os.environ.get("AUTOPILOT_HELPER_MODEL") or "").strip() or HELPER.get(provider) or agent_model


class Model:
    """A run's model for the brain's own reading. provider, agent_model: the agent's; usage: the run's
    telemetry.Usage (calls not through the run's proxy are counted there); proxied: the proxy's URL prefix."""

    def __init__(self, provider, agent_model=None, usage=None, proxied=None):
        self.provider = provider
        self.agent_model = agent_model
        self.route, self.unavailable = vision.route(provider, agent_model)
        if self.route is not None:
            wire, root, key, _, path = self.route
            chosen = helper_model(provider, agent_model)
            self.route = (wire, root, key, chosen or agent_model, path)
        self.usage = usage
        self.proxied = proxied
        self.lock = threading.Lock()
        # Every call's tokens by model, whoever else counts them: the report's reading_usage (what the brain's own
        # reading cost the run, apart from the agent's)
        self.tokens = {}

    @property
    def name(self):
        return self.route[3] if self.route else None

    def ask(self, prompt, system=None, max_tokens=MAX_TOKENS):
        """The model's answer to prompt (text)."""
        reply = self.turn([{"role": "user", "content": prompt}], system=system, max_tokens=max_tokens)
        return reply["text"]

    def turn(self, messages, system=None, tools=None, max_tokens=MAX_TOKENS):
        """One step of a conversation: messages in the neutral shape ({"role": "user"|"assistant", "content": text}
        or a turn() reply's "message", and {"role": "tool", "id", "content"} for a tool's answer); tools: [(name,
        description, JSON schema)]. Returns {"text", "calls": [{"id", "name", "args"}], "message"} (message: the
        assistant's turn, to append)."""
        if self.route is None:
            raise RuntimeError(f"no model to read with: {self.unavailable}")
        wire = self.route[0]
        try:
            return self._anthropic(messages, system, tools, max_tokens) if wire == "anthropic" else \
                self._openai(messages, system, tools, max_tokens)
        except Exception as e:
            if not vision._refused_model(e) or not self.agent_model or self.route[3] == self.agent_model:
                raise
            with self.lock:  # (the cheaper model isn't served here: the agent's own for the rest of the run)
                self.route = self.route[:3] + (self.agent_model,) + self.route[4:]
            return self.turn(messages, system, tools, max_tokens)

    def _anthropic(self, messages, system, tools, max_tokens):
        import anthropic

        _, root, key, model, _ = self.route
        bearer = not (os.environ.get("ANTHROPIC_API_KEY") or "").strip() and key == os.environ.get(
            "ANTHROPIC_AUTH_TOKEN")
        client = anthropic.Anthropic(api_key=None if bearer else key, auth_token=key if bearer else None,
                                     base_url=root, timeout=TIMEOUT, max_retries=2)
        wire_messages = []
        for m in messages:
            if m["role"] == "tool":  # (tool answers go back as the user's tool_result blocks, one turn for all)
                block = {"type": "tool_result", "tool_use_id": m["id"], "content": m["content"]}
                if wire_messages and wire_messages[-1]["role"] == "user" and isinstance(wire_messages[-1]["content"],
                                                                                         list):
                    wire_messages[-1]["content"].append(block)
                else:
                    wire_messages.append({"role": "user", "content": [block]})
            elif m["role"] == "assistant" and "blocks" in m:
                wire_messages.append({"role": "assistant", "content": m["blocks"]})
            else:
                wire_messages.append({"role": m["role"], "content": m["content"]})
        # (cached up to the last block: a tool loop resends its whole conversation each turn, and the researcher's
        # passes share the instructions and tools; uncached, a big project's researcher read millions of tokens at
        # full price)
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": wire_messages,
                  "cache_control": {"type": "ephemeral"}}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [{"name": n, "description": d, "input_schema": s} for n, d, s in tools]
        message = client.messages.create(**kwargs)
        body = message.to_dict()
        self._count(root, body)
        blocks = body.get("content") or []
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        calls = [{"id": b["id"], "name": b["name"], "args": b.get("input") or {}}
                 for b in blocks if b.get("type") == "tool_use"]
        return {"text": text, "calls": calls, "message": {"role": "assistant", "content": text, "blocks": blocks}}

    def _openai(self, messages, system, tools, max_tokens):
        _, root, key, model, path = self.route
        url = root + path
        wire_messages = [{"role": "system", "content": system}] if system else []
        for m in messages:
            if m["role"] == "tool":
                wire_messages.append({"role": "tool", "tool_call_id": m["id"], "content": m["content"]})
            elif m["role"] == "assistant" and "wire" in m:
                wire_messages.append(m["wire"])
            else:
                wire_messages.append({"role": m["role"], "content": m["content"]})
        limit = "max_completion_tokens" if "api.openai.com" in url or self.provider == "openai" else "max_tokens"
        body = {"model": model, limit: max_tokens, "messages": wire_messages}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": n, "description": d, "parameters": s}}
                             for n, d, s in tools]
        r = requests.post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json=body, timeout=TIMEOUT)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
        answer = r.json()
        self._count(url, answer)
        msg = ((answer.get("choices") or [{}])[0].get("message") or {})
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads((c.get("function") or {}).get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append({"id": c.get("id"), "name": (c.get("function") or {}).get("name"), "args": args})
        text = (msg.get("content") or "").strip()
        wire = {"role": "assistant", "content": msg.get("content")}
        if msg.get("tool_calls"):
            wire["tool_calls"] = msg["tool_calls"]
        return {"text": text, "calls": calls, "message": {"role": "assistant", "content": text, "wire": wire}}

    def _count(self, url, body):
        from .telemetry import llm_usage

        model, usage = llm_usage(body)
        if not usage:
            return
        with self.lock:
            kept = self.tokens.setdefault(model or self.name, {"calls": 0})
            kept["calls"] += 1
            for k, v in usage.items():
                kept[k] = kept.get(k, 0) + v
        if self.usage is None or (self.proxied and url.startswith(self.proxied)):
            return
        self.usage.add("llm", model, **usage)


def parts(text, size=MAP_FILE_BYTES):
    """text in parts of at most size characters, cut at line breaks where there are any: every character is in one."""
    out = []
    while len(text) > size:
        at = text.rfind("\n", size // 2, size)
        at = at + 1 if at > 0 else size
        out.append(text[:at])
        text = text[at:]
    out.append(text)
    return out


class Job:
    """One llm_map: a question over files, answered in the background. done(): whether every file is answered;
    result(wait=True): {path: answer} (with wait False, the answers so far); file: where the answers are kept whole
    (written as they come)."""

    def __init__(self, n, question, paths, file):
        self.n = n
        self.question = question
        self.paths = list(paths)
        self.file = file
        self.answers = {}
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.started = time.time()

    def done(self):
        return self.finished.is_set()

    def result(self, wait=True, timeout=None):
        if wait:
            self.finished.wait(timeout)
        with self.lock:
            return dict(self.answers)

    def __repr__(self):
        with self.lock:
            got = len(self.answers)
        state = "done" if self.done() else "running"
        return (f"<llm_map {self.n}: {got} of {len(self.paths)} files answered ({state}); "
                f".result() for {{path: answer}}; all of them in {self.file}>")


class Reader:
    """llm and llm_map for a run's probes: the model (a Model), the folder files are read from (the game's) and the
    run's results folder (each llm_map's answers, whole). Calls run on a pool of WORKERS threads."""

    SYSTEM = ("You answer a question about a video game's code or data for an engineer who is writing a program that "
              "plays the game. Answer from the text you are given: exact names, numbers, conditions and where in the "
              "text they are. Say plainly when the text doesn't answer it.")

    def __init__(self, model, root, results_dir, note=None):
        self.model = model
        self.root = root
        self.results = results_dir
        self.note = note or (lambda text: None)
        self.pool = futures.ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="llm")
        self.jobs = []
        self.lock = threading.Lock()

    def _text(self, item):
        """(label, text) of a path in the game's folder, or of text given as is. A path out of the game's folder is
        refused: the sandbox's child asks through here, and its code may only read the game's folder."""
        item = str(item)
        if "\n" in item:
            return "(text)", item
        root = os.path.realpath(self.root)
        given = item if os.path.isabs(item) else os.path.join(root, *item.replace("\\", "/").split("/"))
        full = os.path.realpath(given)
        inside = full == root or full.startswith(root + os.sep)
        if os.path.isfile(full):
            if not inside:
                raise ValueError(f"{item}: only files in the game's folder can be read")
            with open(full, encoding="utf-8", errors="replace") as f:
                return os.path.relpath(full, root).replace(os.sep, "/"), f.read()
        return "(text)", item

    def ask(self, question, about=None):
        """The model's answer to question about `about`: a path in the game's folder, a list of them, or text."""
        items = about if isinstance(about, (list, tuple)) else ([about] if about is not None else [])
        texts = [self._text(i) for i in items]
        body = "\n\n".join(f"=== {label} ===\n{text}" for label, text in texts)
        prompt = f"{question}\n\n{body}" if body else question
        answer = self.model.ask(prompt, system=self.SYSTEM)
        self.note(f"llm ({self.model.name}): {question} over {[label for label, _ in texts]}")
        return answer

    def map(self, question, paths):
        """A Job: question asked of each path on its own, in the background; a file bigger than one call takes is
        asked in parts (parts()), the answers kept together."""
        with self.lock:
            n = len(self.jobs) + 1
            os.makedirs(self.results, exist_ok=True)
            job = Job(n, question, paths, os.path.join(self.results, f"llm_map-{n}.txt"))
            self.jobs.append(job)
        with open(job.file, "w", encoding="utf-8") as f:
            f.write(f"# llm_map {n}: {question}\n# {len(job.paths)} files\n")
        self.note(f"llm_map {n} ({self.model.name}): {question} over {len(job.paths)} files")
        left = [len(job.paths)]

        def one(path):
            try:
                label, text = self._text(path)
                answers = [self.model.ask(f"{question}\n\n=== {label}"
                                          + (f" (part {i + 1} of {len(chunks)})" if len(chunks) > 1 else "")
                                          + f" ===\n{chunk}", system=self.SYSTEM)
                           for chunks in [parts(text)] for i, chunk in enumerate(chunks)]
                answer = "\n\n".join(answers)
            except Exception as e:
                answer = f"(no answer: {e})"
            with job.lock:
                job.answers[path] = answer
                with open(job.file, "a", encoding="utf-8") as f:
                    f.write(f"\n=== {path} ===\n{answer}\n")
                left[0] -= 1
                if left[0] == 0:
                    job.finished.set()

        if not job.paths:
            job.finished.set()
        for path in job.paths:
            self.pool.submit(one, path)
        return job

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
