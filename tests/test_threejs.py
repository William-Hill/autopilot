"""The Three.js adapter (autopilot/engines/threejs.py) without a browser: the project on disk, the codex, the frame, the
headless launch and the implementer's prompt; and the browser bridge itself (plugin/threejs/autopilot-bridge.js) over
a real WebSocket, in Node with a stand-in game (plugin/threejs/test/fake_game.mjs), when Node is there."""

import json
import os
import re
import shutil
import subprocess
import time

import pytest

from autopilot import bridge, engines
from autopilot.engines import threejs as threemod
from autopilot.kit.game import Game
from autopilot.kit.geom import Vec

ROOT = os.path.join(os.path.dirname(__file__), "..")
FAKE_GAME = os.path.join(ROOT, "plugin", "threejs", "test", "fake_game.mjs")


def _project(tmp_path, three="0.180.0"):
    d = tmp_path / "glider"
    for sub in ("src/sim", "src/render", "node_modules/three", "dist", ".cache", "public", "levels"):
        (d / sub).mkdir(parents=True)
    deps = {"three": three} if three else {"left-pad": "1.0.0"}
    (d / "package.json").write_text(json.dumps({"name": "glider-park", "scripts": {"dev": "vite"},
                                                "dependencies": deps}))
    (d / "index.html").write_text('<canvas></canvas><script type="module" src="/src/main.js"></script>')
    (d / "src" / "main.js").write_text("import * as THREE from 'three';\n")
    (d / "src" / "sim" / "glide.js").write_text("export const LIFT = 2;\n")
    (d / "src" / "render" / "sky.glsl").write_text("void main() {}\n")
    (d / "src" / "autopilot.js").write_text(
        "import { connectBridge } from './autopilot-bridge.js';\nconnectBridge({});\n")
    (d / "src" / "autopilot-bridge.js").write_text("// the bridge\n")
    (d / "node_modules" / "three" / "three.module.js").write_text("// three\n")
    (d / "dist" / "main.js").write_text("// built\n")
    (d / ".cache" / "x.js").write_text("// cache\n")
    (d / "package-lock.json").write_text("{}")
    (d / "levels" / "huge.json").write_text("[" + "0," * (threemod.MAX_BYTES // 2) + "0]")
    (d / "levels" / "small.json").write_text("[1, 2]")
    return d


def test_registered_and_chosen_from_hello():
    assert engines.get("threejs").name == "threejs"
    assert engines.for_hello({"engine": "ThreeJS"}).name == "threejs"
    assert isinstance(engines.get("threejs"), engines.OpEngine)
    # the plugin answers the engine-neutral ops, so the brain asks it for them too
    assert set(bridge.ENGINE_OPS) <= set(bridge.required_ops({"engine": "threejs"}))


def test_project_is_a_folder_whose_package_uses_three(tmp_path):
    t = engines.get("threejs")
    d = _project(tmp_path)
    assert os.path.samefile(t.project_at(str(d)), d)
    assert os.path.samefile(t.project_at(str(d / "package.json")), d)
    assert t.project_at(str(d / "index.html")) is None
    assert t.project_name(str(d)) == "glider-park"
    eng, project = engines.find_project(str(d))
    assert eng.name == "threejs" and os.path.samefile(project, d)
    other = _project(tmp_path / "other", three=None)
    assert t.project_at(str(other)) is None
    assert engines.find_project(str(other)) == (None, None)


def test_codex_is_the_source_text(tmp_path):
    t = engines.get("threejs")
    for ok in ("package.json", "index.html", "src/main.js", "src/sim/glide.ts", "src/render/sky.glsl",
               "src/ui/hud.css", "levels/small.json", "vite.config.js"):
        assert t.codex_path(ok) == ok, ok
    for bad in ("node_modules/three/three.module.js", "dist/main.js", ".cache/x.js", "src/.hidden/x.js",
                "package-lock.json", "src/autopilot-bridge.js", "src/vendor/lib.min.js", "src/main.js.map",
                "public/x.js", "art/skater.glb", "../x.js", "/abs.js", "README.md"):
        assert t.codex_path(bad) is None, bad
    d = _project(tmp_path)
    root = tmp_path / "codex"
    n = t.copy_source(str(d), str(root))
    got = sorted(os.path.relpath(os.path.join(b, f), root).replace(os.sep, "/")
                 for b, _, fs in os.walk(root) for f in fs)
    assert got == ["index.html", "levels/small.json", "package.json", "src/autopilot.js", "src/main.js",
                   "src/render/sky.glsl", "src/sim/glide.js"]
    assert n == len(got)  # (the huge level stays out: over MAX_BYTES)


def test_codex_index_says_where_to_start(tmp_path):
    t = engines.get("threejs")
    d = _project(tmp_path)
    root = tmp_path / "codex"
    t.copy_source(str(d), str(root))
    t.write_index(str(root), "glider-park")
    text = (root / "README.md").read_text(encoding="utf-8")
    assert "three 0.180.0" in text
    assert "dev (`vite`)" in text
    assert "The page loads /src/main.js (in the codex as src/main.js)" in text
    assert "src/autopilot.js" in text  # (where the game gives the bridge its hooks)
    assert "/Scene/" in text


def test_threejs_frame_values():
    t = engines.get("threejs")
    assert t.to_kit({"x": 3.0, "y": 0.5, "z": -6.0}) == Vec(600, 300, 50)  # forward 6 m, right 3 m, up 0.5 m
    assert t.to_kit([3.0, 0.5, -6.0]) == Vec(600, 300, 50)  # (a game's own arrays)
    assert t.to_native(Vec(600, 300, 50)) == {"x": 3.0, "y": 0.5, "z": -6.0}
    yaw, pitch = t.rotation_to_kit({"x": 0.1, "y": -1.5707963, "z": 0})
    assert abs(yaw - 90) < 1e-4 and abs(pitch - 5.7296) < 1e-3
    r = t.rotation_to_native(90, 10)
    assert abs(r["y"] + 1.5707963) < 1e-6 and abs(r["x"] - 0.1745329) < 1e-6


def test_launch_command_opens_the_page_headless(tmp_path):
    t = engines.get("threejs")
    d = _project(tmp_path)
    url = "ws://127.0.0.1:9999/v1/bridge"
    cmd = t.launch_command(str(d), url, "game.log", map="park")
    assert cmd[1] == threemod.HEADLESS and os.path.isfile(cmd[1])
    assert cmd[cmd.index("--project") + 1] == str(d)
    assert cmd[cmd.index("--bridge") + 1] == url
    assert cmd[cmd.index("--log") + 1] == "game.log"
    assert cmd[cmd.index("--level") + 1] == "park"
    assert t.launch_command(str(d), url, "g.log", exe="http://localhost:4000/")[5] == "http://localhost:4000/"
    # headless.mjs reads the names launch_command gives it
    src = open(threemod.HEADLESS, encoding="utf-8").read()
    for name in ("page", "bridge", "log", "level", "project"):
        assert f"a.{name}" in src, name


class OpsLink:
    """A plugin that answers find/transforms/pose/movement as the bridge does."""

    def __init__(self):
        self.hello = {"project": "glider-park", "engine": "threejs"}
        self.asked = []

    def request(self, op, args=None, timeout=None):
        self.asked.append((op, args))
        if op == "find":
            if args.get("things"):
                return {"objects": ["/Scene/Park", "/Scene/Glider", "/Scene/Park/Hoop#2"]}
            return {"objects": ["/Scene/Park/Hoop", "/Scene/Park/Hoop#2"]}
        if op == "transforms":
            return {"objects": {o: {"location": {"X": 1, "Y": 2, "Z": 3}, "extent": {"X": 5, "Y": 5, "Z": 5}}
                                for o in args["objects"]}}
        if op == "pose":
            return {"path": "/Scene/Glider", "at": {"X": 10, "Y": 0, "Z": 90}, "eye": {"X": -300, "Y": 0, "Z": 200},
                    "yaw": 15, "pitch": -5, "velocity": {"X": 400, "Y": 0, "Z": 0}, "on_ground": False}
        if op == "movement":
            return {"walk_speed": 400, "jump_z": 500, "gravity": 980, "half_height": 90, "step": None, "sources": {}}
        raise AssertionError(op)


def test_game_backend_over_the_bridge_ops():
    game = Game(OpsLink())
    assert game.engine.name == "threejs"
    assert game.all_actors() == ["/Scene/Park", "/Scene/Glider", "/Scene/Park/Hoop#2"]
    assert game.actors("Hoop") == ["/Scene/Park/Hoop", "/Scene/Park/Hoop#2"]
    assert game.name_of("/Scene/Park/Hoop#2") == "Hoop#2"
    assert game.locs(["/Scene/Park/Hoop"]) == {"/Scene/Park/Hoop": Vec(1, 2, 3)}
    p = game.pose()
    assert p["at"] == Vec(10, 0, 90) and p["yaw"] == 15 and p["on_ground"] is False
    assert game.engine.movement(game)["walk_speed"] == 400


def test_prompt_threejs_section_is_general():
    from autopilot.agents import session

    text = session.system_prompt(engines.get("threejs"), "runs/r")
    assert "a video game made in Three.js" in text or "Three.js (a game in the web browser)" in text
    section = text[text.index("## The engine: Three.js"):]
    for needle in ("/Scene/", "userData.class", "KeyboardEvent", "getGamepads", "to_kit", "to_native", "move",
                   "restart", "omitted", "render"):
        assert needle in section, needle
    # nothing of a particular game: the brain stays game-agnostic
    for word in ("Skate", "skater", "grind", "Atlanta", "Beltline", "rail grind"):
        assert not re.search(rf"\b{word}\b", section, re.I), word


def _node_with_websocket():
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "-e", "process.stdout.write(typeof WebSocket)"], capture_output=True, text=True,
                             timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return node if out.stdout.strip() == "function" else None


def test_bridge_over_a_real_websocket(tmp_path):
    """The JavaScript bridge connects to serve_local, says hello as threejs, and answers the ops the brain needs."""
    node = _node_with_websocket()
    if not node:
        pytest.skip("needs Node with a global WebSocket (Node 22 or later)")
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = None
    try:
        url = f"ws://127.0.0.1:{port}/v1/bridge"
        proc = subprocess.Popen([node, FAKE_GAME, url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        link = bridge.serve_local(port=port, wait=30)
        assert link.hello["engine"] == "threejs" and link.hello["share"] is False
        assert link.missing_ops() == []
        game = Game(link)
        assert game.engine.name == "threejs"
        assert game.pawn == "/Scene/Player"
        assert game.actors("Rail") == ["/Scene/Rail", "/Scene/Rail#2"]
        assert game.loc("/Scene/Rail") == Vec(0, 200, 50)
        before = game.pose()
        assert before["at"].z == 90 and before["on_ground"] is True
        game.hold("W")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and game.pose()["at"].x - before["at"].x < 50:
            game.renew()
            time.sleep(0.05)
        game.release("W")
        assert game.pose()["at"].x - before["at"].x >= 50  # (W rolls the stand-in forward: kit +X)
        assert game.get("state", "restarts") == 0
        assert game.restart(wait_for_pawn=5) is True
        assert game.get("state", "restarts") == 1
        assert game.engine.movement(game)["walk_speed"] == 400
        seen = game.sees(["/Scene/Group/Cone"])
        assert "/Scene/Group/Cone" in seen and seen["/Scene/Group/Cone"]["distance"] > 0
    finally:
        if proc:
            proc.kill()
            proc.wait(timeout=10)
