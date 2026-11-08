"""What a run is expected to cost, before it starts: `autopilot estimate` and `autopilot play --estimate`.

    prices()        the price tables (prices.json beside this file: one place to update, with a source and a date).
    history()       the earlier runs of a project (memory/<project>/runs/<run>/report.json and its traces), each
                    turned into per-episode amounts that do not depend on the model's price.
    estimate()      a range (low, high) for each part of a run: the agent's language model, the reading model
                    (llm.HELPER), the decision model (Jev through TypeSafe), and the total for each way to pay.
    over_limit()    whether a run's high estimate is above AUTOPILOT_MAX_USD.

A "unit" is one million input tokens at the model's input price. A run's agent cost divided by the model's input price
(fast mode included) gives its units, so a run on one model also predicts a run on another model of the same family.
The decision model's part is per provider (DECISION_COSTS): a new provider adds one function there and a table in
prices.json.
"""

import glob
import json
import math
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
PRICES = os.path.join(HERE, "prices.json")
TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write")
# The agent's provider when AUTOPILOT_AGENT names no other (agents/__init__.py: claude is Anthropic, codex OpenAI)
AGENT_PROVIDERS = {"claude": "anthropic", "codex": "openai"}


def prices(path=PRICES):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _canonical(model, table):
    model = str(model or "").strip()
    return table.get("aliases", {}).get(model, model)


def llm_price(model, table):
    """The price row of a language model ({input, output, cache_read, cache_write, fast?}), or None."""
    model = _canonical(model, table)
    for provider in table.get("llm", {}).values():
        row = provider.get("models", {}).get(model)
        if row:
            return row
    return None


def input_price(model, table, fast=False):
    """USD per million input tokens for model (fast mode's multiplier applied when the model has one), or None."""
    row = llm_price(model, table)
    if not row:
        return None
    return row["input"] * (row.get("fast", 1.0) if fast else 1.0)


def usage_usd(usage, table):
    """What a report's reading_usage ({model: {input, output, cache_read, cache_write}}) cost at the table's prices;
    None when a model has no price."""
    total = 0.0
    for model, tokens in (usage or {}).items():
        row = llm_price(model, table)
        if not row:
            return None
        total += sum((tokens.get(k) or 0) * row.get(k, 0.0) for k in TOKEN_FIELDS) / 1e6
    return total


def usage_units(usage, table):
    """A reading_usage in units (each model's cost over its own input price); None when a model has no price."""
    units = 0.0
    for model, tokens in (usage or {}).items():
        cost = usage_usd({model: tokens}, table)
        if cost is None:
            return None
        units += cost / llm_price(model, table)["input"]
    return units


# -- the decision model ------------------------------------------------------------------------------------------------

def typesafe_cost(model, decisions, tokens, table):
    """Jev (TypeSafe): input tokens only, output free. decisions, tokens: (low, high). Returns (low, high) or None."""
    models = table.get("decision", {}).get("typesafe", {}).get("models", {})
    row = next((r for prefix, r in models.items() if str(model).startswith(prefix)), None)
    if row is None:
        return None
    return tuple(d * t * row["input"] / 1e6 for d, t in zip(decisions, tokens))


# Each decision provider's cost function: (model, decisions (low, high), tokens per decision (low, high), table)
DECISION_COSTS = {"typesafe": typesafe_cost}


def decision_model():
    """The decision model and its provider: AUTOPILOT_JEV_MODEL, else runtime.JEV_MODEL, on TypeSafe."""
    from .runtime import jev_model

    return "typesafe", jev_model()


# -- earlier runs ------------------------------------------------------------------------------------------------------

def _trace_numbers(run_dir):
    """(decisions, seconds of play) per episode, from the run's trace<n>.jsonl files."""
    out = []
    for path in sorted(glob.glob(os.path.join(run_dir, "trace*.jsonl"))):
        decisions, last_t = 0, 0.0
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("kind") == "decision":
                        decisions += 1
                    if isinstance(rec.get("t"), (int, float)):
                        last_t = max(last_t, float(rec["t"]))
        except OSError:
            continue
        out.append((decisions, last_t))
    return out


