#!/usr/bin/env python3
"""Ask the same question from a text view and a rendered image, and compare.

Runs on /chat/completions rather than the DE's native template, because that is
the only path that accepts an image. Both arms of the comparison use the same
path, so the prompt format is held constant and the only difference is the
representation.

Results are cached in vprobe_cache.json after every call, so a timeout costs one
request rather than the run.

  vprobe.py --models shisa-ai/shisa-de-2,qwen3.8-max
  vprobe.py --report
"""
import base64
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import view  # noqa: E402
import oracle  # noqa: E402

BASE = "https://api.shisa.ai/openai/v1"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vprobe_cache.json")

OPTIONS = [("A", "left"), ("B", "right"), ("C", "up"), ("D", "down"),
           ("E", "up-left"), ("F", "up-right"), ("G", "down-left"),
           ("H", "down-right"), ("I", "stay put")]
NAMES = dict(OPTIONS)
LISTING = "\n".join("%s %s" % (l, d) for l, d in OPTIONS)
QUESTION = "Which way should the player move?"

IMG_NOTE = (
    "White disc = the player, which can move 4 units per frame in any of the 8 "
    "directions or stay put. Green dots = bullets; each bullet keeps moving in a "
    "straight line at constant speed, and the faint trail shows where it will be "
    "over the next 15 frames. Grey border = the edge of the play field.")
TXT_NOTE = ("The grid below shows the play field; each cell is 12 units. Bullets "
            "are shown at their current position and along their future path over "
            "the next 15 frames.")

TAIL = ("\n\n%s\n\nOptions:\n%s\n\n"
        "Think briefly if you need to, then end your reply with a final line of "
        "exactly this form:\nANSWER: <letter>")


def load():
    try:
        return json.load(open(CACHE))
    except (OSError, ValueError):
        return {}


def save(cache):
    json.dump(cache, open(CACHE, "w"), indent=1, sort_keys=True)


def ask(model, prompt, png=None, attempts=3, timeout=240):
    parts = [{"type": "text", "text": prompt}]
    if png:
        b64 = base64.b64encode(open(png, "rb").read()).decode()
        parts.append({"type": "image_url",
                      "image_url": {"url": "data:image/png;base64," + b64}})
    body = json.dumps({"model": model, "max_tokens": 2000, "temperature": 0,
                       "messages": [{"role": "user", "content": parts}]}).encode()
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(BASE + "/chat/completions", data=body,
                                         headers={
                "Authorization": "Bearer " + os.environ["SHISA_API_KEY"],
                "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                msg = json.load(r)["choices"][0]["message"]
            txt = (msg.get("content") or "") + "\n" + (msg.get("reasoning_content") or "")
            m = re.findall(r"ANSWER:\s*([A-I])", txt)
            if m:
                return m[-1], txt, "answer"
            # No explicit answer line: only trust a bare letter in a short reply,
            # since the last A-I in a long chain of thought is usually noise.
            allm = re.findall(r"\b([A-I])\b", txt)
            if allm and len(txt) < 200:
                return allm[-1], txt, "fallback"
            return "?", txt, "unparsed"
        except Exception as e:                       # noqa: BLE001
            last = "%s: %s" % (type(e).__name__, e)
            time.sleep(2 * (i + 1))
    return "ERR", str(last), "error"


def run(models, retry_bad=False):
    cache = load()
    scen = list(view.SCENARIOS)
    for model in models:
        for arm in ("text", "image"):
            for s in scen:
                key = "%s|%s|%s" % (model, arm, s)
                if key in cache and not (retry_bad and
                                         cache[key].get("letter") in ("?", "ERR")):
                    continue
                if arm == "image":
                    png = "vprobe_%s.png" % s
                    view.render_model_png(view.SCENARIOS[s], png, scale=2)
                    prompt = IMG_NOTE + TAIL % (QUESTION, LISTING)
                else:
                    png = None
                    prompt = TXT_NOTE + "\n\n" + view.render(view.SCENARIOS[s]) + \
                        TAIL % (QUESTION, LISTING)
                letter, raw, src = ask(model, prompt, png)
                cache[key] = {"letter": letter, "raw": raw[-600:], "src": src}
                save(cache)
                print("%-22s %-6s %-22s -> %-4s (%s)"
                      % (model, arm, s, letter, src), flush=True)
    return cache


def report(cache):
    scen = list(view.SCENARIOS)
    t15 = {s: oracle.safe_moves(view.SCENARIOS[s], 15) for s in scen}
    t45 = {s: oracle.safe_moves(view.SCENARIOS[s], 45) for s in scen}
    stay15 = sum("stay put" in t15[s] for s in scen)
    stay45 = sum("stay put" in t45[s] for s in scen)
    print("baseline always 'stay put': 15f %d/%d   45f %d/%d\n"
          % (stay15, len(scen), stay45, len(scen)))

    models = sorted({k.split("|")[0] for k in cache})
    for model in models:
        print("=== %s" % model)
        picks = {}
        for arm in ("text", "image"):
            picks[arm] = {}
            for s in scen:
                e = cache.get("%s|%s|%s" % (model, arm, s))
                picks[arm][s] = NAMES.get(e["letter"], e["letter"]) if e else "(none)"
        for s in scen:
            print("  %-22s text=%-11s%s image=%-11s%s" % (
                s, picks["text"][s], "  " if picks["text"][s] in t15[s] else "XX",
                picks["image"][s], "  " if picks["image"][s] in t15[s] else "XX"))
        for arm in ("text", "image"):
            p = picks[arm]
            print("  -> %-5s  15f %2d/%d   45f %2d/%d" % (
                arm, sum(p[s] in t15[s] for s in scen), len(scen),
                sum(p[s] in t45[s] for s in scen), len(scen)))
        print()


def main():
    argv = sys.argv[1:]
    cache = load()
    if "--report" not in argv:
        models = (argv[argv.index("--models") + 1].split(",")
                  if "--models" in argv else ["shisa-ai/shisa-de-2"])
        cache = run(models, retry_bad="--retry-bad" in argv)
    report(cache)


if __name__ == "__main__":
    main()
