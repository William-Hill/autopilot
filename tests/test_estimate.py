"""The cost estimate (autopilot/estimate.py, `autopilot estimate`, `play --estimate`, AUTOPILOT_MAX_USD): prices from
prices.json, earlier runs turned into per-episode amounts, a range per part and per way to pay. No model, no network."""

import json
import os
import types

import pytest

from autopilot import cli
from autopilot import estimate as est

LOAD_USER_KEYS = cli.load_user_keys  # (the real one: tests/conftest.py replaces it with a no-op in every test)

# The reference run: renderatl-rush 20261107-165445, 2 episodes, claude on claude-opus-5-5 fast, Aura billed 3.06 USD
REFERENCE = {
    "run": "20261107-165445", "agent": "claude", "model": "claude-opus-5-5", "fast": True, "episodes": 2,
    "seconds": 252.3, "turns": 22, "cost_usd": 2.6558822400000004,
    "reading_usage": {"claude-sonnet-5-5": {"calls": 12, "input": 30, "output": 16888, "cache_read": 246967,
                                            "cache_write": 126305}},
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("AUTOPILOT_AGENT", "AUTOPILOT_MODEL", "AUTOPILOT_FAST", "AUTOPILOT_HELPER_MODEL", "AUTOPILOT_JEV_MODEL",
              "AUTOPILOT_OPENCODE_PROVIDER", "AUTOPILOT_MAX_USD", "AUTOPILOT_CONFIRM_COST"):
        monkeypatch.delenv(k, raising=False)


def write_run(project, report, decisions=(98, 89), seconds=(46.1, 41.7)):
    run = project / "runs" / report["run"]
    run.mkdir(parents=True)
    (run / "report.json").write_text(json.dumps(report))
    for n, (d, s) in enumerate(zip(decisions, seconds), 1):
        lines = [{"kind": "start", "t": 0.0}] + [{"kind": "decision", "t": s * i / d} for i in range(d)]
        lines.append({"kind": "end", "t": s})
        (run / f"trace{n}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    return run


def test_the_price_file_names_a_source_and_a_date_for_every_table():
    table = est.prices()
    assert table["updated"]
    for group in ("llm", "decision"):
        for provider in table[group].values():
            assert provider["source"].startswith("https://") and provider["checked"]
            assert provider["models"]
    assert 0 < table["aura"]["factor"] <= 2


def test_reading_usage_is_priced_per_token_kind():
    usd = est.usage_usd(REFERENCE["reading_usage"], est.prices())
    # 16888 out x $10 + 246967 cache read x $0.20 + 126305 cache write x $2.50 + 30 in x $2, per million
    assert usd == pytest.approx(0.534, abs=0.001)
    assert est.usage_usd({"some-unknown-model": {"input": 5}}, est.prices()) is None


def test_fast_mode_doubles_the_opus_input_price():
    table = est.prices()
    assert est.input_price("claude-opus-5-5", table, fast=True) == 2 * est.input_price("claude-opus-5-5", table)
    assert est.input_price("claude-sonnet-5-5", table, fast=True) == est.input_price("claude-sonnet-5-5", table)
    assert est.input_price("anthropic/claude-opus-5.5", table) == est.input_price("claude-opus-5-5", table)


def test_history_reads_reports_and_traces(tmp_path):
    write_run(tmp_path, REFERENCE)
    write_run(tmp_path, {"run": "20261101-000000", "episodes": 0, "cost_usd": None})  # (no cost data: left out)
    runs = est.history(str(tmp_path), est.prices())
    assert len(runs) == 1
    r = runs[0]
    assert r["agent_units"] == pytest.approx(2.6559 / 8 / 2, rel=1e-3)
    assert r["decisions"] == pytest.approx(93.5)
    assert r["play_seconds"] == pytest.approx(43.9)


def test_the_estimate_from_the_reference_run_contains_what_aura_billed(tmp_path):
    write_run(tmp_path, REFERENCE)
    e = est.estimate(str(tmp_path), agent="claude", model="claude-opus-5-5", fast=True)
    assert e["episodes"] == 2 and e["episodes_basis"] == "earlier runs" and e["runs"] == 1
    low, high = e["own_keys"]
    assert low < 2.66 + 0.534 < high  # (what the run's tokens cost at own-key prices)
    assert e["aura"][0] < 3.06 < e["aura"][1]  # (what Aura billed)
    assert e["parts"]["agent"][0] < 2.66 < e["parts"]["agent"][1]
    assert e["parts"]["decision"][1] < 0.1  # (Jev: input tokens only, at $0.042 per million)
    assert not e["missing"]


def test_a_cheaper_model_scales_the_same_history_down(tmp_path):
    write_run(tmp_path, REFERENCE)
    opus = est.estimate(str(tmp_path), agent="claude", model="claude-opus-5-5", fast=True)
    sonnet = est.estimate(str(tmp_path), agent="claude", model="claude-sonnet-5-5", fast=False)
    assert sonnet["parts"]["agent"][1] == pytest.approx(opus["parts"]["agent"][1] / 4)  # ($2 against $8 input)


def test_without_history_the_priors_give_a_range_and_episodes_scale_it(tmp_path):
    two = est.estimate(str(tmp_path / "none"), agent="claude", model="claude-opus-5-5", fast=True)
    four = est.estimate(None, agent="claude", model="claude-opus-5-5", fast=True, episodes=4)
    assert two["runs"] == 0 and two["episodes"] == 2 and two["episodes_basis"] == "default"
    assert two["own_keys"][0] < two["own_keys"][1]
    assert four["own_keys"][1] == pytest.approx(2 * two["own_keys"][1])
    minutes = est.estimate(None, agent="claude", model="claude-opus-5-5", minutes=3)
    assert minutes["episodes"] == 4  # (180 s of play at 45 s an episode)


def test_the_environment_picks_the_agent_model_and_fast_mode(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_AGENT", "claude")
    monkeypatch.setenv("AUTOPILOT_MODEL", "claude-opus-5-5")
    assert est.agent_setup()[1:] == ("anthropic", "claude-opus-5-5", True)
    monkeypatch.setenv("AUTOPILOT_FAST", "0")
    assert est.agent_setup()[3] is False
    monkeypatch.setenv("AUTOPILOT_HELPER_MODEL", "claude-haiku-5-5")
    assert est.estimate(None)["helper"] == "claude-haiku-5-5"


def test_a_model_without_a_price_is_named(monkeypatch):
    e = est.estimate(None, agent="codex", model="gpt-6.1-sol")
    assert e["parts"]["agent"] is None and "gpt-6.1-sol" in e["missing"]
    assert e["own_keys"] is not None  # (the parts with a price still add up)
    assert "No price for: gpt-6.1-sol" in est.text(e)


def test_a_new_decision_provider_adds_one_function(monkeypatch):
    monkeypatch.setitem(est.DECISION_COSTS, "other", lambda model, d, t, table: (1.0, 2.0))
    monkeypatch.setattr(est, "decision_model", lambda: ("other", "other-1"))
    e = est.estimate(None, agent="claude", model="claude-sonnet-5-5")
    assert e["parts"]["decision"] == (1.0, 2.0) and e["decision_provider"] == "other"


def test_the_limit_refuses_only_above_the_high_estimate(monkeypatch):
    e = est.estimate(None, agent="claude", model="claude-opus-5-5", fast=True)
    assert est.over_limit(e) is None  # (no limit set)
    monkeypatch.setenv("AUTOPILOT_MAX_USD", str(e["own_keys"][1] + 0.01))
    assert est.over_limit(e) is None
    monkeypatch.setenv("AUTOPILOT_MAX_USD", "0.5")
    assert "AUTOPILOT_MAX_USD ($0.50)" in est.over_limit(e)
    monkeypatch.setenv("AUTOPILOT_MAX_USD", "not a number")
    assert est.over_limit(e) is None


def test_estimate_command_prints_and_starts_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.codexmod, "MEMORY", str(tmp_path))
    write_run(tmp_path / "mygame", REFERENCE)
    monkeypatch.setattr(cli.bridge, "serve_local", lambda *a, **k: pytest.fail("no plugin connection"))
    monkeypatch.setattr(cli, "load_user_keys", lambda: None)  # (not this machine's autopilot.env)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(cli.aura, "enabled", lambda: False)
    for name in ("contain", "keep_responsive"):
        monkeypatch.setattr(cli.processes, name, lambda: None)
    for name in ("fix_at_start", "follow_preferences"):
        monkeypatch.setattr(cli.account, name, lambda: None)
    assert cli.main(["estimate", "--name", "mygame", "--agent", "claude", "--model", "claude-opus-5-5"]) == 0
    out = capsys.readouterr().out
    assert "Project: mygame (1 earlier run(s) with cost data)" in out
    assert "Pay with Aura credits (--aura)" in out and "Pay with your own keys" in out
    assert cli.main(["estimate", "--name", "mygame", "--agent", "claude", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["runs"] == 1
    assert cli.main(["play", "a prompt", "--estimate"]) == 0
    assert "Cost estimate for one run" in capsys.readouterr().out


class Link:
    hello = {"project": "mygame"}

    def __init__(self):
        self.said = []

    def status(self, text):
        self.said.append(text)


def test_a_run_above_the_limit_is_refused_unless_the_user_allows_it(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.codexmod, "MEMORY", str(tmp_path))
    monkeypatch.setattr(cli.aura, "enabled", lambda: False)
    link = Link()
    assert cli.cost_check(link) is None  # (no limit, nobody to ask: the plugin's runs)
    assert link.said and link.said[0].startswith("Cost estimate: $")
    monkeypatch.setenv("AUTOPILOT_MAX_USD", "0.01")
    assert "AUTOPILOT_MAX_USD" in cli.cost_check(link)
    assert cli.cost_check(link, confirm=lambda warning: True) is None
    assert "AUTOPILOT_MAX_USD" in cli.cost_check(link, confirm=lambda warning: False)


def test_every_play_run_asks_after_its_estimate_and_the_limit_is_a_warning_line(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.codexmod, "MEMORY", str(tmp_path))
    monkeypatch.setattr(cli.aura, "enabled", lambda: False)
    asked = []

    def answer(yes):
        return lambda warning: asked.append(warning) or yes

    assert cli.cost_check(Link(), confirm=answer(True)) is None
    assert cli.cost_check(Link(), confirm=answer(False)) == cli.NOT_CONFIRMED
    assert asked == [None, None]  # (under no limit: the question, with no warning)
    monkeypatch.setenv("AUTOPILOT_MAX_USD", "0.01")
    assert cli.cost_check(Link(), confirm=answer(True)) is None
    assert "AUTOPILOT_MAX_USD" in asked[-1]


def test_yes_and_the_environment_skip_the_question_but_not_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.codexmod, "MEMORY", str(tmp_path))
    monkeypatch.setattr(cli.aura, "enabled", lambda: False)
    never = lambda warning: pytest.fail("nobody is asked")  # noqa: E731
    for off in ("0", "no", "OFF", "false"):
        monkeypatch.setenv("AUTOPILOT_CONFIRM_COST", off)
        assert cli.cost_check(Link(), confirm=never) is None
    monkeypatch.setenv("AUTOPILOT_MAX_USD", "0.01")
    assert "AUTOPILOT_MAX_USD" in cli.cost_check(Link(), confirm=never)
    monkeypatch.setenv("AUTOPILOT_CONFIRM_COST", "1")  # (set, so the test's end takes back what main() sets)
    assert cli.confirm_wanted()
    monkeypatch.setattr(cli, "load_user_keys", lambda: None)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    for name in ("contain", "keep_responsive"):
        monkeypatch.setattr(cli.processes, name, lambda: None)
    for name in ("fix_at_start", "follow_preferences"):
        monkeypatch.setattr(cli.account, name, lambda: None)
    monkeypatch.setattr(cli, "cmd_estimate", lambda args: 0)
    assert cli.main(["play", "a prompt", "--estimate", "--yes"]) == 0
    assert not cli.confirm_wanted()


def test_the_bench_brain_starts_without_the_question():
    """bench/run_autopilot.py runs `autopilot play` with nobody at its terminal: it must skip the question."""
    with open(os.path.join(os.path.dirname(__file__), "..", "bench", "run_autopilot.py"), encoding="utf-8") as f:
        assert 'AUTOPILOT_CONFIRM_COST="0"' in f.read()


def test_a_failed_estimate_never_stops_a_run(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_MAX_USD", "0.01")
    monkeypatch.setattr(cli.costmod, "estimate", lambda *a, **k: 1 / 0)
    assert cli.cost_check(Link()) is None


def test_run_prompt_reports_a_refused_run_without_playing(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.codexmod, "MEMORY", str(tmp_path))
    monkeypatch.setattr(cli.aura, "enabled", lambda: False)
    monkeypatch.setattr(cli, "told_kind", lambda link, prompt: ("goal", False))
    monkeypatch.setattr(cli, "prepare", lambda *a, **k: pytest.fail("the run must not start"))
    monkeypatch.setenv("AUTOPILOT_MAX_USD", "0.01")
    sent = []
    link = types.SimpleNamespace(hello={"project": "mygame"}, status=lambda text: None, send=sent.append)
    cli.run_prompt(link, "reach the door", sent=[])
    result = next(f["result"] for f in sent if f.get("type") == "result")
    assert "AUTOPILOT_MAX_USD" in json.dumps(result)
    assert os.environ["AUTOPILOT_MAX_USD"] == "0.01"


# -- review fixes --------------------------------------------------------------------------------------------------


def test_the_text_before_a_real_run_does_not_say_the_run_does_not_start():
    e = est.estimate(None, agent="claude", model="claude-opus-5-5")
    assert est.text(e).startswith("Cost estimate for one run (the run does not start)")
    assert est.text(e, before_run=True).startswith("Cost estimate for this run\n")


def test_history_reads_only_the_newest_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(est, "HISTORY_RUNS", 2)
    for n in range(4):
        write_run(tmp_path, dict(REFERENCE, run=f"2026110{n}-000000"))
    assert [r["run"] for r in est.history(str(tmp_path), est.prices())] == ["20261103-000000", "20261102-000000"]


def test_runs_of_other_agents_are_used_and_named(tmp_path):
    write_run(tmp_path, REFERENCE)  # (a claude run)
    e = est.estimate(str(tmp_path), agent="opencode", model="claude-opus-5-5", fast=True)
    assert e["runs"] == 1 and e["other_agents"]
    assert "1 earlier run(s) with cost data, all of other agents" in est.text(e)
    assert not est.estimate(str(tmp_path), agent="claude")["other_agents"]


def run_prompt_estimates(monkeypatch, tmp_path, **kwargs):
    """The cost_check arguments run_prompt uses (None when it does not call it); the run itself never starts."""
    monkeypatch.setattr(cli.codexmod, "MEMORY", str(tmp_path))
    monkeypatch.setattr(cli, "prepare", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no game")))
    seen = []
    monkeypatch.setattr(cli, "cost_check", lambda link, **k: seen.append(k))
    link = types.SimpleNamespace(hello={"project": "mygame"}, status=lambda text: None, send=lambda frame: None)
    with pytest.raises(RuntimeError):
        cli.run_prompt(link, "explore the level", sent=[], **kwargs)
    return seen[0] if seen else None


def test_a_desk_run_is_not_checked_against_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.aura, "enabled", lambda: False)
    desk = types.SimpleNamespace(end=lambda report: None)
    assert run_prompt_estimates(monkeypatch, tmp_path, kind="goal", desk=desk) is None


def test_explore_minutes_count_only_for_an_exploration(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.aura, "enabled", lambda: False)
    assert run_prompt_estimates(monkeypatch, tmp_path, kind="goal", explore_minutes=20)["play_minutes"] is None
    assert run_prompt_estimates(monkeypatch, tmp_path, kind="explore", explore_minutes=20)["play_minutes"] == 20


def test_through_aura_the_estimate_uses_the_model_picked_in_aura(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.aura, "enabled", lambda: True)
    monkeypatch.setattr(cli, "start_aura_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no game")))
    args = run_prompt_estimates(monkeypatch, tmp_path, kind="goal", model="claude-opus-5-5",
                                aura_model="claude-sonnet-5-5")
    assert args["model"] == "claude-sonnet-5-5"


# -- the confirm prompt, the price table and history loading: every wait and every load has an exit -----------------


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))


