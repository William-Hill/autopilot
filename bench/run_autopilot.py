"""Run prompts through Autopilot against a headless copy of a game, and score them.

    python bench/run_autopilot.py --project <copy>/Game.uproject --prompt "Open the gate"
    python bench/run_autopilot.py --project <copy>/Game.uproject --suite bench/example.json [--only A,B] [--parallel 2]
        [--codex memory/<project>/codex] [--warm <memory dir>] [--minutes 30] [--out <dir>] [--model ...]
        [--agent opencode|claude|codex]

--project is any engine's project: an Unreal .uproject, a Unity project folder or a Godot project folder. The game
runs headless as its engine's adapter says (Engine.launch_command): Unreal `-game -nullrhi` (or a packaged build given
with --engine <Game.exe>); Unity a player build given with --engine <Game.exe> (`-batchmode -nographics`); Godot
`--headless --path <folder>` (or an exported build given with --engine <Game.exe>). --engine: the engine's (or the
build's) executable, when not the adapter's default.

A suite is a JSON file: {"prompts": [{"id": "A", "prompt": "...", "expected": "CONFIRMED"}, ...]} ("expected" is
optional; bench/example.json shows the format).

Each prompt gets its own folder and its own memory (AUTOPILOT_MEMORY): cold by default (only the project's codex, which
comes from the project's files, not from play, is copied in), or --warm <dir>, shared one prompt after another
(notes.md, lib.py and earlier runs carry over). A brain (`python -m autopilot play`) listens on its own port; the game
runs headless and connects to it. Results go to bench/results/<time>/summary.json.

Each brain has its own empty settings folder (<prompt>/settings), so runs use the keys in .env and the environment:
never this machine's first-run setup (an Aura choice would send every prompt through Aura, restoring and saving the
team's memory and billing the account) nor its autopilot.env. On macOS the brain's settings folder can't be moved:
sign out of Aura there first.

Use a copy of your game, not the project you work in: runs write to the game's Saved folder, and the bench starts
and stops the game. Headless games still use plenty of memory (several GB each): keep --parallel low.
"""

import argparse
import concurrent.futures as futures
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from autopilot import engines, processes  # noqa: E402

PYTHON = sys.executable


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_one(item, args, root):
    work = os.path.join(root, item["id"])
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    eng, path = args.engine_of, args.project_path
    project = eng.project_name(path)
    memory = args.warm or os.path.join(work, "memory")
    pdir = os.path.join(memory, project)
    os.makedirs(pdir, exist_ok=True)
    if args.codex and not os.path.isdir(os.path.join(pdir, "codex")):
        shutil.copytree(args.codex, os.path.join(pdir, "codex"))
    port = free_port()
    started = time.time()
    # (the brain's own budget follows --minutes too: without it a run believes it has the default 30 whatever the
    # bench waits for)
    # (AUTOPILOT_CONFIRM_COST=0: nobody answers the brain's question before a run; AUTOPILOT_MAX_USD still applies)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", AUTOPILOT_MEMORY=memory, AUTOPILOT_CONFIRM_COST="0",
               AUTOPILOT_TIMEOUT=str(int(args.minutes * 60)))
    settings = os.path.join(work, "settings")  # (bridge.brain_file's base: the game keeps the real one, for its DDC)
    os.makedirs(settings)
    env["LOCALAPPDATA" if os.name == "nt" else "XDG_CONFIG_HOME"] = settings
    if args.model:
        env["AUTOPILOT_MODEL"] = args.model
    if args.agent:
        env["AUTOPILOT_AGENT"] = args.agent
    brain_log = open(os.path.join(work, "brain.log"), "w", encoding="utf-8")
    brain = subprocess.Popen([PYTHON, "-u", "-m", "autopilot", "play", item["prompt"], "--port", str(port),
                              "--project", path],
                             cwd=HERE, stdout=brain_log, stderr=subprocess.STDOUT, env=env, **processes.GROUP)
    processes.track(brain)
    time.sleep(2)
    game = subprocess.Popen(
        eng.launch_command(path, f"ws://127.0.0.1:{port}/v1/bridge", os.path.join(work, "game.log"), map=args.map,
                           exe=args.engine) + fps_cap(eng, args.fps),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **processes.GROUP)
    processes.track(game)
    try:
        deadline = started + args.minutes * 60
        while time.time() < deadline and brain.poll() is None:
            time.sleep(2)
        timed_out = brain.poll() is None
    finally:
        for p in (brain, game):
            processes.kill(p)
        brain_log.close()
    reports = sorted(glob.glob(os.path.join(pdir, "runs", "*", "report.json")), key=os.path.getmtime)
    if not reports and not args.warm:
        # (the plugin named the project otherwise than its folder: this prompt's memory holds that project alone)
        reports = sorted(glob.glob(os.path.join(memory, "*", "runs", "*", "report.json")), key=os.path.getmtime)
    report = {}
    if reports:
        with open(reports[-1], encoding="utf-8") as f:
            report = json.load(f)
    got = report.get("outcome") or ("TIMEOUT" if timed_out else "ERROR")
    expected = item.get("expected")
    return {"id": item["id"], "prompt": item["prompt"], "expected": expected, "got": got,
            "correct": (got == expected) if expected else None, "summary": report.get("summary", ""),
            "evidence": report.get("evidence", [])[:4], "seconds": round(time.time() - started),
            "implementer_seconds": report.get("seconds"), "episodes": report.get("episodes"),
            "cost_usd": report.get("cost_usd"), "turns": report.get("turns"), "agent": report.get("agent"),
            "dir": work}


