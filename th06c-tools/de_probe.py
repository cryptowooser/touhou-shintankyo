#!/usr/bin/env python3
"""Ask the Shisa decision engine for a move, given a rendered state view.

Talks to the completions endpoint directly with urllib, so it needs no SDK. The
readout contract is the one in the shisa-de-1 model card: render the state and
the question with the option list, take one token, and restrict the letter
logits to the valid option letters.

  de_probe.py --all
  de_probe.py --scenarios open,column_on_player
  de_probe.py --all --question fire
  de_probe.py --all --model shisa-ai/shisa-de-2

The API key is read from SHISA_API_KEY and never printed.
"""
import json
import math
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view  # noqa: E402

ENDPOINT = os.environ.get("SHISA_ENDPOINT",
                          "https://api.shisa.ai/openai/v1/completions")
MODEL = "shisa-ai/shisa-de-1"

SYSTEM = ("Apply the supplied criterion to the supplied evidence. Choose exactly "
          "one listed option. Respond with only its uppercase letter, with no "
          "explanation or reasoning.")

MOVE_OPTIONS = [
    ("A", "left"), ("B", "right"), ("C", "up"), ("D", "down"),
    ("E", "up-left"), ("F", "up-right"), ("G", "down-left"), ("H", "down-right"),
    ("I", "stay put"),
]
FIRE_OPTIONS = [("A", "fire"), ("B", "do not fire")]
BOMB_OPTIONS = [("A", "bomb now"), ("B", "do not bomb")]

QUESTIONS = {
    "move": ("Which way should the player move?", MOVE_OPTIONS),
    "fire": ("Should the player fire?", FIRE_OPTIONS),
    "bomb": ("Should the player use a bomb?", BOMB_OPTIONS),
}


def build_prompt(evidence, criterion, options):
    payload = json.dumps({
        "evidence": evidence,
        "criterion": criterion,
        "options": [{"letter": l, "description": d} for l, d in options],
    })
    return ("<bos><|turn>system\n" + SYSTEM + "<turn|>\n"
            "<|turn>user\n" + payload + "<turn|>\n"
            "<|turn>model\n<|channel>thought\n<channel|>")


def ask(evidence, criterion, options, model=MODEL, timeout=60):
    """One decision. Returns (letters, probabilities, reported_model, usage)."""
    body = json.dumps({"model": model,
                       "prompt": build_prompt(evidence, criterion, options),
                       "max_tokens": 1, "temperature": 0,
                       "logprobs": 20}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": "Bearer " + os.environ["SHISA_API_KEY"],
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.load(r)

    top = resp["choices"][0]["logprobs"]["top_logprobs"][0]
    letters = [l for l, _ in options]
    rows = [(l, top[l]) for l in letters if l in top]
    missing = [l for l in letters if l not in top]
    if missing:
        raise RuntimeError("letters missing from top_logprobs: %s" % missing)

    m = max(v for _, v in rows)
    weights = [(l, math.exp(v - m)) for l, v in rows]
    total = sum(w for _, w in weights)
    probs = [(l, w / total) for l, w in weights]
    probs.sort(key=lambda t: -t[1])
    return probs, resp.get("model"), resp.get("usage", {})


def describe(probs, options, top_n=4):
    names = dict(options)
    out = []
    for letter, p in probs[:top_n]:
        out.append("%s=%.3f" % (names[letter], p))
    return "  ".join(out)


def main():
    argv = sys.argv[1:]
    model = argv[argv.index("--model") + 1] if "--model" in argv else MODEL
    question = argv[argv.index("--question") + 1] if "--question" in argv else "move"

    if "--scenarios" in argv:
        names = argv[argv.index("--scenarios") + 1].split(",")
    else:
        names = list(view.SCENARIOS)

    criterion, options = QUESTIONS[question]
    print("question: %s   requested model: %s\n" % (criterion, model))

    for name in names:
        text = view.render(view.SCENARIOS[name])
        probs, reported, usage = ask(text, criterion, options, model)
        print("%-24s %s" % (name, describe(probs, options)))
        print("%-24s   reported=%s  prompt_tokens=%s"
              % ("", reported, usage.get("prompt_tokens")))


if __name__ == "__main__":
    main()