def test_the_plugin_s_stop_ends_the_question_with_no(terminal):
    import threading

    never = threading.Event()
    stops = iter([False, False, True])
    assert cli.ask_on_terminal(None, read=lambda q: never.wait(5) or "y", stopped=lambda: next(stops)) is False
    frames = [{"type": "steer"}, {"type": "stop"}]
    link = types.SimpleNamespace(next_message=lambda timeout=None: frames.pop(0) if frames else None)
    stopped = cli.stop_pressed(link)
    assert stopped() is True and not frames
    assert stopped() is False
    gone = types.SimpleNamespace(next_message=lambda timeout=None: {"type": "closed"})
    assert cli.stop_pressed(gone)() is True  # (the game disconnected: nobody to play for)


def test_the_question_names_the_limit_only_above_it(terminal):
    seen = []
    cli.ask_on_terminal(None, read=lambda q: seen.append(q) or "n")
    cli.ask_on_terminal("The run can cost up to $9.00", read=lambda q: seen.append(q) or "n")
    assert seen == ["Start the run? [y/N] ", "The run can cost up to $9.00\nStart the run? [y/N] "]


def test_the_confirm_prompt_starts_the_run_only_on_yes(terminal):
    assert cli.ask_on_terminal("over", read=lambda q: "y") is True
    assert cli.ask_on_terminal("over", read=lambda q: "YES ") is True
    assert cli.ask_on_terminal("over", read=lambda q: "") is False
    assert cli.ask_on_terminal("over", read=lambda q: "n") is False


