"""The Phaser adapter (autopilot/engines/phaser.py) without a browser: the project on disk, the codex index, the 2D
frame, the headless launch and the implementer's prompt; and the browser bridge with the Phaser helper
(plugin/phaser/autopilot-phaser.js) over a real WebSocket, in Node with a stand-in Phaser game
(plugin/phaser/test/fake_phaser.mjs), when Node is there."""

import json
import os
import re
import shutil
import socket
import subprocess
import time

import pytest

from autopilot import bridge, engines
from autopilot.engines import threejs as threemod
from autopilot.kit.game import Game
from autopilot.kit.geom import Vec

ROOT = os.path.join(os.path.dirname(__file__), "..")
FAKE_GAME = os.path.join(ROOT, "plugin", "phaser", "test", "fake_phaser.mjs")


def _project(tmp_path, deps):
    d = tmp_path / "runner"
    for sub in ("src/scenes", "node_modules/phaser", "dist"):
        (d / sub).mkdir(parents=True)
    (d / "package.json").write_text(json.dumps({"name": "cave-runner", "scripts": {"dev": "vite"},
                                                "dependencies": deps}))
    (d / "index.html").write_text('<div id="game"></div><script type="module" src="/src/main.js"></script>')
    (d / "src" / "main.js").write_text("import Phaser from 'phaser';\n")
    (d / "src" / "scenes" / "Level.js").write_text("export class Level extends Phaser.Scene {}\n")
    (d / "src" / "autopilot.js").write_text(
        "import { connectBridge } from './autopilot-bridge.js';\n"
        "import { phaserBridgeOptions } from './autopilot-phaser.js';\n"
        "connectBridge(phaserBridgeOptions(game, {}));\n")
    (d / "src" / "autopilot-bridge.js").write_text("// the bridge\n")
    (d / "node_modules" / "phaser" / "phaser.js").write_text("// phaser\n")
    return d


def test_registered_and_chosen_from_hello():
    assert engines.get("phaser").name == "phaser"
    assert engines.for_hello({"engine": "Phaser"}).name == "phaser"
    assert isinstance(engines.get("phaser"), engines.OpEngine)
    assert set(bridge.ENGINE_OPS) <= set(bridge.required_ops({"engine": "phaser"}))


def test_project_is_a_folder_whose_package_uses_phaser(tmp_path):
    p = engines.get("phaser")
    d = _project(tmp_path, {"phaser": "^4.2.0"})
    assert os.path.samefile(p.project_at(str(d)), d)
    assert os.path.samefile(p.project_at(str(d / "package.json")), d)
    assert p.project_at(str(d / "index.html")) is None
    assert p.project_name(str(d)) == "cave-runner"
    eng, project = engines.find_project(str(d))
    assert eng.name == "phaser" and os.path.samefile(project, d)
    # a project with both phaser and three is Phaser's; one with three only stays Three.js'
    both = _project(tmp_path / "both", {"phaser": "4.2.0", "three": "0.180.0"})
    assert engines.find_project(str(both))[0].name == "phaser"
    three = _project(tmp_path / "three", {"three": "0.180.0"})
    assert engines.find_project(str(three))[0].name == "threejs"
    assert p.project_at(str(three)) is None
    assert engines.get("threejs").project_at(str(d)) is None


def test_codex_is_the_source_text_and_its_index_says_where_to_start(tmp_path):
    p = engines.get("phaser")
    assert p.codex_path("src/scenes/Level.js") == "src/scenes/Level.js"
    assert p.codex_path("node_modules/phaser/phaser.js") is None
    assert p.codex_path("src/autopilot-bridge.js") is None
    d = _project(tmp_path, {"phaser": "4.2.0"})
    root = tmp_path / "codex"
    p.copy_source(str(d), str(root))
    p.write_index(str(root), "cave-runner")
    text = (root / "README.md").read_text(encoding="utf-8")
    assert "phaser 4.2.0" in text
    assert "The page loads /src/main.js" in text
    assert "src/autopilot.js" in text  # (where the game gives the bridge its hooks)
    assert "/Game/<scene key>/" in text and "/Scene/" not in text


def test_phaser_2d_frame_values():
    p = engines.get("phaser")
    # Phaser's pixels, y down: kit X right, kit Z up, 1 pixel = 1 unit (the Godot plugin's 2D frame)
    assert p.to_kit({"x": 120, "y": 400}) == Vec(120, 0, -400)
    assert p.to_kit([120, 400]) == Vec(120, 0, -400)
    assert p.to_kit({"x": 3, "y": -4, "z": 0}) == Vec(3, 0, 4)
    assert p.to_kit(None) is None
    assert p.to_native(Vec(120, 0, -400)) == {"x": 120, "y": 400}
    for v in (Vec(120, 0, -35.5), Vec(0, 0, 0), Vec(-1, 0, 3)):
        assert p.to_kit(p.to_native(v)) == v
    # a clockwise turn on screen (Phaser's positive rotation) is a pitch down
    yaw, pitch = p.rotation_to_kit(1.5707963267948966)
    assert yaw == 0 and abs(pitch + 90) < 1e-9
    assert abs(p.rotation_to_kit({"rotation": -0.5})[1] - 28.6478897) < 1e-6
    for pitch in (-80, -10, 0, 25, 170):
        assert abs(p.rotation_to_kit(p.rotation_to_native(0, pitch))[1] - pitch) < 1e-9


