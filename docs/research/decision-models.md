# Decision models other than Jev

Date: 2026-11-07. Branch: `feat/pluggable-decision-model` (base `threejs-engine`).

Autopilot uses TypeSafe's Jev as its decision model. Jev plays inside a harness and reads the kind of a run from
a prompt. This note records how Jev is connected now, which other decision models exist, and the adapter on this
branch.

Upstream check (2026-11-07): `ramenvr/autopilot` has no code for another decision model on any of its 6 branches
(`main`, `staging`, and 4 feature branches). No issue and no pull request asks for one. This branch merges with
`upstream/main` with no conflicts.

Key to the marks: **V** means that we read the fact in a primary source (vendor documentation, blog, or price page).
**I** means that the fact comes from a secondary source, or that we inferred it.

## 1. How Jev is connected now

All calls go through one duck-typed interface, the `typesafe_sdk` client:
`client.system_one(state, questions, **options)`.

| Call site | What it sends | What it reads from the answer |
|---|---|---|
| `autopilot/runtime.py` `Runtime.ask` / `answers` (play, each decision) | `state` = goal, situation, options' facts, `now_doing`, `recent`, `plan`. `questions` = `{"pick": Choice(instructions, criteria={option: description})}` plus the harness's extra questions (`Harness.questions`: `Choice`, `Noul`, `Score`) | `choices["pick"].probabilities` (the switch margin compares them), else `.choice`. Extra answers: `nouls[k].noul`, `choices[k].probabilities`, `scores[k].score`. Also `r.raw` or `r.model_dump()` for the trace |
| `autopilot/cli.py` `classify` (the kind of a run) | `state={"request": prompt}`, one `Choice` over verify / goal / explore / test, `timeout=8.0`, `retry=RetryPolicy(...)` | `choices["kind"].choice`. On any error, the first words of the prompt decide |
| `autopilot/sandbox.py` `Sandbox._jev` (the parent, for a sandboxed child) | The questions as JSON (`model_dump`), rebuilt as `Choice` / `Noul` / `Score` | `.choice`, `.probabilities`, `.confidence`, `.noul`, `.score`, `raw` |
| `autopilot/sandbox.py` `RemoteJev` (the child) | Sends the questions to the parent | A `SimpleNamespace` in the same shape |
| `autopilot/telemetry.py` `CountingJev` (a wrapper) | Passes every call through | `r.model`, `r.usage.input_tokens`, `r.usage.output_tokens` |
| `autopilot/runtime.py` `jev_client` / `_keep_warm` | `client.models.list()` every 15 s to keep the server warm | Nothing |

Settings and keys:

- `runtime.JEV_MODEL = "jev-1.13.0"`. `AUTOPILOT_JEV_MODEL` overrides it.
- `runtime.JEV_TIMEOUT = 1.5`. The runtime forgets an answer after 2.5 times that value.
- `cli.decision_key_missing` requires `TYPESAFE_API_KEY` unless the run goes through Aura.
- `aura.py` points `TYPESAFE_BASE_URL` / `TYPESAFE_API_KEY` and `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` at the
  relay for the run. The relay passes only TypeSafe and Anthropic traffic.
- `account.state` shows the masked `TYPESAFE_API_KEY` as the "decision" key in the editor plugins.

Three question types are in use. All three return probabilities:

- **Choice**: `choice`, `confidence`, and `probabilities` per option.
- **Noul**: `noul`, the probability of yes.
- **Score**: `score`, the probability-weighted level, plus `probabilities` and `confidence`.

## 2. The alternatives

### Summary of the two claims

- **Cloudflare: correct.** Cloudflare released Clef and Clef-flash on Workers AI on 2026-10-01. The blog says they
  are "fully Jev-API compatible" (V).
- **OpenAI: correct.** OpenAI has a Decisions API, `POST /v1/decisions`, on `gpt-6-luna`. The guide says that it
  is "now generally available" (V).

### Comparison