def test_the_confirm_prompt_says_no_on_ctrl_c_end_of_input_and_timeout(terminal):
    def raises(error):
        def read(q):
            raise error
        return read

    assert cli.ask_on_terminal("over", read=raises(KeyboardInterrupt())) is False
    assert cli.ask_on_terminal("over", read=raises(EOFError())) is False
    import threading

    never = threading.Event()
    assert cli.ask_on_terminal("over", seconds=0.05, read=lambda q: never.wait(5) or "y") is False


def test_the_confirm_prompt_says_no_without_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    assert cli.ask_on_terminal("over", read=lambda q: pytest.fail("no terminal to ask on")) is False
    assert "--yes" in capsys.readouterr().out


def test_history_reads_the_report_s_episode_stats_before_the_traces(tmp_path):
    run = write_run(tmp_path, dict(REFERENCE, episode_stats=[{"decisions": 50, "play_seconds": 30.0},
                                                             {"decisions": 70, "play_seconds": 50.0}]))
    for trace in run.glob("trace*.jsonl"):
        trace.unlink()  # (a new run: the report has them, the traces are not read)
    r = est.history(str(tmp_path), est.prices())[0]
    assert r["decisions"] == pytest.approx(60) and r["play_seconds"] == pytest.approx(40)


def test_history_reads_the_traces_of_an_older_run_or_damaged_stats(tmp_path):
    write_run(tmp_path, dict(REFERENCE, episode_stats=[{"decisions": "many"}]))
    r = est.history(str(tmp_path), est.prices())[0]
    assert r["decisions"] == pytest.approx(93.5)  # (from the traces, as test_history_reads_reports_and_traces)


