"""Ask the chosen decision model (AUTOPILOT_DECISION_PROVIDER) a few play-like questions and print the answers and
the latency. It reads the keys from Autopilot/autopilot.env, as a run does, and never prints them.

    AUTOPILOT_DECISION_PROVIDER=anthropic python tools/decision_smoke.py [calls]
"""

import statistics
import sys
import time

from typesafe_sdk import Choice, Noul

from autopilot import account, decision

STATE = {
    "goal": "Reach the exit door at the end of the level",
    "situation": {"health": 30, "enemy": {"distance": 4.0, "bearing": "ahead"}, "door": {"distance": 22.0}},
    "options": {"attack": {"enemy_distance": 4.0}, "retreat": {"cover_distance": 3.0}, "run_to_door": {}},
    "now_doing": None,
    "recent": [{"option": "run_to_door", "worked": False, "why": "hit by the enemy"}],
}
QUESTIONS = {
    "pick": Choice(instructions="Choose the option to do now that best serves `goal`.",
                   criteria={"attack": "Fight the nearest enemy", "retreat": "Back off to cover",
                             "run_to_door": "Run straight to the exit door"}),
    "hurt": Noul(instructions="The player is badly hurt."),
}


def main(calls=5):
    account.load_user_settings()
    print(f"provider: {decision.provider()}")
    client = decision.client(warm=False)
    seconds = []
    for i in range(calls):
        t = time.monotonic()
        r = client.system_one(state=STATE, questions=QUESTIONS)
        seconds.append(time.monotonic() - t)
        pick = r.choices["pick"]
        probs = {k: round(v, 2) for k, v in (pick.probabilities or {}).items()}
        tokens = f"{getattr(r.usage, 'input_tokens', None)}/{getattr(r.usage, 'output_tokens', None)}"
        print(f"{i + 1}: {pick.choice} {probs} hurt={r.nouls['hurt'].noul:.2f} {seconds[-1]:.2f}s "
              f"model={r.model} tokens={tokens}")
    print(f"latency: median {statistics.median(seconds):.2f}s, max {max(seconds):.2f}s")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