def fps_cap(eng, fps):
    """The game's command-line arguments that cap its frame rate (an editor on a modest machine runs the game at about
    20 fps, a headless game much faster: play must work at both)."""
    if not fps:
        return []
    if eng.name == "unreal":
        return [f"-ExecCmds=t.MaxFPS {fps:g}"]
    sys.exit(f"--fps: no frame-rate cap for {eng.title or eng.name} yet")


def main():
    sys.stdout.reconfigure(errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", required=True)
    p.add_argument("--prompt")
    p.add_argument("--suite")
    p.add_argument("--only", default="")
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--codex", help="a codex folder to start each prompt's memory with")
    p.add_argument("--warm", help="a memory folder the prompts share, one after another")
    p.add_argument("--minutes", type=float, default=30)
    p.add_argument("--engine", help="the executable that runs the game (default: the engine adapter's)")
    p.add_argument("--map")
    p.add_argument("--fps", type=float, help="cap the game's frame rate (e.g. 20, as an editor plays)")
    p.add_argument("--model")
    p.add_argument("--agent", choices=["opencode", "claude", "codex"],
                   help="the agent (default: AUTOPILOT_AGENT, else opencode)")
    p.add_argument("--out")
    p.add_argument("--quiet", action="store_true",
                   help="print only verdicts, times and costs (no summaries: for a held-out test set)")
    args = p.parse_args()
    args.engine_of, args.project_path = engines.find_project(args.project)
    if not args.engine_of:
        sys.exit(f"no Unreal, Unity or Godot project at {args.project}")
    try:  # (an engine whose headless launch isn't there yet says so before anything starts)
        args.engine_of.launch_command(args.project_path, "ws://127.0.0.1:0/v1/bridge", "game.log", map=args.map,
                                      exe=args.engine)
    except NotImplementedError as e:
        sys.exit(str(e))
    fps_cap(args.engine_of, args.fps)  # (an engine without a cap says so before anything starts)
    processes.contain()
    items = []
    if args.prompt:
        items.append({"id": "P", "prompt": args.prompt})
    if args.suite:
        spec = json.load(open(args.suite, encoding="utf-8"))
        for c in spec.get("prompts", []):
            items.append({"id": c["id"], "prompt": c["prompt"], "expected": c.get("expected")})
    if args.only:
        keep = set(args.only.split(","))
        items = [i for i in items if i["id"] in keep]
    if args.warm:
        args.parallel = 1
    root = args.out or os.path.join(HERE, "bench", "results", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(root, exist_ok=True)
    print(f"{len(items)} prompt(s), {args.parallel} at a time -> {root}", flush=True)
    results = []
    lock = threading.Lock()
    with futures.ThreadPoolExecutor(args.parallel) as pool:
        for job in futures.as_completed([pool.submit(run_one, i, args, root) for i in items]):
            r = job.result()
            with lock:
                results.append(r)
                mark = "-  " if r["correct"] is None else "ok " if r["correct"] else "MISS"
                print(f"  {r['id']:4} expected {str(r['expected']):12} got {r['got']:13} {mark} {r['seconds']:>5}s "
                      f"${r['cost_usd'] or 0:.2f}" + ("" if args.quiet else f"  {r['summary'][:140]}"), flush=True)
    results.sort(key=lambda r: r["id"])
    scored = [r for r in results if r["correct"] is not None]
    summary = {"project": args.project, "score": sum(r["correct"] for r in scored), "total": len(scored),
               "results": results}
    with open(os.path.join(root, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    if scored:
        print(f"score {summary['score']}/{summary['total']}")


if __name__ == "__main__":
    main()