def test_a_damaged_price_table_ends_the_estimate_command_with_an_error(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "prices.json"
    bad.write_text("{not json")
    monkeypatch.setattr(est, "PRICES", str(bad))
    monkeypatch.setattr(est.prices, "__defaults__", (str(bad),))
    args = types.SimpleNamespace(path=None, name=None, agent="claude", model=None, fast=None, episodes=None,
                                 minutes=None, json=False)
    assert cli.cmd_estimate(args) == 1
    assert "Check autopilot/prices.json" in capsys.readouterr().out


def test_damaged_and_unfinished_reports_are_left_out_of_history(tmp_path):
    write_run(tmp_path, REFERENCE)
    write_run(tmp_path, dict(REFERENCE, run="20261108-000000", cost_usd="2.5"))
    write_run(tmp_path, dict(REFERENCE, run="20261109-000000", episodes="2"))
    write_run(tmp_path, dict(REFERENCE, run="20261110-000000", reading_usage=["not", "a", "dict"]))
    (tmp_path / "runs" / "20261111-000000").mkdir()  # (a run still going: no report yet)
    (tmp_path / "runs" / "20261111-000000" / "trace1.jsonl").write_text('{"kind": "decision", "t": 1')
    damaged = tmp_path / "runs" / "20261112-000000"
    damaged.mkdir()
    (damaged / "report.json").write_text('{"cost_usd": 1.0, "episodes"')  # (written half)
    runs = est.history(str(tmp_path), est.prices())
    assert [r["run"] for r in runs] == ["20261110-000000", "20261107-165445"]
    assert runs[0]["reading_units"] is None and runs[0]["agent_units"] is not None


def test_a_trace_cut_off_mid_line_still_counts_its_whole_lines(tmp_path):
    run = write_run(tmp_path, REFERENCE, decisions=(10,), seconds=(20.0,))
    with open(run / "trace1.jsonl", "a") as f:
        f.write('\n{"kind": "decision", "t": 2')
    assert est.trace_numbers(str(run)) == [(10, 20.0)]


# -- each way to pay, with and without history, and a model with no price --------------------------------------------


def test_through_aura_the_estimate_prices_aura_s_agent_and_the_aura_total(monkeypatch):
    monkeypatch.setattr("autopilot.aura.enabled", lambda: True)
    monkeypatch.setenv("AUTOPILOT_AGENT", "codex")  # (Aura plays with its own agent, whatever this says)
    e = est.estimate(None, model="claude-opus-5-5")
    assert e["agent"] == "claude" and e["provider"] == "anthropic" and e["fast"]
    factor = est.prices()["aura"]["factor"]
    assert e["aura"] == pytest.approx(tuple(x * factor for x in e["own_keys"]))


def test_the_limit_reads_the_figure_of_the_way_the_run_pays(monkeypatch):
    e = {"own_keys": (1.0, 4.0), "aura": (1.0, 2.0)}
    monkeypatch.setenv("AUTOPILOT_MAX_USD", "3")
    assert est.over_limit(e, aura=False) and est.over_limit(e, aura=True) is None


def test_own_keys_split_into_the_provider_part_and_the_decision_part(tmp_path):
    for project in (None, str(tmp_path)):  # (without history, and with it)
        if project:
            write_run(tmp_path, REFERENCE)
        e = est.estimate(project, agent="claude", model="claude-opus-5-5", fast=True)
        parts = e["parts"]
        assert e["own_keys"] == pytest.approx(tuple(sum(p[i] for p in parts.values()) for i in (0, 1)))
        out = est.text(e)
        assert "anthropic: agent $" in out and "typesafe: decisions $" in out


def test_a_reading_model_with_no_price_is_named_and_left_out(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_HELPER_MODEL", "some-new-model")
    e = est.estimate(None, agent="claude", model="claude-opus-5-5")
    assert e["parts"]["reading"] is None and e["missing"] == ["some-new-model"]
    assert e["own_keys"][1] == pytest.approx(e["parts"]["agent"][1] + e["parts"]["decision"][1])


# -- the Codex review on William-Hill/autopilot#2 -------------------------------------------------------------------


def test_an_aura_run_of_a_kind_not_known_yet_is_estimated_with_its_exploration_minutes(tmp_path, monkeypatch):
    """Through Aura, the kind is known only after the run starts, and an `auto` run can become an exploration with all
    its minutes: the limit must see those minutes."""
    monkeypatch.setattr(cli.aura, "enabled", lambda: True)
    monkeypatch.setattr(cli, "start_aura_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no game")))
    assert run_prompt_estimates(monkeypatch, tmp_path, explore_minutes=20)["play_minutes"] == 20