def history(project_dir, table):
    """The project's earlier runs that have cost data, newest first: one dict per run with the agent, model, fast
    mode, episodes, and per-episode agent units, reading units, decisions and seconds of play (None where unknown)."""
    runs = []
    for report in sorted(glob.glob(os.path.join(project_dir, "runs", "*", "report.json")), reverse=True):
        try:
            with open(report, encoding="utf-8") as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        episodes = r.get("episodes") or 0
        if r.get("cost_usd") is None or episodes < 1:
            continue
        price = input_price(r.get("model"), table, bool(r.get("fast")))
        traces = _trace_numbers(os.path.dirname(report))
        reading = usage_units(r.get("reading_usage"), table)
        runs.append({
            "run": r.get("run") or os.path.basename(os.path.dirname(report)),
            "agent": r.get("agent"), "model": r.get("model"), "fast": bool(r.get("fast")),
            "episodes": episodes, "cost_usd": r["cost_usd"],
            "agent_units": r["cost_usd"] / price / episodes if price else None,
            "reading_units": reading / episodes if reading is not None else None,
            "decisions": statistics.mean(d for d, _ in traces) if traces else None,
            "play_seconds": statistics.mean(s for _, s in traces) if traces and any(s for _, s in traces) else None,
        })
    return runs


def spread(values, prior):
    """(low, high) from earlier runs' values, widened while there are few of them; the prior when there are none."""
    values = [v for v in values if v is not None]
    if not values:
        return tuple(prior)
    widen = {1: (0.6, 1.6), 2: (0.75, 1.33)}.get(len(values), (0.9, 1.1))
    return min(values) * widen[0], max(values) * widen[1]


# -- the estimate ------------------------------------------------------------------------------------------------------

def agent_setup(agent=None, model=None, fast=None):
    """The agent, its provider, its model and whether fast mode is on, as a run would pick them now."""
    from .agents import DEFAULT
    from .agents.session import MODELS, fast_mode

    agent = (agent or os.environ.get("AUTOPILOT_AGENT") or DEFAULT).strip().lower()
    provider = AGENT_PROVIDERS.get(agent)
    if provider is None:
        provider = (os.environ.get("AUTOPILOT_OPENCODE_PROVIDER") or "").strip().lower() or "anthropic"
    model = model or (os.environ.get("AUTOPILOT_MODEL") or "").strip() or MODELS.get(provider) or MODELS["anthropic"]
    if fast is None:
        fast = fast_mode() and "opus" in model.lower() and agent != "codex"
    return agent, provider, model, bool(fast)


def helper_model(provider, agent_model):
    """The reading model a run uses (llm.Model's choice)."""
    from .llm import HELPER

    return (os.environ.get("AUTOPILOT_HELPER_MODEL") or "").strip() or HELPER.get(provider) or agent_model


