#!/usr/bin/env python3
"""The DE-2 readout, as its spec states it. Text and image.

This is the contract from the DE-2 readout page, not the DE-1 one. The
difference that matters: the user turn is written TWICE, separated by a fixed
sentence. Reading a DE-2 model with the single DE-1 prompt changes answers --
on the eight hand-built scenarios it cost three of eight -- so it is worth
keeping straight.

Text goes to /completions with max_tokens=1. Images go to /chat/completions,
also with max_tokens=1, and per the spec an image question is a single direct
read: no repeat, no thinking, at most 26 options, and no fallback, because
appending a letter does not reproduce the same multimodal answer boundary.

Nothing here generates prose. The answer is read out of the option letters'
logprobs at the first generated position.
"""
import base64
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_ENDPOINT = os.environ.get("SHISA_ENDPOINT",
                                  "https://api.shisa.ai/openai/v1")
DEFAULT_MODEL = "shisa-ai/shisa-de-2"

# Byte for byte from the readout. The model was trained with this line.
SYSTEM = ("Apply the supplied criterion to the supplied evidence. Choose exactly "
          "one listed option. Respond with only its uppercase letter, with no "
          "explanation or reasoning.")
REPEAT = "\n\nRead the same input again before answering:\n"

LETTERS = [chr(65 + i) for i in range(26)]


class ReadError(RuntimeError):
    pass


def payload(evidence, criterion, options):
    """options: list of (letter, description) pairs, in answer order."""
    return json.dumps({"evidence": evidence, "criterion": criterion,
                       "options": [{"letter": l, "description": d}
                                   for l, d in options]}, ensure_ascii=False)


def text_prompt(evidence, criterion, options):
    body = payload(evidence, criterion, options)
    return ("<bos><|turn>system\n" + SYSTEM + "<turn|>\n"
            "<|turn>user\n" + body + REPEAT + body + "<turn|>\n"
            "<|turn>model\n<|channel>thought\n<channel|>")


def _post(url, body, key, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _softmax(logprobs):
    peak = max(logprobs.values())
    w = {k: math.exp(v - peak) for k, v in logprobs.items()}
    total = sum(w.values())
    return {k: v / total for k, v in w.items()}


def _finish(letters, logprobs, options, extra):
    probs = _softmax(logprobs)
    best = max(probs, key=probs.get)
    desc = dict(options)[best]
    out = {"letter": best, "choice": desc, "probabilities": probs,
           "logprobs": logprobs}
    out.update(extra)
    return out


def read_text(evidence, criterion, options, model=DEFAULT_MODEL,
              endpoint=None, key=None, timeout=60, allow_fallback=True):
    """One DE-2 text question. Returns the chosen option and its probabilities."""
    key = key or os.environ.get("SHISA_API_KEY")
    if not key:
        raise ReadError("SHISA_API_KEY is not set")
    endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
    letters = [l for l, _ in options]
    prompt = text_prompt(evidence, criterion, options)
    t0 = time.perf_counter()
    resp = _post(endpoint + "/completions", {
        "model": model, "prompt": prompt, "max_tokens": 1, "temperature": 0,
        "logprobs": 20}, key, timeout)
    ms = (time.perf_counter() - t0) * 1000
    top = resp["choices"][0]["logprobs"]["top_logprobs"][0]
    logprobs = {l: top[l] for l in letters if l in top}

    requests = 1
    missing = [l for l in letters if l not in logprobs]
    if missing and not allow_fallback:
        raise ReadError("letters missing from top-20: %s" % missing)
    for l in missing:
        # The DE-1 fallback, which the DE-2 page keeps for text: ask for the
        # token's own logprob at the answer boundary.
        fb = _post(endpoint + "/completions", {
            "model": model, "prompt": prompt + l, "max_tokens": 1,
            "temperature": 0, "prompt_logprobs": 0}, key, timeout)
        entries = fb["choices"][0]["prompt_logprobs"][-1]
        if not entries:
            raise ReadError("no prompt_logprobs for %s" % l)
        logprobs[l] = next(iter(entries.values()))["logprob"]
        requests += 1

    return _finish(letters, logprobs, options,
                   {"ms": ms, "requests": requests, "missing_from_top": missing,
                    "usage": resp.get("usage", {}), "reported_model":
                    resp.get("model")})


def read_image(image_path, evidence, criterion, options, model=DEFAULT_MODEL,
               endpoint=None, key=None, timeout=60, top_logprobs=20):
    """One DE-2 image question: a single direct read, no repeat, no thinking."""
    key = key or os.environ.get("SHISA_API_KEY")
    if not key:
        raise ReadError("SHISA_API_KEY is not set")
    endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
    letters = [l for l, _ in options]
    if len(letters) > 26:
        raise ReadError("an image question takes at most 26 options, got %d"
                        % len(letters))
    b64 = base64.b64encode(open(image_path, "rb").read()).decode()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + b64}},
                {"type": "text", "text": payload(evidence, criterion, options)}]}],
        "max_tokens": 1, "temperature": 0, "logprobs": True,
        "top_logprobs": top_logprobs, "prompt_logprobs": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    t0 = time.perf_counter()
    resp = _post(endpoint + "/chat/completions", body, key, timeout)
    ms = (time.perf_counter() - t0) * 1000
    content = resp["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
    logprobs = {e["token"]: e["logprob"] for e in content
                if e.get("token") in letters}
    missing = [l for l in letters if l not in logprobs]
    if missing:
        # No fallback exists on this path. Raising beats normalising an
        # incomplete distribution.
        raise ReadError("letters missing from the image read: %s -- raise "
                        "top_logprobs or use fewer options" % missing)
    return _finish(letters, logprobs, options,
                   {"ms": ms, "requests": 1, "missing_from_top": [],
                    "usage": resp.get("usage", {}), "reported_model":
                    resp.get("model")})


MOVE_OPTIONS = [("A", "left"), ("B", "right"), ("C", "up"), ("D", "down"),
                ("E", "up-left"), ("F", "up-right"), ("G", "down-left"),
                ("H", "down-right"), ("I", "stay put")]
MOVE_QUESTION = "Which way should the player move?"


def move_options(stay_put=True):
    """The move menu. Dropping `stay put` forces a move on every decision.

    Worth doing deliberately. With it offered, the model chose it on 80 of 100
    live decisions, which pins the player at the bottom of the field. Danger
    then sits above it in every frame, the safe set skews toward down, and
    every constant "do not move up" policy scores as well as the model does.
    """
    return [o for o in MOVE_OPTIONS if stay_put or o[1] != "stay put"]


def main():
    import view
    argv = sys.argv[1:]
    name = argv[argv.index("--scenario") + 1] if "--scenario" in argv \
        else "column_on_player"
    st = view.SCENARIOS[name]
    if "--image" in argv:
        view.render_model_png(st, "de_client_tmp.png", scale=1)
        ans = read_image("de_client_tmp.png", {"field": "see image"},
                         MOVE_QUESTION, MOVE_OPTIONS)
    else:
        ans = read_text(view.render_as(st, "crop"), MOVE_QUESTION, MOVE_OPTIONS)
    print("%s -> %s (%.3f) in %.0f ms, %d request(s)"
          % (name, ans["choice"], ans["probabilities"][ans["letter"]],
             ans["ms"], ans["requests"]))
    print("   probabilities: %s" % {k: round(v, 3) for k, v in
                                    sorted(ans["probabilities"].items(),
                                           key=lambda kv: -kv[1])})


if __name__ == "__main__":
    main()