def test_the_command_line_limit_and_yes_stay_after_autopilot_env_loads(monkeypatch):
    saved = {"AUTOPILOT_MAX_USD": "50", "AUTOPILOT_CONFIRM_COST": "1"}
    monkeypatch.setattr(cli.account, "user_settings", lambda: dict(saved))
    monkeypatch.setattr(cli, "CLI_SETTINGS", {"AUTOPILOT_MAX_USD": "1", "AUTOPILOT_CONFIRM_COST": "0"})
    monkeypatch.setenv("AUTOPILOT_MAX_USD", "1")
    monkeypatch.setenv("AUTOPILOT_CONFIRM_COST", "0")
    LOAD_USER_KEYS()
    assert os.environ["AUTOPILOT_MAX_USD"] == "1" and not cli.confirm_wanted()
    monkeypatch.setattr(cli, "CLI_SETTINGS", {})
    LOAD_USER_KEYS()  # (with no flags, autopilot.env decides)
    assert os.environ["AUTOPILOT_MAX_USD"] == "50" and cli.confirm_wanted()


def test_a_stop_that_comes_with_the_answer_still_refuses_the_run(terminal):
    stops = iter([True])  # (the answer is there before the first check; the Stop came with it)
    assert cli.ask_on_terminal(None, read=lambda q: "y", stopped=lambda: next(stops, True)) is False
    assert cli.ask_on_terminal(None, read=lambda q: "y", stopped=lambda: False) is True


def test_the_estimate_says_that_frame_descriptions_are_not_in_the_total():
    e = est.estimate(None, agent="claude", model="claude-opus-5-5")
    assert "Not included: frame descriptions (describe_frames)" in est.text(e)