| Candidate | API shape | Probabilities or one choice | Latency | Price | License / terms |
|---|---|---|---|---|---|
| **TypeSafe Jev** (the default) | `POST /v1/systemone`: `{model, state, questions}`. Noul, Choice, Score, and Box (images, 1.14) (V) | Full distributions (V) | TypeSafe publishes no figure. Cloudflare measured median 524 ms / p95 536 ms (V, a competitor's benchmark, before Jev 1.14) | $0.042 / M input tokens. Output is free (V) | No training on customer data without consent. TypeSafe can keep data for telemetry and fraud checks. The terms prohibit distillation and a "competing product" (V, MCA §2.3, §4.1) |
| **Cloudflare Clef** / **Clef-flash** | Workers AI `/ai/run/@cf/cloudflare/clef` with `{model, state, questions}`, the Jev shape (V) | Full distributions (V) | Clef median 209 ms / p95 239 ms. Clef-flash median 39 ms / p95 122 ms (V, vendor benchmark) | Clef $0.24 / M input. Clef-flash $0.038 / M input. Output is free (V, model pages) | Open weights, Apache 2.0, on Hugging Face: self-host is possible (V). Zero data retention on Workers AI (V) |
| **OpenAI Decisions API** | `POST /v1/decisions`: `{model: "gpt-6-luna", input, questions[]}`. Types `predicate` (returns `probability`), `choice`, `score` (V) | Full distributions (V) | "about 10x faster than the Responses API". No absolute figure (V) | $0.10 / M uncached input. Output is not charged (V) | Not checked for this endpoint. Standard OpenAI terms assumed (I) |
| **Claude through structured outputs** (on this branch) | Messages API with `output_config.format` = JSON schema (V) | One choice natively. The Messages API has no logprobs (V). The model states probabilities: these are not calibrated (I) | Docs say only "Fastest" for Haiku 5.5. No ms figure (V). Not measured here | `claude-haiku-5-5`: $0.10 in / $0.50 out per M tokens up to 100k tokens (V) | Anthropic commercial terms. Not re-checked in this pass |
| **OpenAI small models + logprobs** | Chat Completions / Responses with `logprobs` (I) | Distribution from token logprobs. Reasoning models reject logprobs or return them inconsistently (I) | Not published | `gpt-5-nano` $0.05 / $0.40 (V) | Standard OpenAI terms (I). The Decisions API makes this method mostly obsolete |
| **Cerebras** | OpenAI-compatible chat, `logprobs` 0–20, JSON schema (V) | Distribution from logprobs (V) | ~1850–3000 tokens/s (V). No decision latency published | Not verified | Developer tier is "not production" (V) |
| **Groq** | OpenAI-compatible chat | One choice only. `logprobs` returns HTTP 400 (V) | Not published | Not checked | Not checked |
| **Google Gemini Flash-Lite** | `generateContent` with JSON schema | One choice for 3.x models: a forum reply says logprobs are no longer returned (I) | Not published | `gemini-3.5-flash-lite` $0.30 / $2.50, `gemini-2.5-flash-lite` $0.10 / $0.40 (V) | Free-tier data improves Google products (V) |
| **Cohere `/v1/classify`** | Deprecated from 2025-09-15. Not available to new customers (V) | — | — | — | Not usable |

### Sources

- TypeSafe: <https://docs.typesafe.ai/api/system-one.md>, <https://docs.typesafe.ai/models.md>,
  <https://typesafe.ai/legal/mca>
- Cloudflare: <https://blog.cloudflare.com/clef-decision-models/>,
  <https://developers.cloudflare.com/changelog/post/2026-10-01-clef-workers-ai/>,
  <https://developers.cloudflare.com/workers-ai/models/clef/>,
  <https://developers.cloudflare.com/workers-ai/models/clef-flash/>
- OpenAI: <https://developers.openai.com/api/docs/guides/decisions>,
  <https://developers.openai.com/api/docs/pricing>
- Anthropic: <https://platform.claude.com/docs/en/about-claude/models/overview>,
  <https://platform.claude.com/docs/en/about-claude/pricing>,
  <https://platform.claude.com/docs/en/build-with-claude/structured-outputs>,
  <https://platform.claude.com/docs/en/api/messages.md> (no "logprob" in the full reference)
- Cerebras: <https://inference-docs.cerebras.ai/api-reference/chat-completions>
- Groq: <https://console.groq.com/docs/openai>
- Cohere: <https://docs.cohere.com/docs/deprecations>

We read the Cloudflare blog and the OpenAI Decisions guide again on 2026-11-07 to confirm the main claims. A research
agent read the other sources on the same day.

### Discrepancies and gaps

- The Clef changelog gives Clef-flash a 64K context. The Clef-flash model page gives 24,576 tokens.
- The Cloudflare blog says that Jev has a 32k context and takes text only. TypeSafe's current docs say 64k and images.
- All latency figures come from vendors. Nobody has measured them on Autopilot's prompts.
- We did not check OpenAI's terms and data retention for `/v1/decisions`, or the Cerebras prices.

## 3. The adapter on this branch

`autopilot/decision.py` names the interface that the code already used. It adds one setting:

- `AUTOPILOT_DECISION_PROVIDER` = `jev` (the default), `anthropic`, `clef`, or `openai`. Any other value is an error.
- Each provider has its own model setting, as Jev has `AUTOPILOT_JEV_MODEL`:
  - `AUTOPILOT_ANTHROPIC_DECISION_MODEL`. The default is `claude-haiku-5-5`.
  - `AUTOPILOT_CLEF_MODEL`. The default is `clef`. Use `clef-flash` for the smaller Clef model.
  - `AUTOPILOT_OPENAI_DECISION_MODEL`. The default is `gpt-6-luna`.

| Provider | Keys | Through Aura |
|---|---|---|
| `jev` | `TYPESAFE_API_KEY` | Yes |
| `anthropic` | `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`) | Yes (not tested) |
| `clef` | `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` | No: the relay does not pass Cloudflare traffic |
| `openai` | `OPENAI_API_KEY` (`OPENAI_BASE_URL` is optional) | No: the relay does not pass OpenAI traffic |

