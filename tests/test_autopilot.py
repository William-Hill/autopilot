"""Autopilot's runtime, kit and agent tools against a simulated game (no Unreal, no models)."""

import json
import math
import os
import threading
import time
import types

import pytest
from conftest import wall_clock

from autopilot import runtime
from autopilot.kit import Harness, Option, Result, Status, Vec, go_and_press, walk_to, where
from autopilot.kit.game import Game, GameError


class FakeLink:
    """A flat world: the player walks at 400/s while W is held, the mouse turns the view 0.1 degree per count;
    pressing E within 150 of the crate opens it."""

    def __init__(self):
        self.pos = Vec(0, 0, 100)
        self.yaw = 0.0
        self.pitch = 0.0
        self.held = {}
        self.crate = Vec(800, 600, 100)
        self.opened = False
        self.paused = False
        self.closed = None
        self.game_time = 0.0
        self._wall = time.monotonic()
        self.lock = threading.Lock()
        self.inputs = []
        self.hello = {"project": "fake"}

    def _advance(self):
        now = time.monotonic()
        dt, self._wall = now - self._wall, now
        if self.paused:
            return
        self.game_time += dt
        if "W" in self.held:
            f = Vec(math.cos(math.radians(self.yaw)), math.sin(math.radians(self.yaw)), 0)
            self.pos = self.pos + f * (400 * dt)

    def post(self, op, args=None):
        self.request(op, args)

    def request_at(self, op, args=None, timeout=None):
        return self.request(op, args, timeout), self.game_time

    def request(self, op, args=None, timeout=None):
        from autopilot.bridge import BridgeError

        args = args or {}
        with self.lock:
            self._advance()
            if op == "context":
                return {"world": "/Game/M.M", "character": "/Game/M.M:PersistentLevel.Hero_C_0",
                        "controller": "/Game/M.M:PersistentLevel.PC_0",
                        "camera_manager": "/Game/M.M:PersistentLevel.Cam_0",
                        "game_time": self.game_time}
            if op == "pause":
                self.paused = args.get("paused", True)
                return {"paused": self.paused}
            if op == "session":
                self.pos, self.yaw, self.opened, self.held = Vec(0, 0, 100), 0.0, False, {}
                return {"running": True}
            if op == "key":
                self.inputs.append(dict(args))
                mode, key = args.get("mode"), args.get("key")
                if mode == "release_all":
                    self.held.clear()
                elif mode == "hold":
                    self.held[key] = args.get("value")
                elif mode == "release":
                    self.held.pop(key, None)
                elif key == "MouseX":
                    self.yaw = (self.yaw + 0.1 * args.get("value", 0)) % 360
                elif key == "MouseY":
                    self.pitch = max(-89, min(89, self.pitch + 0.1 * args.get("value", 0)))
                elif key == "E" and self.pos.dist2d(self.crate) < 150:
                    self.opened = True
                return {}
            if op == "view":
                return {"body": {"sonar": {"ways": []}}}
            if op == "rc":
                body = args["body"]
                obj, fn = body.get("objectPath"), body.get("functionName")
                if args["verb"] == "describe":
                    if obj in ("/Game/Crate.Crate_C", "/Script/Engine.Actor") or "Crate" in obj or "Hero" in obj:
                        return {"Name": obj, "Class": "x"}
                    raise BridgeError(400, f"Object {obj} does not exist")
                if args["verb"] == "property":
                    if obj.endswith("Crate_C_0") and body.get("propertyName") == "bOpened":
                        return {"bOpened": self.opened}
                    raise BridgeError(400, "no property")
                if fn == "GetAllActorsOfClass":
                    cls = body["parameters"]["ActorClass"]
                    if "Crate" in cls:
                        return {"OutActors": ["/Game/M.M:PersistentLevel.Crate_C_0"]}
                    if cls == "/Script/Engine.Actor":
                        return {"OutActors": ["/Game/M.M:PersistentLevel.Crate_C_0",
                                              "/Game/M.M:PersistentLevel.Hero_C_0"]}
                    return {"OutActors": []}
                if fn == "K2_GetActorLocation":
                    v = self.crate if "Crate" in obj else self.pos
                    return {"ReturnValue": v.ue()}
                if fn == "GetActorBounds":
                    v = self.crate if "Crate" in obj else self.pos
                    return {"Origin": v.ue(), "BoxExtent": {"X": 50, "Y": 50, "Z": 50}}
                if fn == "GetControlRotation":
                    return {"ReturnValue": {"Yaw": self.yaw, "Pitch": self.pitch % 360, "Roll": 0}}
                if fn == "GetCameraLocation":
                    return {"ReturnValue": (self.pos + Vec(0, 0, 60)).ue()}
                if fn == "GetVelocity":
                    return {"ReturnValue": {"X": 0, "Y": 0, "Z": 0}}
                raise BridgeError(400, f"no function {fn}")
            return {}


class FakeJev:
    """Prefers the option named `likes` when offered, else the first."""

    def __init__(self, likes):
        self.likes = likes
        self.calls = 0

    def system_one(self, state, questions):
        self.calls += 1
        crit = questions["pick"].criteria
        names = list(crit)
        best = self.likes if self.likes in names else names[0]
        probs = {n: (0.8 if n == best else 0.2 / max(1, len(names) - 1)) for n in names}
        choice = types.SimpleNamespace(choice=best, probabilities=probs, confidence=0.6)
        return types.SimpleNamespace(choices={"pick": choice}, nouls={}, scores={})


class CrateHarness(Harness):
    goal = "Open the crate"
    tempo = 2.0
    tick_hz = 30.0

    def setup(self, game):
        self.crate = game.actors("Crate")[0]

    def observe(self, game):
        pose, opened = game.batch([lambda: game.pose(), lambda: game.get(self.crate, "bOpened")])
        return {"_pose": pose, "opened": opened, "crate": where(pose, game.loc(self.crate))}

    def options(self, obs):
        return [
            Option("open_crate", "Walk to the crate and open it",
                   lambda g, o: go_and_press(g, self.crate, "E", within=120),
                   facts={"crate": obs["crate"]}, effect=lambda a, b: b["opened"]),
            Option("wait", "Stand still", lambda g, o: (x for x in range(5))),
        ]

    def status(self, obs):
        return Status("done", "opened", {"opened": True}) if obs["opened"] else Status()


@pytest.fixture
def game():
    link = FakeLink()
    g = Game(link)
    g.look = {"x": 0.1, "y": 0.1}
    return g


def test_walk_to_reaches_target(game):
    gen = walk_to(game, Vec(500, -400, 100), within=60, timeout=8)
    t0 = time.monotonic()
    while True:
        game.renew()
        try:
            next(gen)
        except StopIteration as s:
            res = s.value
            break
        time.sleep(1 / 30)
    assert res.ok, res
    assert game.link.pos.dist2d(Vec(500, -400, 0)) < 80
    assert "W" not in game.link.held  # let go at the end
    assert time.monotonic() - t0 < 6


def test_runtime_plays_harness_to_done(game, tmp_path):
    jev = FakeJev("open_crate")
    rt = runtime.Runtime(game, CrateHarness(), str(tmp_path / "t.jsonl"), client=jev, max_seconds=20)
    res = rt.run()
    assert res["status"]["state"] == "done", res
    assert res["options"]["open_crate"]["started"] >= 1
    assert jev.calls >= 1
    text = runtime.summarize(str(tmp_path / "t.jsonl"))
    assert "open_crate" in text and "status=done" in text


def test_runtime_reports_harness_errors(game, tmp_path):
    class Broken(CrateHarness):
        def observe(self, game):
            raise KeyError("nope")

    rt = runtime.Runtime(game, Broken(), str(tmp_path / "t.jsonl"), client=FakeJev("x"), max_seconds=5)
    res = rt.run()
    assert "observe kept failing" in res["reason"]
    assert "KeyError" in runtime.summarize(str(tmp_path / "t.jsonl"))


def test_switch_needs_margin(game, tmp_path):
    """A running option is kept unless the new pick is preferred by the margin."""
    h = CrateHarness()
    h.setup(game)
    rt = runtime.Runtime(game, h, str(tmp_path / "t.jsonl"), client=None, max_seconds=5)
    obs = {"_pose": game.pose(), "opened": False, "crate": {}}
    opts = h.options(obs)
    rt.start(opts[0], obs, p=0.5)
    assert rt.running.option.name == "open_crate"
    rt.apply({"open_crate": 0.45, "wait": 0.55}, {}, 0.1, opts, obs, 0.0, {})
    assert rt.running.option.name == "open_crate"
    rt.apply({"open_crate": 0.2, "wait": 0.8}, {}, 0.1, opts, obs, 0.0, {})
    assert rt.running.option.name == "wait"


def test_load_harness_from_file(tmp_path):
    p = tmp_path / "harness.py"
    p.write_text("from autopilot.kit import *\nclass A(Harness):\n    goal='x'\nclass B(A):\n    goal='y'\n")
    h = runtime.load_harness(str(p))
    assert h.goal == "y"


def test_resolve_class_by_codex_and_name(game, tmp_path):
    from autopilot.codex import Codex

    bp = tmp_path / "codex" / "blueprints" / "Props"
    bp.mkdir(parents=True)
    (bp / "Crate.txt").write_text("Blueprint /Game/Props/Crate (parent: Actor; generated class /Game/Crate.Crate_C)\n")
    game.codex = Codex(str(tmp_path / "codex"))
    assert game.resolve_class("Crate") == "/Game/Crate.Crate_C"
    assert game.actors("Crate") == ["/Game/M.M:PersistentLevel.Crate_C_0"]
    assert game.named("hero") == ["/Game/M.M:PersistentLevel.Hero_C_0"]
    with pytest.raises(GameError):
        game.resolve_class("Nothing")