def test_launch_command_is_the_web_pages(tmp_path):
    p = engines.get("phaser")
    d = _project(tmp_path, {"phaser": "4.2.0"})
    cmd = p.launch_command(str(d), "ws://127.0.0.1:9999/v1/bridge", "game.log", map="Level2")
    assert cmd[1] == threemod.HEADLESS and os.path.isfile(cmd[1])
    assert cmd[cmd.index("--project") + 1] == str(d)
    assert cmd[cmd.index("--level") + 1] == "Level2"


class OpsLink:
    """A plugin that answers find/transforms/pose as the bridge does for a Phaser game."""

    def __init__(self):
        self.hello = {"project": "cave-runner", "engine": "phaser"}

    def request(self, op, args=None, timeout=None):
        if op == "find":
            if args.get("things"):
                return {"objects": ["/Game/Level/player", "/Game/Level/coins/coin#2"]}
            return {"objects": ["/Game/Level/coins/coin", "/Game/Level/coins/coin#2"]}
        if op == "transforms":
            return {"objects": {o: {"location": {"X": 400, "Y": 0, "Z": -300}, "extent": {"X": 8, "Y": 0, "Z": 8}}
                                for o in args["objects"]}}
        if op == "pose":
            return {"path": "/Game/Level/player", "at": {"X": 100, "Y": 0, "Z": -368}, "yaw": 0, "pitch": 0,
                    "velocity": {"X": 200, "Y": 0, "Z": 0}, "on_ground": True}
        raise AssertionError(op)


def test_game_backend_over_the_bridge_ops():
    game = Game(OpsLink())
    assert game.engine.name == "phaser"
    assert game.all_actors() == ["/Game/Level/player", "/Game/Level/coins/coin#2"]
    assert game.actors("Coin") == ["/Game/Level/coins/coin", "/Game/Level/coins/coin#2"]
    assert game.name_of("/Game/Level/coins/coin#2") == "coin#2"
    assert game.loc("/Game/Level/coins/coin") == Vec(400, 0, -300)
    assert game.pose()["at"] == Vec(100, 0, -368)


def test_prompt_phaser_section_is_general():
    from autopilot.agents import session

    text = session.system_prompt(engines.get("phaser"), "runs/r")
    assert "Phaser (a 2D game in the web browser)" in text
    section = text[text.index("## The engine: Phaser"):]
    for needle in ("2D", "/Game/", "setData", "KeyboardEvent", "getGamepads", "to_kit", "to_native", "move",
                   "restart", "label", "preserveDrawingBuffer", "omitted"):
        assert needle in section, needle
    # nothing of a particular game: the brain stays game-agnostic
    for word in ("RenderATL", "Rush", "freight", "Atlanta", "RenderMan"):
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


def test_bridge_over_a_real_websocket():
    """The bridge with the Phaser helper connects to serve_local, says hello as phaser, and plays in 2D."""
    node = _node_with_websocket()
    if not node:
        pytest.skip("needs Node with a global WebSocket (Node 22 or later)")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = None
    try:
        url = f"ws://127.0.0.1:{port}/v1/bridge"
        proc = subprocess.Popen([node, FAKE_GAME, url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        link = bridge.serve_local(port=port, wait=30)
        assert link.hello["engine"] == "phaser" and link.hello["share"] is False
        assert link.missing_ops() == []
        game = Game(link)
        assert game.engine.name == "phaser"
        assert game.pawn == "/Game/Level/player"
        assert game.actors("Coin") == ["/Game/Level/coins/coin", "/Game/Level/coins/coin#2"]
        assert game.loc("/Game/Level/coins/coin#2") == Vec(400, 0, -300)
        before = game.pose()
        assert before["at"] == Vec(100, 0, -368) and before["on_ground"] is True  # (the middle of its box)
        assert game.request("pose")["feet"] == {"X": 100, "Y": 0, "Z": -400}
        game.hold("D")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and game.pose()["at"].x - before["at"].x < 50:
            game.renew()
            time.sleep(0.05)
        game.release("D")
        assert game.pose()["at"].x - before["at"].x >= 50  # (D runs the stand-in right: kit +X)
        assert game.pose()["at"].z == before["at"].z
        assert game.restart(wait_for_pawn=5) is True
        assert game.get("/Game/Level/player", "x") == 100
        # game.get reads Phaser's own values (pixels, y down); to_kit turns them into the kit's frame
        native = {"x": game.get("/Game/Level/player", "x"), "y": game.get("/Game/Level/player", "y")}
        assert game.to_kit(native) == game.loc("/Game/Level/player") == Vec(100, 0, -400)
        seen = game.sees(["/Game/Level/crate"])
        assert "/Game/Level/crate" in seen
    finally:
        if proc:
            proc.kill()
            proc.wait(timeout=10)