Rules of the adapter:

1. `decision.client(warm)` returns the client of the chosen provider. For `jev`, it calls `runtime.jev_client`,
   with no change to Jev's client, model, timeouts, or keep-warm thread.
2. A provider gives the `typesafe_sdk` answer shape: `choices`, `nouls`, `scores`, `usage`, `model`, `raw`. So the
   runtime, the sandbox, `CountingJev`, and `classify` need no change to read it.
3. A provider can set `answer_timeout`. The runtime uses it in place of `JEV_TIMEOUT` to forget late answers.
4. `cli.decision_key_missing` asks for the key of the chosen provider.

The `anthropic` provider (`AnthropicDecider`):

- It sends one Messages API call per decision. The JSON schema has one property per question.
- A Choice gets a number per option, a Noul gets one number, and a Score gets a number per level.
- The code normalizes the numbers to probabilities, so the switch margin in the runtime still works.
- On Haiku models it turns thinking off. On other models it sends `effort: "low"`, because Opus 5.5 refuses
  thinking off.
- Through Aura, `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` already point at the relay. So this provider can work
  through Aura with no relay change. We did not test this, and we do not know how Aura bills these calls.

### Limits of the `anthropic` provider

- The probabilities are numbers that the model writes. They are not calibrated like Jev's.
- The latency is not measured. If a decision takes more than about 1 s, a harness gets fewer decisions per
  second. Reflex options must stay in executors.
- It does not answer `Box` questions.

### The `clef` and `openai` providers

- `clef` sends Jev's request body unchanged to Workers AI and reads Jev's answer fields. It accepts the answer with
  or without Cloudflare's `{"result": ...}` envelope, because the docs do not show which one comes back.
- `openai` changes the names: a Noul becomes a `predicate`, a Choice's criteria become `choices`, and a Score's
  levels become `levels` with the labels `"0"`, `"1"`, and so on. The state goes in `input` as JSON text.
- Both use `requests`, which Autopilot already depends on. The PR adds no package.
- Both give probabilities from the model, as Jev does.

## 4. Live check (2026-11-07)

We ran `tools/decision_smoke.py` (5 calls each, one Choice of 3 options and one Noul) and `cli.classify` (4
prompts each) from a Mac, with the user's own keys. No call went through Aura.

| Provider | Model | Smoke latency, median / max | Input tokens per call | Pick (probability) | `hurt` (health 30) | `classify`, 4 prompts |
|---|---|---|---|---|---|---|
| `jev` | `jev-1.13.0` | 0.39 s / 0.82 s | 510 | retreat (0.64–0.69) | 0.76–0.78 | 4 of 4 correct, 1.3 s |
| `anthropic` | `claude-haiku-5-5` | 1.19 s / 2.23 s | 788 (+49 output) | retreat 3 times, run_to_door 2 times (0.45–0.50) | 0.10–0.15 | 4 of 4 correct, 5.8 s |
| `clef` | `clef` | 0.25 s / 1.58 s | 331 | attack (0.50) | 0.93 | 4 of 4 correct, 9.8 s |
| `openai` | `gpt-6-luna` | 0.24 s / 1.16 s | 410 | retreat (0.64) | 0.87 | 4 of 4 correct, 2.1 s |

What the live answers confirmed:

- Workers AI wraps the answer: `{"success", "errors", "messages", "result"}`. `result` has Jev's fields
  (`answers`, `model`, `usage`), and a Score answer has `legend`.
- In `/v1/decisions`, a score's `probabilities` is a list of `{"value": 0, "label": "0", "probability": ...}`.
  `usage` has `input_tokens`, `output_tokens` (0), and `total_tokens`.
- `clef` and `openai` gave the same answer on each of the 5 calls. `jev` changed by a few hundredths.

What the live answers show about quality (one situation only, so not a benchmark):

- `anthropic` was the slowest and the least stable. It changed its pick between calls, and it said the player was
  not hurt (0.10) at health 30, where the other three said 0.76–0.93.
- `clef` picked `attack`, where `jev` and `openai` picked `retreat`.
- One situation cannot rank the models. A benchmark on recorded game states is necessary for that.

Not verified:

- No game run with a provider other than Jev. The runtime tests use stand-in APIs.
- No run through Aura with the `anthropic` provider.
- The first call of each provider was slower (0.77–2.23 s), probably from connection setup.