def test_probe_tool(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    s = session.Session(game, "fake", "Open the crate", "r1")
    out = s.probe("print(len(game.all_actors()))\ngame.loc(game.actors('Crate')[0])")
    assert "2" in out and "800" in out
    out = s.probe("raise ValueError('boom')")
    assert "ValueError: boom" in out
    assert game.link.paused  # paused again after the probe


def test_run_harness_tool(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    s = session.Session(game, "fake", "Open the crate", "r1", client=FakeJev("open_crate"))
    src = open(__file__, encoding="utf-8").read()
    # the harness file: this test module's CrateHarness, standalone
    start = src.index("class CrateHarness")
    end = src.index("@pytest.fixture")
    (tmp_path / "fake" / "runs" / "r1" / "harness.py").write_text(
        "from autopilot.kit import *\n\n" + src[start:end], encoding="utf-8")
    # (each episode its own video: the recorder told when it starts and when it has ended)
    told = []
    recorder = types.SimpleNamespace(begin=lambda n: told.append(("begin", n)), end=lambda n: told.append(("end", n)))
    s.describer = types.SimpleNamespace(recorder=recorder)
    out = s.run_harness(15)
    assert "status=done" in out, out
    assert os.path.exists(tmp_path / "fake" / "runs" / "r1" / "harness1.py")
    s.run_harness(15)
    assert told == [("begin", 1), ("end", 1), ("begin", 2), ("end", 2)]


def test_work_after_an_episode_is_looking_at_its_results(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    said = []
    s = session.Session(game, "fake", "Open the crate", "r1", say=said.append, client=FakeJev("open_crate"))
    harness = tmp_path / "fake" / "runs" / "r1" / "harness.py"
    harness.write_text("raise ValueError('broken')\n", encoding="utf-8")
    assert "doesn't load" in s.run_harness(15)  # (nothing played: reading is still reading)
    for line in (session.READING, session.PROBING, session.WRITING):
        s.say(line)
    assert said == [session.READING, session.PROBING, session.WRITING]
    src = open(__file__, encoding="utf-8").read()
    crate = src[src.index("class CrateHarness"):src.index("@pytest.fixture")]
    harness.write_text("from autopilot.kit import *\n\n" + crate, encoding="utf-8")
    del said[:]
    s.run_harness(15)
    # (as a live run goes: a probe of what it saw, a look at the code, the harness fixed, played again, notes written)
    for line in (session.PROBING, session.READING, session.WRITING, session.PROBING):
        s.say(line)
    s.run_harness(15)
    s.say(session.WRITING)
    plain = [t for t in said if not t.startswith("Now: ")]
    # (every one of those lines is RESULTS, said once: the same stage line isn't sent twice running)
    assert plain == ["Trying the harness (run 2)", session.RESULTS, "Trying the harness (run 3)", session.RESULTS], said


def test_a_continued_episode_holds_the_game_paused_until_the_player_is_driven(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    s = session.Session(game, "fake", "Wait", "r1", client=FakeJev("stand"))
    (tmp_path / "fake" / "runs" / "r1" / "harness.py").write_text('''from autopilot.kit import *

SEEN = {}


class Holding(Harness):
    tick_hz = 30.0

    def setup(self, game):
        SEEN["setup"] = game.link.paused

    def observe(self, game):
        SEEN.setdefault("first_observe", game.link.paused)
        return {"t": game.t}

    def stand(self, game, obs):
        SEEN.setdefault("first_step", game.link.paused)
        yield from wait(game, 0.3)

    def options(self, obs):
        return [Option("stand", "Stand", self.stand), Option("look", "Look", self.stand)]

    def status(self, obs):
        return Status("done", "stood") if "first_step" in SEEN else Status()
''', encoding="utf-8")
    game.link.paused = False  # (as play left it)
    out = s.run_harness(5, restart=False)
    assert "status=done" in out, out
    import sys
    seen = next(m.SEEN for name, m in list(sys.modules.items()) if name.startswith("harness_") and hasattr(m, "SEEN"))
    assert seen == {"setup": True, "first_observe": True, "first_step": False}, seen
    events = [json.loads(line) for line in open(tmp_path / "fake" / "runs" / "r1" / "trace1.jsonl", encoding="utf-8")]
    assert [e["option"] for e in events if e["kind"] == "resumed"] == ["stand"]
    assert game.link.paused  # (paused again after the episode, as ever)


def test_cli_prompt_and_result_frames():
    from autopilot import cli

    assert cli.prompt_of({"kind": "verify", "claim": "Verify that X"}) == "Verify that X"
    assert cli.prompt_of({"kind": "goal", "goal": "Win"}) == "Win"
    assert "Explore" in cli.prompt_of({"kind": "explore", "minutes": 5})
    f = cli.result_frame({"outcome": "DENIED", "summary": "no", "evidence": ["e"]}, "Verify that X")
    assert f["verdict"] == "DENIED" and f["exit_code"] == 2 and f["claim"] == "Verify that X"
    f = cli.result_frame({"outcome": "achieved", "summary": "yes", "evidence": ["e"]}, "Win")
    assert f["kind"] == "goal" and f["reached"] and f["exit_code"] == 0 and f["notes"][0] == "yes"
    assert f["changes"] == []
    # the run's kind decides the shape: a goal worded as a claim, reported CONFIRMED, is a goal reached
    confirm = "Confirm the player spawns as the stock character"
    f = cli.result_frame({"outcome": "CONFIRMED", "summary": "it does", "evidence": ["e"]}, confirm, "goal")
    assert f["kind"] == "goal" and f["reached"] and f["goal"] == confirm and f["notes"] == ["it does", "e"]
    assert f["exit_code"] == 0
    f = cli.result_frame({"outcome": "DENIED", "summary": "it doesn't", "evidence": []}, confirm, "goal")
    assert f["kind"] == "goal" and not f["reached"] and f["notes"] == ["it doesn't"]
    assert "verdict" in cli.result_frame({"outcome": "CONFIRMED", "summary": "", "evidence": []}, confirm)
    # and a claim reported as a goal is a verdict
    f = cli.result_frame({"outcome": "achieved", "summary": "yes", "evidence": []}, "Doors open", "verify")
    assert f["verdict"] == "CONFIRMED" and f["exit_code"] == 0 and f["claim"] == "Doors open"
    f = cli.result_frame({"outcome": "not_achieved", "summary": "no", "evidence": []}, "Doors open", "verify")
    assert f["verdict"] == "INCONCLUSIVE" and f["exit_code"] == 3
    f = cli.result_frame({"outcome": "explored", "summary": "no bugs", "evidence": []}, "Explore", "explore")
    assert f["kind"] == "goal" and f["exit_code"] == 0 and f["notes"] == ["no bugs"]
    # the direct changes reach the result, in the plugin's words
    writes = [{"t": 1.5, "object": "/Game/Map.Map:PersistentLevel.Wolf_2", "name": "Wolf_2", "property": "bHidden",
               "value": True, "why": "the wolf blocks Jev's path"},
              {"t": 2.0, "object": "/Game/Map.Map:PersistentLevel.Wolf_3", "name": "Wolf_3", "call": "K2_DestroyActor",
               "parameters": {}},
              {"t": 3.0, "object": "/Script/Engine.Default__GameplayStatics", "call": "SetGlobalTimeDilation",
               "parameters": {"TimeDilation": 0.5}}]
    f = cli.result_frame({"outcome": "CONFIRMED", "summary": "yes", "evidence": [], "level_modified": writes},
                         "Verify that X")
    assert f["changes"][0] == dict(writes[0], why="the wolf blocks the agent's path",
                                   text="Set Wolf_2.bHidden to true (the wolf blocks the agent's path)")
    assert f["changes"][1] == dict(writes[1], text="Called Wolf_3.K2_DestroyActor")
    assert f["changes"][2]["text"] == ('Called /Script/Engine.Default__GameplayStatics.SetGlobalTimeDilation with '
                                       '{"TimeDilation": 0.5}')  # (no name: the path)
    assert writes[0]["why"] == "the wolf blocks Jev's path"  # (the report itself is left as it was)
    assert len(cli.change_text({"name": "A", "property": "P", "value": "x" * 500})) == 200


def test_a_prompts_kind_is_read_from_its_first_words_when_jev_cant_be_asked():
    from autopilot import cli

    kinds = {"Verify that doors open": "verify", "check the jump height": "verify", "Can the player swim?": "verify",
             "is it true that crops grow": "verify", "Explore": "explore",
             "explore the game for bugs: farm area": "explore", "Find bugs in the shop": "explore",
             "Playtest the boss fight": "explore", "Reach the castle gate": "goal", "Sell three crops": "goal",
             "Make a sandwich": "goal", "Checkpoint 3: reach it": "goal"}
    assert {prompt: cli.kind_by_words(prompt) for prompt in kinds} == kinds


def test_jev_reads_a_prompts_kind(no_jev_kinds):
    from autopilot import cli

    classify = no_jev_kinds  # (the real one)
    asked = []

    class Jev:
        def __init__(self, picks):
            self.picks = picks

        def system_one(self, state, questions, **k):
            asked.append((state, questions, k))
            choice = types.SimpleNamespace(choice=self.picks)
            return types.SimpleNamespace(choices={"kind": choice})

    prompt = ("Lvl_HordeArea test this game and find all the bugs with animation and everything you see in level, "
              "make a complete report with proper fixes.")
    assert classify(prompt, Jev("test")) == ("explore", True)  # (a test plays as an exploration)
    state, questions, k = asked[0]
    assert state == {"request": prompt} and set(questions["kind"].criteria) == {"verify", "goal", "explore", "test"}
    assert k["timeout"] == cli.KIND_TIMEOUT
    assert classify("Reach the test chamber", Jev("goal")) == ("goal", False)
    assert classify("Can the player swim?", Jev("verify")) == ("verify", False)
    assert classify("Find bugs in the shop", Jev("explore")) == ("explore", False)

    class Down:
        def system_one(self, *a, **k):
            raise ConnectionError("no network")

    # Jev not there (or answering what it was never offered): the first words decide, and it is never a test
    assert classify("Find bugs in the shop", Down()) == ("explore", False)
    assert classify("Lvl_HordeArea test this game", Down()) == ("goal", False)
    assert classify("Verify that doors open", Jev("maybe")) == ("verify", False)
    # the agent reads a claim as one: starting with Verify
    assert cli.as_claim("Can the player swim?") == "Verify: Can the player swim?"
    assert cli.as_claim("Verify that doors open") == "Verify that doors open"


def test_what_a_run_added_to_the_memory(tmp_path):
    from autopilot import memory

    before = memory.snapshot(str(tmp_path))
    assert memory.learned(str(tmp_path), before, "r1") == {"updated": False, "notes": False, "lib": False}
    (tmp_path / "notes.md").write_text("Jump is Space")
    assert memory.learned(str(tmp_path), before, "r1") == {"updated": True, "notes": True, "lib": False}
    before = memory.snapshot(str(tmp_path))
    (tmp_path / "runs" / "r2").mkdir(parents=True)
    (tmp_path / "runs" / "r2" / "harness.py").write_text("")
    assert memory.learned(str(tmp_path), before, "r2") == {"updated": True, "notes": False, "lib": False}


def test_direct_changes_are_kept_with_the_objects_name(game):
    from autopilot.kit.game import GameError

    crate = "/Game/Map.Map:PersistentLevel.Crate_C_0"
    game.set(crate, "bOpened", True, why="start open")
    try:
        game.call(crate, "K2_DestroyActor")
    except GameError:
        pass  # (the fake game has no such function: the change is kept all the same, as it was asked for)
    assert [(w["name"], w.get("property") or w.get("call")) for w in game.writes] == [
        ("Crate_C_0", "bOpened"), ("Crate_C_0", "K2_DestroyActor")]


def test_findings_reach_the_trace(game, tmp_path):
    game.link.listeners = []
    jev = FakeJev("open_crate")
    rt = runtime.Runtime(game, CrateHarness(), str(tmp_path / "t.jsonl"), client=jev, max_seconds=20)
    h = rt.h
    orig = h.setup

    def setup(g):
        orig(g)
        for listener in g.link.listeners:
            listener({"kind": "finding", "t": 1.0, "finding": {"detector": "hud_fraction", "summary": "HUD shows 6/5"}})
            listener({"kind": "log", "t": 1.2, "verbosity": "Error", "category": "LogScript",
                      "message": "Accessed None"})

    h.setup = setup
    rt.run()
    text = runtime.summarize(str(tmp_path / "t.jsonl"))
    assert "HUD shows 6/5" in text and "Accessed None" in text
    assert game.link.listeners == []
    assert "no errors logged" not in runtime.summarize(str(tmp_path / "t.jsonl"), error_log=True)


def test_summary_says_when_no_errors_were_logged(game, tmp_path):
    rt = runtime.Runtime(game, CrateHarness(), str(tmp_path / "t.jsonl"), client=FakeJev("open_crate"),
                         max_seconds=20)
    rt.run()
    path = str(tmp_path / "t.jsonl")
    assert "no errors logged during this episode" in runtime.summarize(path, error_log=True)
    # (an engine whose plugin doesn't send the game's errors: the summary can't tell)
    assert "error log" not in runtime.summarize(path)


def test_live_link_follows_reconnects():
    from autopilot.cli import LiveLink

    a, b = types.SimpleNamespace(name="a"), types.SimpleNamespace(name="b")
    state = {"current": a}
    a.server_state = state
    live = LiveLink(a)
    assert live.name == "a"
    state["current"] = b  # the plugin reconnected
    assert live.name == "b"


def test_report_needs_an_episode(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    s = session.Session(game, "fake", "Verify that X", "r1")
    assert "Not reported" in s.do_report({"outcome": "CONFIRMED", "summary": "x", "evidence": []})
    assert s.report is None
    assert "Reported" in s.do_report({"outcome": "INCONCLUSIVE", "summary": "x", "evidence": []})
    s.report, s.episodes, s.most_decisions = None, 2, 1  # played, but Jev hardly chose anything
    assert "Not reported" in s.do_report({"outcome": "CONFIRMED", "summary": "x", "evidence": []})
    s.most_decisions = 5
    assert "Reported" in s.do_report({"outcome": "CONFIRMED", "summary": "x", "evidence": []})


def test_clock_nudges_toward_a_report(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    s = session.Session(game, "fake", "Win", "r1")
    s.budget = 100.0
    assert "Wrap up" not in s.clock()
    s.started -= 75
    assert "Wrap up" in s.clock()
    s.started -= 20
    assert "nearly up" in s.clock()


def test_play_exits_only_once_the_runs_data_is_sent(monkeypatch):
    """`autopilot play` waits for what its run left going (the memory saved with Aura, the shared data sent): once
    the main thread ends, a thread pool takes no work, and the upload failed ("cannot schedule new futures after
    interpreter shutdown")."""
    from autopilot import cli

    done = threading.Event()

    def run_prompt(link, prompt, path, model, sent=None, timeout=None, level=None, confirm_cost=None):
        def upload():
            time.sleep(0.3)
            done.set()

        thread = threading.Thread(target=upload, daemon=False)
        thread.start()
        cli.AFTER_RUN.append(thread)
        return 0

    monkeypatch.setattr(cli, "AFTER_RUN", [])
    monkeypatch.setattr(cli, "run_prompt", run_prompt)
    link = types.SimpleNamespace(hello={})
    assert cli.cmd_play(link, types.SimpleNamespace(prompt="Win", path=None, model=None)) == 0
    assert done.is_set()


def test_a_local_brain_answers_a_command_line_run_asked_again_with_its_result(monkeypatch):
    """python -m autopilot play|serve: a plugin that lost the result of its command-line run and connects again with the
    same hello gets that result again (never a second run); another run, a stale one, or the connection the result
    went to already, nothing."""
    from autopilot import bridge, cli

    sent = []
    first, second = types.SimpleNamespace(), types.SimpleNamespace()
    link = types.SimpleNamespace(current=second, send=sent.append)
    run = {"kind": "goal", "goal": "autopilot"}
    frame = {"type": "result", "result": {"exit_code": 0}}
    done = [(time.time(), run, frame, first)]
    assert not cli.answer_again(link, {"type": "hello", "run": {"kind": "goal", "goal": "other"}}, done)
    assert not cli.answer_again(link, {"type": "hello"}, done) and not sent
    assert cli.answer_again(link, {"type": "hello", "run": {"goal": "autopilot", "kind": "goal"}}, done)
    assert [f["type"] for f in sent] == ["status", "result"] and sent[-1] == frame
    sent.clear()
    assert not cli.answer_again(link, {"type": "hello", "run": run}, [(time.time(), run, frame, second)])
    monkeypatch.setattr(bridge, "REPEAT_SECONDS", 0)
    assert not cli.answer_again(link, {"type": "hello", "run": run}, done) and not sent


def test_fast_mode_is_on_unless_turned_off_and_only_for_opus(monkeypatch):
    from autopilot.agents import claude_code

    # (on by default: the plugins, the agent service and Aura's brain set nothing)
    monkeypatch.delenv("AUTOPILOT_FAST", raising=False)
    assert claude_code.fast_settings("claude-opus-5-5") == '{"fastMode": true}'
    assert claude_code.fast_settings("claude-sonnet-5-5") is None
    for on in ("1", "", "yes"):
        monkeypatch.setenv("AUTOPILOT_FAST", on)
        assert claude_code.fast_settings("claude-opus-5-5") == '{"fastMode": true}'
    # (read at each run, as autopilot.env says it then: the agent service loads it after this module is imported)
    monkeypatch.setenv("AUTOPILOT_FAST", "0")
    assert claude_code.fast_settings("claude-opus-5-5") is None


def test_claude_code_gets_its_key_and_no_other_secret(monkeypatch, tmp_path):
    from autopilot.agents import claude_code

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    env = claude_code._env(str(tmp_path))
    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert env["TYPESAFE_API_KEY"] == ""


def test_sandbox_runs_model_code_in_a_child_without_secrets(game, tmp_path, monkeypatch):
    """Probes and episodes run in a child process with no secrets; its game requests, Jev questions and episode
    videos (the run's recorder) are served by this (the parent) process."""
    from autopilot import codex, sandbox

    monkeypatch.setenv("AUTOPILOT_SANDBOX", "process")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-secret-value-123456")
    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    root = os.path.join(str(tmp_path), "fake")
    run_dir = os.path.join(root, "runs", "r1")
    os.makedirs(run_dir)
    src = open(__file__, encoding="utf-8").read()
    harness = src[src.index("class CrateHarness"):src.index("@pytest.fixture")]
    open(os.path.join(run_dir, "harness.py"), "w", encoding="utf-8").write("from autopilot.kit import *\n\n" + harness)
    jev = FakeJev("open_crate")
    said = []
    monkeypatch.setattr(game.link, "status", said.append, raising=False)
    marks = []

    class Recorder:
        def begin(self, n):
            marks.append(("begin", n))

        def end(self, n):
            marks.append(("end", n))

    sb = sandbox.Sandbox(game.link, jev, "fake", "Open the crate", "r1", root, str(tmp_path), recorder=Recorder())
    try:
        out = sb.call("probe", code="import os\nprint(os.environ.get('TYPESAFE_API_KEY'))\nlen(game.all_actors())",
                      timeout=60)
        assert "None" in out and "ts-secret" not in out and "2" in out
        out = sb.call("run_harness", seconds=15, timeout=120)
        assert "status=done" in out, out
        assert jev.calls >= 1  # the child's questions were asked by the parent's client
        state = sb.call("state", timeout=30)
        assert state["episodes"] == state["played"] == 1
        assert marks == [("begin", 1), ("end", 1)]  # (the episode the child played has its video, a probe none)
        # (the child's progress reaches the plugin in the user's words: never "Jev")
        assert any("Trying the harness" in t for t in said), said
        assert said[-1] == "Looking at the results", said  # (said once, by the child that played)
        assert not any("Jev" in t for t in said), said
    finally:
        sb.close()


def test_codex_paths_never_leave_the_codex():
    from autopilot.engines.unreal import codex_path

    assert codex_path("Source/Game/Door.cpp") == "source/Game/Door.cpp"
    assert codex_path("Config/DefaultInput.ini") == "config/DefaultInput.ini"
    assert codex_path("Saved/AutopilotCodex/blueprints/BP_Door.txt") == "blueprints/BP_Door.txt"
    assert codex_path("Game.uproject") == "Game.uproject"
    for bad in ("../x.cpp", "Source/../../x", "/etc/passwd", "C:/Windows/win.ini", "Source\\x.cpp",
                "Saved/Logs/game.log", "Config/sub/x.ini", ".env", "", None):
        assert codex_path(bad) is None, bad


def test_fetch_from_plugin_reads_only_what_changed(tmp_path):
    from autopilot.codex import fetch_from_plugin

    files = {"Source/G/A.cpp": "int a;", "Config/DefaultGame.ini": "[x]", "Saved/AutopilotCodex/summary.txt": "ok",
             "../evil.txt": "no"}
    hashes = {p: f"h{len(t)}" for p, t in files.items()}
    reads = []

    class FakeGame:
        def request(self, op, args, timeout=None):
            if op == "codex_manifest":
                return {"files": [{"path": p, "size": len(t), "hash": hashes[p]} for p, t in files.items()],
                        "export": {"ran": False}}
            reads.append(list(args["paths"]))
            return {"files": {p: files[p] for p in args["paths"] if p in files}}

    root = str(tmp_path / "codex")
    os.makedirs(root)
    stats = fetch_from_plugin(FakeGame(), root)
    assert stats["files"] == 3 and stats["fetched"] == 3
    assert open(os.path.join(root, "source", "G", "A.cpp")).read() == "int a;"
    assert not os.path.exists(str(tmp_path / "evil.txt"))
    reads.clear()
    files["Source/G/A.cpp"] = "int a = 2;"
    hashes["Source/G/A.cpp"] = "changed"
    del files["Config/DefaultGame.ini"]
    stats = fetch_from_plugin(FakeGame(), root)
    assert reads == [["Source/G/A.cpp"]] and stats["removed"] == 1
    assert not os.path.exists(os.path.join(root, "config", "DefaultGame.ini"))


class DestroyingLink(FakeLink):
    """The crate's actor can be destroyed: it still describes and reads (as Unreal's do until garbage collection),
    and its flag says it is being destroyed."""

    destroyed = False

    def request(self, op, args=None, timeout=None):
        body = (args or {}).get("body") or {}
        if op == "rc" and args.get("verb") == "property" and body.get("propertyName") == "bActorIsBeingDestroyed":
            if body["objectPath"].endswith("Crate_C_0"):
                return {"bActorIsBeingDestroyed": self.destroyed}
            if body["objectPath"].endswith("Hero_C_0"):
                return {"bActorIsBeingDestroyed": False}
        return super().request(op, args, timeout)


def test_a_destroyed_actor_and_its_components_dont_exist():
    from autopilot.engines.unreal import owner_actor

    link = DestroyingLink()
    game = Game(link)
    crate = "/Game/M.M:PersistentLevel.Crate_C_0"
    assert owner_actor(crate + ".Lid") == crate and owner_actor(crate) == crate
    assert owner_actor("/Game/Crate.Crate_C") is None
    assert game.exists(crate) and game.exists(crate + ".Lid") and game.exists("/Game/Crate.Crate_C")
    assert not game.exists("/Game/M.M:PersistentLevel.Ghost_C_0")
    link.destroyed = True
    assert game.describe(crate)  # still readable...
    assert not game.exists(crate) and not game.exists(crate + ".Lid")  # ...but gone


def test_sees_always_says_whether_the_crosshair_is_on():
    class SeeingLink(FakeLink):
        def request(self, op, args=None, timeout=None):
            if op == "view":
                return {"visible": [{"name": "Crate_C_0", "distance": 500, "screen_x": 0.1, "screen_y": 0.0}]}
            return super().request(op, args, timeout)

    game = Game(SeeingLink())
    crate = "/Game/M.M:PersistentLevel.Crate_C_0"
    assert game.sees([crate])[crate] == {"distance": 500, "screen_x": 0.1, "screen_y": 0.0, "crosshair_on": False}


class LidHarness(Harness):
    """An option that isn't offered any more once it has begun (as an "open" option is gone once opening starts)."""
    goal = "Open the lid"
    tempo = 5.0
    tick_hz = 30.0

    def setup(self, game):
        self.started = self.opened = False

    def observe(self, game):
        return {"opened": self.opened}

    def open_lid(self, game, obs):
        self.started = True
        for _ in range(10):
            yield
        self.opened = True
        return Result(True, "lid open")

    def options(self, obs):
        opts = [Option("wait", "Stand still", lambda g, o: (x for x in range(3)))]
        if not self.started:
            opts.insert(0, Option("open_lid", "Open the lid", self.open_lid))
        return opts

    def status(self, obs):
        return Status("done", "opened") if obs["opened"] else Status()


def test_a_running_option_the_harness_stops_listing_runs_to_its_end(game, tmp_path):
    rt = runtime.Runtime(game, LidHarness(), str(tmp_path / "t.jsonl"), client=FakeJev("open_lid"), max_seconds=10)
    res = rt.run()
    assert res["status"]["state"] == "done", res
    assert res["options"]["open_lid"].get("worked") == 1 and not res["options"]["open_lid"].get("interrupted")


def test_jev_can_still_switch_from_an_option_no_longer_listed(game, tmp_path):
    rt = runtime.Runtime(game, LidHarness(), str(tmp_path / "t.jsonl"), client=FakeJev("wait"), max_seconds=2)
    h = rt.h
    h.setup(game)
    obs = h.observe(game)
    opts = h.options(obs)
    rt.start(opts[0], obs, p=0.5)
    h.started = True  # (as its first step does)
    now = h.options(obs)  # open_lid isn't listed any more
    assert [o.name for o in now] == ["wait"]
    rt.apply({"wait": 0.55, "open_lid": 0.45}, {}, 0.1, now + [rt.running.option], obs, 0.0, {})
    assert rt.running.option.name == "open_lid"  # inside the margin: kept
    rt.apply({"wait": 0.9, "open_lid": 0.1}, {}, 0.1, now + [rt.running.option], obs, 0.0, {})
    assert rt.running.option.name == "wait"


def test_the_implementer_hears_how_many_decisions_a_report_needs(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    assert f"at least {session.MIN_JEV_DECISIONS} decisions" in session.system_prompt(game.engine, "runs/r1")
    assert "{min_decisions}" not in session.system_prompt(game.engine, "runs/r1")
    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    s = session.Session(game, "fake", "Open the crate", "r1", client=FakeJev("open_crate"))
    # one option: it runs without asking Jev, so the episode has no decisions
    (tmp_path / "fake" / "runs" / "r1" / "harness.py").write_text(
        "from autopilot.kit import *\n\n"
        "class One(Harness):\n"
        "    goal = 'Stand'\n"
        "    def observe(self, game):\n"
        "        return {}\n"
        "    def options(self, obs):\n"
        "        return [Option('stand', 'Stand still', lambda g, o: (x for x in range(3)))]\n", encoding="utf-8")
    out = s.run_harness(2)
    assert "report needs an episode with at least" in out, out
    assert "Not reported" in s.do_report({"outcome": "CONFIRMED", "summary": "x", "evidence": []})
    log = open(tmp_path / "fake" / "runs" / "r1" / "implementer.log", encoding="utf-8").read()
    assert "Not reported: in no episode did Jev make" in log


def drive(game, gen, seconds=20):
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        game.renew()
        try:
            next(gen)
        except StopIteration as s:
            return s.value
        time.sleep(1 / 30)
    raise AssertionError("the controller didn't finish")


class TurningHarness(Harness):
    """Keeps turning the view (as walking and aiming do) for a few seconds."""
    goal = "Look around"
    tempo = 0.0
    tick_hz = 20.0

    def setup(self, game):
        self.t0 = game.t

    def observe(self, game):
        game.pose()  # (a harness reads the game every tick: the game's clock comes with its answers)
        return {"t": game.t - self.t0}

    def spin(self, game, obs):
        while True:
            game.turn(15, 0)
            yield

    def options(self, obs):
        return [Option("look_around", "Turn round", self.spin, max_seconds=60)]

    def status(self, obs):
        return Status("done", "looked") if obs["t"] > 5.0 else Status()


def test_the_summary_says_when_the_player_stops_answering_its_inputs(tmp_path):
    class DeadLink(FakeLink):
        def request(self, op, args=None, timeout=None):
            if op == "key" and (args or {}).get("key") in ("MouseX", "MouseY"):
                return {}  # the view doesn't turn any more
            return super().request(op, args, timeout)

    for make, flagged in ((FakeLink, False), (DeadLink, True)):
        link = make()  # (now: its clock starts when it's made)
        game = Game(link)
        game.look = {"x": 0.1, "y": 0.1}
        path = str(tmp_path / f"{make.__name__}.jsonl")
        runtime.Runtime(game, TurningHarness(), path, client=None, max_seconds=10).run()
        text = runtime.summarize(path)
        assert ("DIDN'T ANSWER ITS INPUTS" in text) == flagged, text[:600]


def test_probes_after_an_episode_log_as_before(game, tmp_path):
    before = game.log
    runtime.Runtime(game, CrateHarness(), str(tmp_path / "t.jsonl"), client=FakeJev("open_crate"), max_seconds=5).run()
    assert game.log is before
    game.press("E")  # no "I/O operation on closed file"


def test_reads_waited_on_count_a_batch_once(game):
    crate = game.actors("Crate")[0]
    w0 = game.waits()
    game.get(crate, "bOpened")
    game.get(crate, "bOpened")
    assert game.waits() - w0 == 2
    w0 = game.waits()
    game.batch([lambda: game.get(crate, "bOpened") for _ in range(5)])
    assert game.waits() - w0 == 1


def test_summary_flags_steps_that_read_one_after_another(game, tmp_path):
    class Steps(CrateHarness):
        goal = "Watch the crate"
        tempo = 0.0

        def watcher(self, batched):
            def run(g, o):
                for _ in range(40):
                    reads = [lambda: g.get(self.crate, "bOpened") for _ in range(5)]
                    if batched:
                        g.batch(reads)
                    else:
                        for r in reads:
                            r()
                    yield
            return run

        def options(self, obs):
            return [Option("read_one_by_one", "Watch the crate", self.watcher(False)),
                    Option("read_in_a_batch", "Watch the crate", self.watcher(True))]

        def status(self, obs):
            return Status()

    for likes in ("read_one_by_one", "read_in_a_batch"):
        path = str(tmp_path / f"{likes}.jsonl")
        res = runtime.Runtime(game, Steps(), path, client=FakeJev(likes), max_seconds=3).run()
        mean = res["steps"][likes]["waits_mean"]  # (the step an option ends on reads nothing)
        assert (4.5 <= mean <= 5) if likes == "read_one_by_one" else (0.8 <= mean <= 1), res["steps"]
        text = runtime.summarize(path)
        assert "Executor steps" in text and f"  {likes}: " in text
        assert ("SLOW STEPS: read_one_by_one" in text) == (likes == "read_one_by_one"), text


def test_harness_code_can_run_a_controller_to_its_end():
    from autopilot.kit import run, wait

    game = Game(FakeLink())
    t0 = game.t
    assert run(wait(game, 0.3)).ok and game.t - t0 >= 0.3


class SlowFrameLink(FakeLink):
    """A game that runs slowly: every read waits for a frame (here 0.06 s, as at under 20 fps), the player walks at
    600 and the game says how fast it moves."""

    frame = 0.06

    def request(self, op, args=None, timeout=None):
        if op not in ("key",):
            time.sleep(self.frame)
        body = (args or {}).get("body") or {}
        if op == "rc" and body.get("functionName") == "GetVelocity" and "Hero" in str(body.get("objectPath")):
            with self.lock:
                self._advance()
                if "W" in self.held:
                    yaw = math.radians(self.yaw)
                    return {"ReturnValue": {"X": 400 * math.cos(yaw), "Y": 400 * math.sin(yaw), "Z": 0}}
                return {"ReturnValue": {"X": 0, "Y": 0, "Z": 0}}
        return super().request(op, args, timeout)


@wall_clock
def test_a_slow_observe_doesnt_slow_the_executor(tmp_path):
    class SlowObserve(Harness):
        goal = "Count steps"
        tempo = 0.0
        tick_hz = 20.0

        def setup(self, game):
            self.steps, self.t0 = 0, game.t

        def observe(self, game):
            game.pose()
            time.sleep(0.4)  # (a harness reading many things, on a slow machine)
            return {"t": game.t - self.t0}

        def count(self, game, obs):
            while True:
                self.steps += 1
                yield

        def options(self, obs):
            return [Option("count", "Count", self.count, max_seconds=60)]

        def status(self, obs):
            return Status("done", "counted") if obs["t"] > 3.0 else Status()

    game = Game(FakeLink())
    h = SlowObserve()
    runtime.Runtime(game, h, str(tmp_path / "t.jsonl"), client=None, max_seconds=10).run()
    assert h.steps >= 30, h.steps  # (stepped with observe: about 8 in 3 s)


def test_hold_for_is_timed_by_the_game_when_the_plugin_can():
    from autopilot.kit import hold_for

    for features, timed in ((["timed_hold"], True), ([], False)):
        link = FakeLink()
        link.hello = {"project": "fake", "features": features}
        game = Game(link)
        drive(game, hold_for(game, "D", 0.2))
        holds = [i for i in link.inputs if i.get("key") == "D" and i.get("mode") == "hold"]
        assert holds and (holds[0].get("seconds") == 0.2) == timed, holds
        assert "D" not in game.held()


@wall_clock
def test_look_at_and_walk_to_work_when_every_read_takes_a_frame():
    from autopilot.kit import look_at

    link = SlowFrameLink()
    game = Game(link)
    game.look = {"x": 0.1, "y": 0.1}
    r = drive(game, look_at(game, Vec(0, 3000, 100), tolerance=1.5, timeout=0.6), seconds=20)
    assert r.ok, r  # (a 90 degree turn: more corrections than 0.6 s of slow reads allows)
    target = Vec(1200, 3000, 100)
    r = drive(game, walk_to(game, target, within=150, timeout=20), seconds=30)
    assert r.ok, r
    assert link.pos.dist2d(target) >= 100, link.pos.dist2d(target)  # stopped near `within`, not on top of it


def test_an_option_is_judged_on_an_observation_begun_after_it_ended(tmp_path):
    class Counter(Harness):
        goal = "Count"
        tempo = 0.0
        tick_hz = 20.0

        def setup(self, game):
            self.n, self.t0 = 0, game.t

        def observe(self, game):
            seen = self.n
            game.pose()
            time.sleep(0.3)  # (slow: by the time it returns, the option below may have ended)
            return {"n": seen, "t": game.t - self.t0}

        def bump(self, game, obs):
            yield
            self.n += 1

        def options(self, obs):
            return [Option("bump", "Add one", self.bump, effect=lambda a, b: b["n"] > a["n"])]

        def status(self, obs):
            return Status("done", "counted") if obs["t"] > 3.0 else Status()

    game = Game(FakeLink())
    res = runtime.Runtime(game, Counter(), str(tmp_path / "t.jsonl"), client=None, max_seconds=10).run()
    s = res["options"]["bump"]
    assert s.get("worked", 0) >= 3 and not s.get("failed"), s


def test_no_decision_on_a_world_from_before_the_last_option_ended(tmp_path):
    class Take(Harness):
        goal = "Take the coins"
        tempo = 0.0
        tick_hz = 20.0

        def setup(self, game):
            self.coins, self.starts_on_taken, self.t0 = 3, 0, game.t

        def observe(self, game):
            left = self.coins
            game.pose()
            time.sleep(0.3)
            return {"left": left, "t": game.t - self.t0}

        def take(self, game, obs):
            if self.coins < obs["left"]:  # (started on an observation from before the last take)
                self.starts_on_taken += 1
            yield
            self.coins -= 1

        def options(self, obs):
            return [Option("take_coin", "Take a coin", self.take)] if obs["left"] > 0 else []

        def status(self, obs):
            return Status("done", "all taken") if obs["left"] == 0 else Status()

    game = Game(FakeLink())
    h = Take()
    res = runtime.Runtime(game, h, str(tmp_path / "t.jsonl"), client=None, max_seconds=10).run()
    assert res["status"]["state"] == "done" and h.starts_on_taken == 0, (res["status"], h.starts_on_taken)


class RampLink(FakeLink):
    """A flat fake that reports one way up in view (its foot and top), when asked (ways=True), or says nothing of
    ways up (ways=False, as a plugin that can't tell). The path walked is kept."""

    def __init__(self, ways=True):
        super().__init__()
        self.ways = ways
        self.path = []

    def request(self, op, args=None, timeout=None):
        if op == "view" and (args or {}).get("ways_up"):
            body = {"sonar": {"ways": []}}
            if self.ways:
                body["ways_up"] = [{"foot": {"X": 600, "Y": -400, "Z": 0}, "top": {"X": 600, "Y": 200, "Z": 300},
                                    "rises_by": 300}]
            return {"body": body}
        r = super().request(op, args, timeout)
        if op == "rc" and ((args or {}).get("body") or {}).get("functionName") == "K2_GetActorLocation":
            self.path.append(self.pos)
        return r


def test_walk_to_a_higher_goal_goes_by_a_way_up_it_sees():
    link = RampLink()
    game = Game(link)
    game.look = {"x": 0.1, "y": 0.1}
    goal = Vec(600, 800, 400)  # (300 above the player's middle: a floor up)
    r = drive(game, walk_to(game, goal, within=100, timeout=30), seconds=40)
    assert r.ok, r
    assert min(p.dist2d(Vec(600, -400, 0)) for p in link.path) < 120  # by its foot
    assert min(p.dist2d(Vec(600, 200, 0)) for p in link.path) < 120  # and its top


def test_walk_to_without_ways_up_walks_straight():
    link = RampLink(ways=False)
    game = Game(link)
    game.look = {"x": 0.1, "y": 0.1}
    r = drive(game, walk_to(game, Vec(600, 800, 400), within=100, timeout=30), seconds=40)
    assert r.ok, r
    assert min(p.dist2d(Vec(600, -400, 0)) for p in link.path) > 300  # nowhere near the way up it wasn't told of


def test_the_request_message_brings_what_runs_read_first(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    root = tmp_path / "fake"
    (root / "codex" / "blueprints").mkdir(parents=True)
    (root / "codex" / "README.md").write_text("# Codex of fake\nblueprints/: the game's Blueprints", encoding="utf-8")
    (root / "codex" / "blueprints" / "BP_Crate.txt").write_text("var Opened\nfunction OpenCrate", encoding="utf-8")
    (root / "codex" / "blueprints" / "BP_Hero.txt").write_text("var Health", encoding="utf-8")
    (root / "notes.md").write_text("- E opens a crate within 150", encoding="utf-8")
    (root / "runs" / "r0").mkdir(parents=True)
    (root / "runs" / "r0" / "prompt.txt").write_text("Open the crate", encoding="utf-8")
    (root / "runs" / "r0" / "report.json").write_text(json.dumps({"outcome": "achieved", "summary": "Opened it."}))
    s = session.Session(game, "fake", "Verify that opening the crates works", "r1")
    opening = s.opening()
    assert "Hero_C_0" in opening and game.link.paused
    text = session.briefing(s, opening)
    assert "E opens a crate within 150" in text and "# Codex of fake" in text
    assert "runs/r0: Open the crate -> achieved: Opened it." in text and "runs/r1" not in text
    assert "codex/blueprints/BP_Crate.txt (open: 2, crate: 1)" in text and "BP_Hero" not in text
    assert "Hero_C_0" in text
    assert "{" not in session.PROMPT.replace("{prompt}", "").replace("{project}", "").replace(
        "{run}", "").replace("{briefing}", "")


def test_the_request_message_says_what_changed_in_the_codex_since_the_last_run(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    root = tmp_path / "fake"
    files = {"README.md": "# Codex of fake", "level.txt": "Hero at (0, 0, 0)", "summary.txt": "Time: 12.0 s",
             "blueprints/BP_Crate.txt": "var Opened", "blueprints/BP_Hero.txt": "var Health",
             "blueprints/BP_Door.txt": "var Locked"}
    for rel, text in files.items():
        (root / "codex" / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / "codex" / rel).write_text(text, encoding="utf-8")
    (root / "notes.md").write_text("- BP_Crate opens with E within 150", encoding="utf-8")

    def run(run_id, played=True, write=None):
        s = session.Session(game, "fake", "Verify that opening the crates works", run_id)
        text = session.briefing(s)
        s.episodes = 1 if played else 0
        for name, content in (write or {}).items():  # (what the agent wrote during the run)
            (root / name).write_text(content, encoding="utf-8")
        session.finish(s, time.time(), None if played else "stopped by the user", "")
        return text

    def since(text):
        return text[text.index("## The codex since the last run"):].split("\n\n")[0].splitlines()

    assert "The codex since the last run" not in run("r1")  # (no run recorded its codex yet)
    assert "Unchanged: the same code and data as that run worked from." in run("r2")
    (root / "codex" / "blueprints" / "BP_Crate.txt").write_text("var Opened\nvar NeedsKey", encoding="utf-8")
    (root / "codex" / "blueprints" / "BP_Hero.txt").unlink()
    (root / "codex" / "blueprints" / "BP_Key.txt").write_text("var Picked", encoding="utf-8")
    (root / "codex" / "level.txt").write_text("Hero at (5, 0, 0)", encoding="utf-8")  # (the moment's: not a change)
    (root / "codex" / "summary.txt").write_text("Time: 13.5 s", encoding="utf-8")
    told = since(run("r3", played=False))
    assert told == [
        "## The codex since the last run (runs/r2)",
        "1 changed, 1 removed, 1 added since that run. What notes.md, lib.py and earlier harnesses say about these may "
        "be out of date: check it before you rely on it. A file notes.md or lib.py names stays listed until the note "
        'or helper naming it is edited: fix it, or, if it still holds, say so there ("checked in runs/r3").',
        "- changed: codex/blueprints/BP_Crate.txt (named in notes.md)",
        "- removed: codex/blueprints/BP_Hero.txt", "- added: codex/blueprints/BP_Key.txt"]
    # r3 was stopped before it played: r4 is told the same. r4 plays but leaves the crate's note as it was
    assert since(run("r4")) == [told[0], told[1].replace("runs/r3", "runs/r4"), *told[2:]]
    # so r5 is told again about the crate (named), never again about the hero and the key (named nowhere)
    assert since(run("r5"))[1:] == [
        "1 earlier changes to files notes.md or lib.py name, not checked since. What notes.md, lib.py and earlier "
        "harnesses say about these may be out of date: check it before you rely on it. A file notes.md or lib.py "
        'names stays listed until the note or helper naming it is edited: fix it, or, if it still holds, say so there '
        '("checked in runs/r5").',
        "- changed: codex/blueprints/BP_Crate.txt (named in notes.md; changed before runs/r2, not checked since)"]
    # r6 checks it and says so in the note: then it is done
    assert "BP_Crate.txt" in run("r6", write={"notes.md": "- BP_Crate opens with E within 150 (checked in runs/r6)"})
    assert since(run("r7")) == ["## The codex since the last run (runs/r6)",
                                "Unchanged: the same code and data as that run worked from."]
    assert codex.SEEN_FILE in os.listdir(root) and not os.path.exists(root / "codex" / codex.SEEN_FILE)


def test_a_codex_change_named_in_lib_py_stays_listed_until_its_helper_is_edited(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    root = tmp_path / "fake"
    (root / "codex" / "blueprints").mkdir(parents=True)
    (root / "codex" / "blueprints" / "BP_Door.txt").write_text("var Locked", encoding="utf-8")
    lib = "def open_door(game):\n    # BP_Door: E within 150\n    return 1\n\n\ndef jump(game):\n    return 2\n"
    (root / "lib.py").write_text(lib, encoding="utf-8")

    def run(run_id, lib_after=None):
        s = session.Session(game, "fake", "Verify that x", run_id)
        text = session.briefing(s)
        s.episodes = 1
        if lib_after:
            (root / "lib.py").write_text(lib_after, encoding="utf-8")
        session.finish(s, time.time(), None, "")
        return text

    run("r1")
    (root / "codex" / "blueprints" / "BP_Door.txt").write_text("var Locked\nvar NeedsKey", encoding="utf-8")
    assert "- changed: codex/blueprints/BP_Door.txt (named in lib.py)" in run("r2", lib.replace("return 2", "return 3"))
    assert "(named in lib.py; changed before runs/r1, not checked since)" in run("r3")  # (another helper was edited)
    monkeypatch.setattr(session, "BRIEF_CHANGES", 0)  # (a briefing that can't list it: that run wasn't told)
    run("r4", lib.replace("E within 150", "E with the key"))
    monkeypatch.setattr(session, "BRIEF_CHANGES", 30)
    assert "not checked since" in run("r5", lib.replace("E within 150", "E with the key (checked in runs/r5)"))
    assert "Unchanged" in run("r6")


def test_a_codex_fingerprint_reads_again_only_what_was_rewritten(tmp_path):
    from autopilot import codex

    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "Pump.cpp").write_text("void Pump() {}", encoding="utf-8")
    (tmp_path / "README.md").write_text("# index", encoding="utf-8")
    first = codex.fingerprint(str(tmp_path))
    assert list(first) == ["source/Pump.cpp"] and codex.fingerprint(str(tmp_path)) == first
    st = os.stat(tmp_path / "source" / "Pump.cpp")
    (tmp_path / "source" / "Pump.cpp").write_text("void Pomp() {}", encoding="utf-8")  # (same size)
    os.utime(tmp_path / "source" / "Pump.cpp", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert codex.fingerprint(str(tmp_path))["source/Pump.cpp"] != first["source/Pump.cpp"]


def test_probe_is_cut_off_after_its_time(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    monkeypatch.setattr(session, "PROBE_SECONDS", 0.5)
    s = session.Session(game, "fake", "Open the crate", "r1")
    t0 = time.time()
    out = s.probe("import time\nprint('before')\ntime.sleep(3)\nprint('after')")
    assert time.time() - t0 < 2.5
    assert "before" in out and "after" not in out
    assert "CUT OFF" in out and "0 s" in out
    assert game.link.paused  # (the keys let go and the game paused, though the code runs on)


def test_probe_writes_are_research_in_the_report(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    s = session.Session(game, "fake", "Open the crate", "r1")
    s.probe("game.writes.append({'t': 1, 'object': 'crate', 'property': 'bOpen', 'value': True, 'why': 'research'})")
    assert game.writes[-1]["probe"] is True
    report = session.finish(s, time.time(), None, "")
    assert report["research_modified"] and report["research_modified"][0]["property"] == "bOpen"
    assert report["level_modified"] == []
    # a change made between an episode and its continuation bears on play: it counts as the play's
    game.writes[-1]["in_play"] = True
    report = session.finish(s, time.time(), None, "")
    assert report["level_modified"] and report["research_modified"] == []


def test_the_report_keeps_jev_s_decisions_and_the_seconds_of_play_per_episode(game, tmp_path, monkeypatch):
    """report.json's episode_stats: what the next cost estimate reads (autopilot/estimate.py), not the traces."""
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    s = session.Session(game, "fake", "Open the crate", "r1")
    os.makedirs(s.run_dir, exist_ok=True)
    with open(os.path.join(s.run_dir, "trace1.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(x) for x in ({"kind": "start", "t": 2.0}, {"kind": "decision", "t": 3.0},
                                                    {"kind": "decision", "t": 9.5}, {"kind": "end", "t": 12.0})))
    session.finish(s, time.time(), None, "")
    with open(os.path.join(s.run_dir, "report.json"), encoding="utf-8") as f:
        assert json.load(f)["episode_stats"] == [{"decisions": 2, "play_seconds": 10.0}]


def test_system_prompt_fills_the_research_and_agents_parts(game):
    from autopilot.agents import session

    text = session.system_prompt(game.engine, "runs/r1")
    for hole in ("{agents}", "{research}", "{probe_seconds", "{kit}"):
        assert hole not in text
    assert f"at most {session.PROBE_SECONDS:.0f} s" in text
    assert "check_harness" in text and "STALLED" in text and "restart=False" in text
    told = session.system_prompt(game.engine, "runs/r1", agents="Ask the researcher.")
    assert "grepping their text. Ask the researcher. Never wait" in told


def test_a_continued_episode_hands_the_plan_progress_to_resume(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    s = session.Session(game, "fake", "Open the crate", "r1", client=FakeJev("walk"))
    told = tmp_path / "told.json"
    (tmp_path / "fake" / "runs" / "r1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "fake" / "runs" / "r1" / "harness.py").write_text(f"""
import json
from autopilot.kit import *

class PlanHarness(Harness):
    goal = "walk, then open"
    stall_seconds = 0
    plan = [Step("walk", "Walk up to the crate", lambda o: o["near"], options=["walk"]),
            Step("open", "Open the crate", lambda o: o["open"], options=["open"])]

    def resume(self, game, progress):
        open({json.dumps(str(told))}, "w").write(json.dumps(progress))

    def observe(self, game):
        p = game.pose()
        return {{"_pose": p, "near": True, "open": False}}

    def options(self, obs):
        return [Option("walk", "walk", lambda g, o: wait(g, 0.2)), Option("open", "open", lambda g, o: wait(g, 0.2))]
""", encoding="utf-8")
    assert "Plan" in s.run_harness(5)
    assert s.progress and s.progress["episode"] == 1 and "walk" in s.progress["done"]
    assert not told.exists()  # (a restarted level starts the plan over: nothing to resume)
    s.run_harness(5, restart=False)
    progress = json.loads(told.read_text(encoding="utf-8"))
    assert progress["episode"] == 1 and "walk" in progress["done"]
    assert "continued from episode 1" in open(s.log, encoding="utf-8").read()


def test_a_continuation_needs_a_live_player(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    s = session.Session(game, "fake", "Open the crate", "r1", client=FakeJev("open_crate"))
    src = open(__file__, encoding="utf-8").read()
    (tmp_path / "fake" / "runs" / "r1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "fake" / "runs" / "r1" / "harness.py").write_text(
        "from autopilot.kit import *\n\n" + src[src.index("class CrateHarness"):src.index("@pytest.fixture")],
        encoding="utf-8")
    assert "status=done" in s.run_harness(15)  # (a pawn was there)
    monkeypatch.setattr(game, "context", lambda fresh=False: {"character": ""})  # (dead, or a menu)
    out = s.run_harness(15, restart=False)
    assert "Not played" in out and "no player pawn" in out
    assert s.episodes == 1 and s.played == 1 and not os.path.exists(tmp_path / "fake" / "runs" / "r1" / "harness2.py")


def test_a_game_without_a_pawn_continues(game, tmp_path, monkeypatch):
    """A game played without a player pawn at all (a strategy game's camera) is not "dead": it goes on as it is."""
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    s = session.Session(game, "fake", "Open the crate", "r1", client=FakeJev("open_crate"))
    src = open(__file__, encoding="utf-8").read()
    (tmp_path / "fake" / "runs" / "r1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "fake" / "runs" / "r1" / "harness.py").write_text(
        "from autopilot.kit import *\n\n" + src[src.index("class CrateHarness"):src.index("@pytest.fixture")],
        encoding="utf-8")
    real = game.context
    monkeypatch.setattr(game, "context", lambda fresh=False: {**real(fresh), "character": ""})
    monkeypatch.setattr(game, "restart", lambda map=None, wait_for_pawn=20.0: False)
    assert "status=done" in s.run_harness(15)
    out = s.run_harness(15, restart=False)
    assert "Not played" not in out and "status=done" in out
    assert s.episodes == 2 and "continued from episode 1" in open(s.log, encoding="utf-8").read()


def test_probe_namespace_has_check_harness(game, tmp_path, monkeypatch):
    import io

    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    s = session.Session(game, "fake", "Open the crate", "r1")
    ns = s._namespace(io.StringIO())
    assert "check_harness" in ns
    assert "doesn't exist yet" in ns["check_harness"]()


class InputNotesLink(FakeLink):
    """A game whose context also says how input reaches it (Unity's "input": Hands.Capabilities())."""

    def __init__(self, input):
        super().__init__()
        self.input = input

    def request(self, op, args=None, timeout=None):
        out = super().request(op, args, timeout)
        return {**out, "input": self.input} if op == "context" else out


def test_the_request_message_brings_the_plugins_input_notes(tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    note = 'Point the mouse first (game.request("pointer", ...)), then press RightMouseButton there.'
    s = session.Session(Game(InputNotesLink({"pointer": True, "notes": [note]})), "fake", "Verify that x", "r1")
    opening = s.opening()
    assert "input_notes" in opening and "Hero_C_0" in opening
    assert note.replace('"', '\\"') in session.briefing(s, opening)
    for input in ({"pointer": True, "notes": []}, {"pointer": True}, "none", None):  # (Unreal's and Godot's: none)
        opening = session.Session(Game(InputNotesLink(input)), "fake", "Verify that x", "r1").opening()
        assert "input_notes" not in opening and "Hero_C_0" in opening, (input, opening)


class NetLink(FakeLink):
    """A multiplayer game: the context's `net` (the plugin's), and a pause answered with why it didn't happen."""

    def __init__(self, net, why=None):
        super().__init__()
        self.net, self.why = net, why

    def request(self, op, args=None, timeout=None):
        out = super().request(op, args, timeout)
        if op == "context":
            return {**out, "net": self.net}
        if op == "pause" and self.why and (args or {}).get("paused"):
            return {"paused": False, "why": self.why}
        return out


def test_the_request_message_says_whose_player_a_multiplayer_run_plays_and_where_the_truth_is(tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    server = "/Game/Maps/UEDPIE_0_Arena.Arena"
    net = {"mode": "client", "server_world": server, "server_character": server + ":PersistentLevel.Hero_C_0",
           "server_controller": server + ":PersistentLevel.PC_C_0", "clients": 2}
    opening = session.Session(Game(NetLink(net)), "fake", "Verify that x", "r1").opening()
    line = next(x for x in opening.splitlines() if x.startswith("network: "))
    assert "client of a dedicated server" in line and "first of 2 clients" in line
    assert server in line and "PersistentLevel.Hero_C_0" in line and "Play settings" in line
    assert "can't be paused" not in line
    elsewhere = {"mode": "client", "server_world": "", "server_character": "", "server_controller": "", "clients": 1}
    why = "the game's server runs in another process: it can't be paused from here"
    opening = session.Session(Game(NetLink(elsewhere, why)), "fake", "Verify that x", "r1").opening()
    line = next(x for x in opening.splitlines() if x.startswith("network: "))
    assert "another process" in line and "can't be paused" in line and "first of" not in line
    opening = session.Session(Game(NetLink({"mode": "listen_server"})), "fake", "Verify that x", "r1").opening()
    assert "listen server" in opening
    for single in ({"mode": "standalone"}, None):  # (a single-player game, and a plugin from before `net`)
        opening = session.Session(Game(NetLink(single)), "fake", "Verify that x", "r1").opening()
        assert "network" not in opening and "Hero_C_0" in opening


class StalledPauseLink(FakeLink):
    """A game that doesn't answer a pause (an unfocused editor that stalled); what it was asked is kept."""

    def __init__(self):
        super().__init__()
        self.asked = []

    def request(self, op, args=None, timeout=None):
        import requests

        self.asked.append((op, dict(args or {})))
        if op == "pause" and (args or {}).get("paused", True):
            raise requests.ConnectionError("the game didn't answer pause within 30s")
        return super().request(op, args, timeout)


@pytest.mark.parametrize("agent", ["claude_code", "codex", "opencode"])
def test_an_agent_cleans_up_and_reports_when_the_game_doesnt_answer_the_first_pause(agent, tmp_path, monkeypatch):
    import importlib.util

    from autopilot import codex

    sdk = {"claude_code": "claude_agent_sdk", "codex": "openai_codex"}.get(agent)
    if sdk and importlib.util.find_spec(sdk) is None:
        pytest.skip(f"{sdk} isn't installed")
    mod = importlib.import_module(f"autopilot.agents.{agent}")
    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    monkeypatch.setenv("AUTOPILOT_SANDBOX", "off")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    if agent == "opencode":
        monkeypatch.setattr(mod, "bun", lambda: "bun")
        monkeypatch.setattr(mod, "ensure_installed", lambda exe: None)
        monkeypatch.setattr(mod, "run_host", lambda *a, **k: pytest.fail("the host ran on a game that didn't pause"))
    link = StalledPauseLink()
    report = mod.implement(Game(link), "fake", "Verify that the crate opens", run_id="r1", timeout=60)
    assert report["stopped"] == "ConnectionError: the game didn't answer pause within 30s", report
    assert report["outcome"] == "INCONCLUSIVE" and (tmp_path / "fake" / "runs" / "r1" / "report.json").exists()
    assert link.asked[-2:] == [("key", {"mode": "release_all"}), ("pause", {"paused": False})]  # (the game let go)


class BoomLink(FakeLink):
    """A third-person camera: 300 behind the head along the view, so it swings up and forward as the view pitches down;
    the player slides on after forward is let go (braking 1500/s/s)."""

    def __init__(self):
        super().__init__()
        self.speed = 0.0

    def _advance(self):
        now = time.monotonic()
        dt, self._wall = now - self._wall, now
        if self.paused:
            return
        self.game_time += dt
        self.speed = 400.0 if "W" in self.held else max(0.0, self.speed - 1500.0 * dt)
        f = Vec(math.cos(math.radians(self.yaw)), math.sin(math.radians(self.yaw)), 0)
        self.pos = self.pos + f * (self.speed * dt)

    def request(self, op, args=None, timeout=None):
        if op == "rc":
            fn = (args.get("body") or {}).get("functionName")
            if fn == "GetCameraLocation":
                with self.lock:
                    self._advance()
                    y, p = math.radians(self.yaw), math.radians(self.pitch)
                    view = Vec(math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), math.sin(p))
                    return {"ReturnValue": (self.pos + Vec(0, 0, 60) - view * 300).ue()}
            if fn == "GetActorBounds" and "Crate" in args["body"].get("objectPath", ""):
                return {"Origin": self.crate.ue(), "BoxExtent": {"X": 12, "Y": 12, "Z": 8}}  # (a small thing)
            if fn == "GetVelocity":
                with self.lock:
                    self._advance()
                    y = math.radians(self.yaw)
                    return {"ReturnValue": Vec(math.cos(y) * self.speed, math.sin(y) * self.speed, 0).ue()}
        return super().request(op, args, timeout)


def test_look_at_puts_a_third_person_crosshair_on_a_small_thing_below():
    from autopilot.kit import look_at

    link = BoomLink()
    game = Game(link)
    game.look = {"x": 0.1, "y": 0.1}
    link.crate = Vec(220, 0, 10)  # (on the floor ahead)
    r = drive(game, look_at(game, "/Game/M.M:PersistentLevel.Crate_C_0", timeout=1.5), seconds=10)
    assert r.ok, r
    right, up = game.aim_error(link.crate)
    assert abs(up) < 1.0 and abs(right) < 1.0, (right, up)  # (inside the thing: it spans about 2.6 degrees)
    assert link.pitch < -25


@wall_clock
def test_walk_to_lets_go_early_by_the_slide_it_measured():
    link = BoomLink()
    game = Game(link)
    game.look = {"x": 0.1, "y": 0.1}
    ends = []
    for target in (Vec(1500, 0, 100), Vec(1500, 1500, 100), Vec(0, 1500, 100)):
        r = drive(game, walk_to(game, target, within=150, timeout=20), seconds=30)
        assert r.ok, r
        assert link.speed < 20.0  # (it returns stopped: slower than _settle's 20 cm/s)
        ends.append(link.pos.dist2d(target))
    assert game.coast_s > 0.05
    assert all(90 <= d <= 210 for d in ends[1:]), ends  # (once the slide is measured: about `within`, not on top)


def test_wait_counts_real_time_while_the_game_clock_stands_still():
    from autopilot.kit import wait

    link = FakeLink()
    game = Game(link)
    link.paused = True  # (a menu that pauses the game)
    t0 = time.monotonic()
    drive(game, wait(game, 1.0), seconds=10)
    assert 0.9 < time.monotonic() - t0 < 1.8, time.monotonic() - t0  # (not its cap, 3 s + 1)
    link.paused = False
    game.context(fresh=True)
    g0, t0 = game.t, time.monotonic()
    drive(game, wait(game, 1.0), seconds=10)  # (a running game: game time, read now and then)
    assert game.t - g0 >= 0.95 and time.monotonic() - t0 < 1.6, (game.t - g0, time.monotonic() - t0)


def test_steering_ends_the_episode_and_play_goes_on_without_a_restart(game, tmp_path, monkeypatch):
    from autopilot import codex
    from autopilot.agents import session

    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    s = session.Session(game, "fake", "Open the crate", "r1", client=FakeJev("wait"))
    src = open(__file__, encoding="utf-8").read()
    (tmp_path / "fake" / "runs" / "r1" / "harness.py").write_text(
        "from autopilot.kit import *\n\n" + src[src.index("class CrateHarness"):src.index("@pytest.fixture")],
        encoding="utf-8")
    threading.Timer(1.0, lambda: s.steering.add("go and open the crate now")).start()
    t0 = time.monotonic()
    out = s.run_harness(30)
    assert time.monotonic() - t0 < 6, "the episode didn't end when the user steered"
    assert "the user steered" in out
    note = s.steer_note()
    assert "go and open the crate now" in note and s.steered and not s.steering.pending()
    sessions = []
    real = game.link.request
    monkeypatch.setattr(game.link, "request", lambda op, args=None, timeout=None: (
        sessions.append(args) if op == "session" else None) or real(op, args, timeout))
    out = s.run_harness(5, restart=True)
    assert "Not restarted" in out and not sessions  # (the game went on from where it was)
    assert s.restart_level().startswith("Not restarted") and not sessions


def test_an_exploration_plays_only_its_minutes_then_its_run_ends(game, tmp_path, monkeypatch):
    from autopilot import codex, sandbox
    from autopilot.agents import session

    monkeypatch.setenv("AUTOPILOT_SANDBOX", "off")
    monkeypatch.setattr(codex, "MEMORY", str(tmp_path))
    monkeypatch.setattr(session, "PLAY_GRACE", 1.0)
    stopped = []
    s = session.open_session(game, "fake", "Explore the game for bugs (0.05 minutes of play)", "r1",
                             client=FakeJev("wait"), stop=lambda: bool(stopped), play_seconds=6)
    src = open(__file__, encoding="utf-8").read()
    crate = "from autopilot.kit import *\n\n" + src[src.index("class CrateHarness"):src.index("@pytest.fixture")]
    (tmp_path / "fake" / "runs" / "r1" / "harness.py").write_text(crate, encoding="utf-8")
    assert "play: 0.0 of 0 min used" in s.clock() and s.play_started is None  # (reading and writing don't count)
    t0 = time.monotonic()
    out = s.run_harness(30)  # (asks for more than is left: the episode ends when the minutes are up)
    assert 5 < time.monotonic() - t0 < 9, "the episode didn't end when the exploration's minutes were up"
    assert "minutes of play are up" in out and "the user steered" not in out
    assert s.play_left() < 0.5 and "play time is up: call report now" in s.clock()
    assert s.run_harness(30).startswith("No episode") and s.episodes == 1  # (nothing more is played)
    assert not s.stop()  # (the agent has PLAY_GRACE to report)
    time.sleep(1.2)
    assert s.stop() and s.out_of_time()  # (then the run ends)
    report = session.finish(s, time.time(), "stopped", "")
    assert report["outcome"] == "explored" and "minutes of play were up" in report["summary"]
    # A run without minutes of play: only the user's Stop ends it
    s = session.open_session(game, "fake", "Open the crate", "r2", stop=lambda: bool(stopped))
    assert s.play_left() is None and not s.stop() and "play:" not in s.clock()
    stopped.append(True)
    assert s.stop()
    # In the sandbox, the episode ends at the time it is given
    monkeypatch.setenv("AUTOPILOT_SANDBOX", "process")
    root = os.path.join(str(tmp_path), "fake")
    sb = sandbox.Sandbox(game.link, FakeJev("wait"), "fake", "Explore", "r1", root, str(tmp_path))
    try:
        t0 = time.monotonic()
        out = sb.call("run_harness", seconds=30, until=time.time() + 2, timeout=120)
        assert time.monotonic() - t0 < 8 and "minutes of play are up" in out, out
    finally:
        sb.close()


def test_what_the_user_reads_says_the_agent_not_jev():
    from autopilot import cli

    said = cli.plain_words("Jev lit the lantern. Then Jev's second try worked: Jev made 20 decisions "
                           "(Jev-played episode).\n- Jev picked it up\n"
                           "The harness counted it; harnesses: 3, see harness2.py")
    assert "Jev" not in said
    assert said.startswith("The agent lit the lantern. Then the agent's second try worked: the agent made 20")
    assert "(agent-played episode)" in said and "\n- The agent picked it up" in said
    assert "The harness counted it; harnesses: 3, see harness2.py" in said  # (a harness is called a harness)
    frame = cli.result_frame({"outcome": "achieved", "summary": "Jev did it.", "evidence": ["Jev opened it"]},
                             "Open the crate")
    assert frame["notes"] == ["The agent did it.", "The agent opened it"]
    frame = cli.result_frame({"outcome": "CONFIRMED", "summary": "Shown by Jev.", "evidence": []}, "Verify that X")
    assert frame["reason"] == "Shown by the agent."


def test_jev_client_asks_for_an_exact_version(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "jev-latest")  # (the SDK's own default: not what Jev plays with)
    monkeypatch.delenv("AUTOPILOT_JEV_MODEL", raising=False)
    assert runtime.jev_client(warm=False)._config.default_model == runtime.JEV_MODEL == "jev-1.13.0"
    monkeypatch.setenv("AUTOPILOT_JEV_MODEL", "jev-1.14.0")
    assert runtime.jev_client(warm=False)._config.default_model == "jev-1.14.0"


def test_jev_client_keeps_its_connection_and_server_warm(monkeypatch):
    import gc
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = []  # (path, client port) per request

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # (keep-alive)

        def do_GET(self):
            seen.append((self.path, self.client_address[1]))
            body = b'{"models": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("TYPESAFE_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
        monkeypatch.setattr(runtime, "WARM_EVERY", 0.3)
        monkeypatch.setattr(runtime, "WARM_POLL", 0.05)
        client = runtime.jev_client()
        deadline = time.monotonic() + 5
        while len(seen) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        # Warmed at once, then again while idle, all on the one connection kept open
        assert len(seen) >= 3 and {p for p, _ in seen} == {"/v1/models"}
        assert len({port for _, port in seen}) == 1
        del client
        gc.collect()
        time.sleep(0.5)
        quiet = len(seen)
        time.sleep(0.7)
        assert len(seen) == quiet  # (a dropped client stops its keep-warm calls)
    finally:
        server.shutdown()
        server.server_close()