def estimate(project_dir=None, agent=None, model=None, fast=None, episodes=None, minutes=None, table=None):
    """The expected cost of one run. Returns a dict: what the run uses, the basis, and (low, high) USD per part and per
    way to pay ("own_keys", "aura"). A part with no price is None, and `missing` says which price to add."""
    table = table or prices()
    priors = table["priors"]
    agent, provider, model, fast = agent_setup(agent, model, fast)
    helper = helper_model(provider, model)
    decision_provider, decision = decision_model()
    runs = history(project_dir, table) if project_dir and os.path.isdir(project_dir) else []
    same = [r for r in runs if r["agent"] == agent] or runs  # (the same agent's runs, when there are some)

    seconds = spread([r["play_seconds"] for r in same], [priors["play_seconds_per_episode"]] * 2)
    if episodes:
        planned, basis = int(episodes), "asked"
    elif minutes:
        per_episode = statistics.mean(seconds)
        planned, basis = max(1, math.ceil(float(minutes) * 60 / per_episode)), f"{minutes:g} minutes of play"
    elif same:
        planned, basis = max(1, round(statistics.median(r["episodes"] for r in same))), "earlier runs"
    else:
        planned, basis = priors["episodes"], "default"

    missing = []
    parts = {}
    price = input_price(model, table, fast)
    if price is None:
        missing.append(model)
        parts["agent"] = None
    else:
        units = spread([r["agent_units"] for r in same], priors["agent_units_per_episode"])
        parts["agent"] = tuple(u * price * planned for u in units)
    helper_price = input_price(helper, table)
    if helper_price is None:
        missing.append(helper)
        parts["reading"] = None
    else:
        units = spread([r["reading_units"] for r in runs], priors["reading_units_per_episode"])
        parts["reading"] = tuple(u * helper_price * planned for u in units)
    decisions = spread([r["decisions"] for r in runs], priors["decisions_per_episode"])
    cost = DECISION_COSTS.get(decision_provider)
    parts["decision"] = cost(decision, tuple(d * planned for d in decisions), tuple(priors["decision_tokens"]),
                             table) if cost else None
    if parts["decision"] is None:
        missing.append(decision)

    known = [p for p in parts.values() if p is not None]
    own = (sum(p[0] for p in known), sum(p[1] for p in known)) if known else None
    factor = table.get("aura", {}).get("factor", 1.0)
    return {
        "agent": agent, "provider": provider, "model": model, "fast": fast, "helper": helper,
        "decision_provider": decision_provider, "decision_model": decision,
        "episodes": planned, "episodes_basis": basis, "runs": len(same), "project_dir": project_dir,
        "parts": parts, "own_keys": own, "aura": tuple(x * factor for x in own) if own else None,
        "missing": missing, "prices_checked": table.get("updated"),
    }


def money(x):
    return f"${x:.3f}" if x < 0.1 else f"${x:.2f}"


def usd(pair):
    return "no price" if pair is None else f"{money(pair[0])} to {money(pair[1])}"


def text(e):
    """The estimate as the user reads it."""
    project = os.path.basename(e["project_dir"]) if e["project_dir"] else None
    history_words = (f"{e['runs']} earlier run(s) with cost data" if e["runs"] else "no earlier runs with cost data")
    lines = [
        "Cost estimate for one run (the run does not start)",
        f"  Project: {project} ({history_words})" if project else "  Project: not named (default amounts)",
        f"  Agent: {e['agent']}, model {e['model']}, fast mode {'on' if e['fast'] else 'off'}",
        f"  Reading model: {e['helper']}",
        f"  Decision model: {e['decision_model']} ({e['decision_provider']})",
        f"  Episodes: {e['episodes']} ({e['episodes_basis']})",
        "",
        "  Pay with your own keys (autopilot.env):",
        f"    {e['provider']}: agent {usd(e['parts']['agent'])}, reading {usd(e['parts']['reading'])}",
        f"    {e['decision_provider']}: decisions {usd(e['parts']['decision'])}",
        f"    Total: {usd(e['own_keys'])}",
        f"  Pay with Aura credits (--aura): {usd(e['aura'])}",
    ]
    if e["missing"]:
        lines.append(f"  No price for: {', '.join(e['missing'])}. Add it to autopilot/prices.json.")
    lines.append(f"  Prices from autopilot/prices.json (updated {e['prices_checked']}). This is a range, not a quote.")
    return "\n".join(lines)


def summary(e, aura=False):
    """One line for the plugin at the start of a run (the amount for the way the run pays, and no more: the plugins
    say nothing of credits in a run's status)."""
    return f"Cost estimate: {usd(e['aura'] if aura else e['own_keys'])}"


def limit():
    """AUTOPILOT_MAX_USD as a number, or None when it is not set (or not a number)."""
    try:
        value = float((os.environ.get("AUTOPILOT_MAX_USD") or "").strip())
    except ValueError:
        return None
    return value if value > 0 else None


def over_limit(e, aura=False, max_usd=None):
    """The words that say the run's high estimate is above the limit, or None when it is not (or there is no limit)."""
    max_usd = limit() if max_usd is None else max_usd
    pair = e["aura"] if aura else e["own_keys"]
    if max_usd is None or pair is None or pair[1] <= max_usd:
        return None
    return (f"The run can cost up to ${pair[1]:.2f}, which is more than AUTOPILOT_MAX_USD (${max_usd:.2f}). "
            "Raise the limit or make the run shorter, then start it again.")
