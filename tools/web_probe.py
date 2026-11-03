"""Try every op of a browser game's bridge (plugin/threejs) by hand, without a model: no Jev, no language model, no
key. It waits for the game's page to connect (open the page with ?autopilot=1, or plugin/threejs/headless.mjs), runs
each op once through the kit's Game, and writes what each answered to a JSON report (and a still of the canvas).

    AUTOPILOT_SHARE=0 python tools/web_probe.py --out probe/ [--port 8765] [--key W] [--class Mesh]

Exit code: 0 when every op answered, 1 otherwise.
"""

import argparse
import base64
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from autopilot import bridge  # noqa: E402
from autopilot.kit.game import Game  # noqa: E402
from autopilot.kit.geom import Vec  # noqa: E402


def _plain(v):
    if isinstance(v, Vec):
        return {"X": round(v.x, 2), "Y": round(v.y, 2), "Z": round(v.z, 2)}
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--wait", type=float, default=120.0, help="seconds to wait for the page")
    ap.add_argument("--out", default="probe", help="folder for report.json and still.jpg")
    ap.add_argument("--key", default="W", help="a key that moves the player (held for --hold seconds)")
    ap.add_argument("--hold", type=float, default=2.0)
    ap.add_argument("--class", dest="cls", default="Mesh", help="a class for find")
    args = ap.parse_args(argv)
    if os.environ.get("AUTOPILOT_SHARE") != "0":
        print("note: set AUTOPILOT_SHARE=0 (this tool sends nothing anywhere, but keep the habit)", file=sys.stderr)
    os.makedirs(args.out, exist_ok=True)
    print(f"Waiting for the page on ws://127.0.0.1:{args.port}/v1/bridge ...", flush=True)
    link = bridge.serve_local(port=args.port, wait=args.wait)
    game = Game(link)
    frames = []
    link.frame_listeners.append(lambda f: frames.append({k: f.get(k) for k in ("n", "t", "width", "height")}))
    steps = []

    def step(name, fn):
        t0 = time.monotonic()
        try:
            out = fn()
            steps.append({"op": name, "ok": True, "ms": round((time.monotonic() - t0) * 1000), "answer": _plain(out)})
            print(f"  ok   {name}")
        except Exception as e:  # noqa: BLE001 - every op is tried, its failure recorded
            steps.append({"op": name, "ok": False, "ms": round((time.monotonic() - t0) * 1000),
                          "error": f"{type(e).__name__}: {e}"})
            print(f"  FAIL {name}: {e}")
        return steps[-1].get("answer")

    hello = {k: link.hello.get(k) for k in ("engine", "engine_version", "project", "plugin_version", "features",
                                            "share", "host")}
    print(f"Connected: {hello}")
    step("missing_ops", lambda: link.missing_ops())
    ctx = step("context", lambda: {k: v for k, v in game.context(fresh=True).items() if k != "input"})
    step("ping", lambda: game.request("ping"))
    step("levels", lambda: game.request("levels"))
    things = step("find things (all_actors)", lambda: game.all_actors()) or []
    step(f"find class {args.cls}", lambda: game.actors(args.cls)[:20])
    step("transforms (locs of 5 things)", lambda: game.locs(things[:5]))
    step("bounds of the first thing", lambda: list(game.bounds(things[0])) if things else None)
    start = step("pose", lambda: game.pose())
    step("movement", lambda: game.engine.movement(game))
    view = step("view", lambda: game.view())
    if view:
        steps[-1]["answer"] = {"visible_count": len(view.get("visible", [])),
                               "nearest": [{k: v.get(k) for k in ("name", "path", "class", "distance", "screen_x",
                                                                    "screen_y")} for v in view["visible"][:6]],
                               "camera": view.get("camera"), "body": view.get("body"), "omitted": view.get("omitted")}
    step("sees (the 3 nearest things)", lambda: game.sees([v["path"] for v in (view or {}).get("visible", [])[:3]]))
    step("screen", lambda: game.screen())
    step("ui", lambda: game.ui())
    step("rc describe (context's player)", lambda: {k: v for k, v in game.describe(ctx["character"]).items()
                                                    if k in ("Name", "Class")} if ctx else None)

    def hold():
        before = game.pose()["at"]
        watcher = {}
        th = threading.Thread(target=lambda: watcher.update(game.request(
            "watch", {"objects": [game.pawn], "seconds": args.hold})))
        th.start()
        game.hold(args.key)
        t_end = time.monotonic() + args.hold
        while time.monotonic() < t_end:
            game.renew()
            time.sleep(0.1)
        game.release(args.key)
        th.join()
        after = game.pose()["at"]
        return {"key": args.key, "seconds": args.hold, "from": before, "to": after,
                "moved_cm": round(before.dist(after), 1), "watch": watcher}

    step(f"key hold {args.key} with watch", hold)
    step("move toward kit -X for 2 s", lambda: game.request("move", {"direction": {"X": -1, "Y": 0, "Z": 0},
                                                                    "seconds": 2.0}, timeout=30))
    step("press SpaceBar", lambda: game.press("SpaceBar") or "posted")
    step("pointer at the middle", lambda: game.pointer(0.0, 0.0))

    def still():
        r = game.request("capture", {"max_dim": 960, "quality": 85})
        path = os.path.join(args.out, "still.jpg")
        with open(path, "wb") as f:
            f.write(base64.b64decode(r.pop("jpeg")))
        return {**r, "file": path, "bytes": os.path.getsize(path)}

    step("capture", still)

    def record():
        game.request("record", {"action": "start", "fps": 5, "max_dim": 480})
        time.sleep(2.0)
        out = game.request("record", {"action": "stop"})
        return {**out, "frames_received": len(frames), "first": frames[:1]}

    step("record 2 s at 5 fps", record)
    step("pause", lambda: [game.pause(True), game.pause(False)])
    step("session restart", lambda: {"pawn_back": game.restart(wait_for_pawn=10), "pose": game.pose()})
    step("codex_manifest (expected: unknown op)", lambda: game.request("codex_manifest", {}))
    step("logs and findings so far", lambda: {"logs": link.logs[-5:], "findings": link.findings[-5:],
                                              "heartbeat_age_s": round(time.time() - (link.last_heartbeat or 0), 1)})

    expected_fail = {"codex_manifest (expected: unknown op)"}
    ok = all(s["ok"] for s in steps if s["op"] not in expected_fail)
    report = {"hello": hello, "pose_at_start": _plain(start), "steps": steps, "all_ok": ok,
              "when": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"{sum(s['ok'] for s in steps)}/{len(steps)} ops answered; report: {os.path.join(args.out, 'report.json')}")
    link.server.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
