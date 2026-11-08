# Playtest evidence: four decision models (2026-11-07)

This folder holds the evidence for `docs/research/decision-models.md`, sections 4 and 5. Each run used the user's
own keys. No call went through Aura.

## Whole game (`whole-game/`)

- Game: RenderATL Rush (Phaser 4), commit `b060a24`, with `?render3d=1` (the 3D runner, enemies, boss and drive).
- Bridge: a dev-only copy of `plugin/threejs/autopilot-bridge.js` and `plugin/phaser/autopilot-phaser.js` in the
  game, loaded with `?autopilot=1`.
- Harness: `harness.py.txt`. The Claude Code agent (`claude-opus-5-5`, fast mode off) wrote it once, with Jev.
  It is stored as `.txt` because it uses `from autopilot.kit import *`, which this repo's ruff settings refuse.
- Play: `autopilot run harness.py --seconds 480`, once for each provider, in a Chromium window with background
  throttling off.

| Provider (`AUTOPILOT_DECISION_PROVIDER`) | Model | Result | Play time | Decisions | Latency median / p90 / max | Errors | Lives lost |
|---|---|---|---|---|---|---|---|
| `jev` | `jev-1.13.0` | Reached the credits | 178.5 s | 187 | 0.15 / 0.33 / 0.92 s | 0 | 2 |
| `clef` | `clef` | Reached the credits | 178.4 s | 193 | 0.35 / 0.70 / 1.73 s | 0 | 2 |
| `openai` | `gpt-6-luna` | Reached the credits | 186.0 s | 203 | 0.22 / 0.48 / 1.90 s | 0 | 2 |
| `anthropic` | `claude-haiku-5-5` | Reached the credits | 197.4 s | 189 | 0.87 / 1.35 / 2.48 s | 1 timeout | 2 |

- `metrics.json`: the numbers above, from the runs' traces (one `decision` event for each answer).
- `summary-<provider>.txt`: the `autopilot run` output for each provider (the plan, the options, every decision
  in order, and the situation every ~5 s).

## Freight-lift slice (`freight-lift-slice/`)

One level slice of the same game, with a harness from an earlier Autopilot run:

| Provider | Result | Clear time | Decisions | Latency median | Errors |
|---|---|---|---|---|---|
| `jev` | Cleared | 42.37 s | 95 | 0.12 s | 0 |
| `clef` | Cleared | 39.8 s | 278 | 0.47 s | 5 read timeouts (2.0 s) |

## Limits

- One run for each provider. The harness code does most of the play, and the decision model chooses between
  options, so these runs show that each provider works, not which model plays best.
- The full traces (about 560 KB each) are not in the repository.

## Reproduce

1. Put the keys in `autopilot.env`: `TYPESAFE_API_KEY`, `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`,
   `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`.
2. Start the game's dev server, and open it with `?autopilot=1&cutscene=0&render3d=1` in a window that the browser
   does not throttle.
3. Run `AUTOPILOT_DECISION_PROVIDER=<provider> python -m autopilot run harness.py --seconds 480`.
4. Reload the game page before the next provider.
