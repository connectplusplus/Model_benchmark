#!/usr/bin/env python3
"""
Token-cost benchmark across three Claude models on frontier-oriented tasks.

What it measures: the dollar cost of a single-shot code generation that actually
passes a unit-test gate, averaged over N trials per (model, task). It does NOT
measure full agentic / Claude Code session cost, which is a larger, separate
number. Say that plainly in the blog so the scope is honest.

Run:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-...
    python benchmark.py --trials 5

Outputs:
    results_raw.csv      one row per trial
    results_summary.csv  one row per (model, task): mean tokens, mean cost, pass rate
    (then: python make_chart.py  ->  cost_chart.png for the post)
"""

import argparse
import csv
import os
import re
import subprocess
import sys
import tempfile
import time
from statistics import mean

from anthropic import Anthropic

from tasks import TASKS

# ---------------------------------------------------------------------------
# Models and pricing. Prices are $ per million tokens (MTok), verified against
# Anthropic's pricing docs on 2026-06-30.
#
# Sonnet 5 is on INTRODUCTORY pricing ($2/$10) through 2026-08-31; standard is
# $3/$15. We compute cost both ways so the post can show both.
#
# effort: Opus 4.8 and Sonnet 5 have adaptive thinking with effort defaulting to
# "high". We pin it explicitly so all thinking models spend a comparable budget.
# Opus 4.7's effort support differs; we leave it unset there and flag it. Verify
# the exact kwarg against your installed SDK version.
# ---------------------------------------------------------------------------
MODELS = [
    {
        "key": "opus-4.7",
        "id": "claude-opus-4-7",
        "in_price": 5.0,  "out_price": 25.0,
        "in_price_std": 5.0, "out_price_std": 25.0,
        "effort": "high",
    },
    {
        "key": "opus-4.8",
        "id": "claude-opus-4-8",
        "in_price": 5.0,  "out_price": 25.0,
        "in_price_std": 5.0, "out_price_std": 25.0,
        "effort": "high",
    },
    {
        "key": "sonnet-5",
        "id": "claude-sonnet-5",
        "in_price": 2.0,  "out_price": 10.0,    # introductory, through 2026-08-31
        "in_price_std": 3.0, "out_price_std": 15.0,
        "effort": "high",
    },
]

# Adaptive thinking models keep thinking OFF unless set explicitly, even at high
# effort. THINK=True measures the realistic thinking-on cost. Keep all models in
# the same mode for a fair comparison.
THINK = True

SYSTEM = "You are a senior software engineer. Produce correct, idiomatic Python."
MAX_TOKENS = 16000  # fixed output budget; exhausting it is a real failure mode
TIMEOUT_S = 25      # per-test subprocess timeout

client = Anthropic()  # reads ANTHROPIC_API_KEY


def call_model(model, prompt):
    """Single API call. Returns (text, input_tokens, output_tokens, stop_reason)."""
    kwargs = dict(
        model=model["id"],
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    # effort lives INSIDE output_config (top-level field), not as a bare "effort"
    # field; sending it bare returns a 400. Thinking is off unless set explicitly.
    if THINK and model.get("effort"):
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": model["effort"]}

    try:
        resp = client.messages.create(**kwargs)
    except TypeError:
        # Older SDK without native kwargs: pass through extra_body.
        extra = {}
        for k in ("output_config", "thinking"):
            if k in kwargs:
                extra[k] = kwargs.pop(k)
        if extra:
            kwargs["extra_body"] = extra
        resp = client.messages.create(**kwargs)

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return text, resp.usage.input_tokens, resp.usage.output_tokens, getattr(resp, "stop_reason", None)


def extract_code(text):
    """Pull the first fenced code block; fall back to the whole response."""
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```(?:python)?\s*(.*)", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def verify(code, task):
    """Run the model's code plus the task's test harness. Returns (passed, reason)."""
    code = "from __future__ import annotations\n" + code
    for bad in task["forbidden"]:
        if bad.endswith("("):          # banned builtin CALL: eval( exec( compile(
            name = re.escape(bad[:-1])  # lookbehind allows re.compile, obj.eval, etc.
            hit = re.search(r"(?<![\w.])" + name + r"\s*\(", code)
        elif bad.endswith("."):        # banned module attr: ast.
            name = re.escape(bad[:-1])
            hit = re.search(r"(?<![\w.])" + name + r"\.", code)
        else:
            hit = bad in code
        if hit:
            return False, f"forbidden:{bad}"
    program = code + "\n\n" + task["test_code"]
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(program)
        path = f.name
    try:
        out = subprocess.run(
            [sys.executable, path],
            capture_output=True, text=True, timeout=TIMEOUT_S,
        )
        if out.returncode == 0 and "ALL_TESTS_PASSED" in out.stdout:
            return True, "ok"
        reason = (out.stderr.strip().splitlines() or ["assert_failed"])[-1]
        return False, reason[:120]
    except subprocess.TimeoutExpired:
        return False, "timeout"
    finally:
        os.unlink(path)


def cost(in_tok, out_tok, in_price, out_price):
    return in_tok / 1e6 * in_price + out_tok / 1e6 * out_price


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=5)
    args = ap.parse_args()

    raw_rows = []
    for model in MODELS:
        for task in TASKS:
            for trial in range(1, args.trials + 1):
                try:
                    text, in_tok, out_tok, stop_reason = call_model(model, task["prompt"])
                    if stop_reason == "max_tokens" or out_tok >= MAX_TOKENS:
                        passed, reason = False, "token_budget_exhausted:max_tokens"
                    else:
                        passed, reason = verify(extract_code(text), task)
                except Exception as e:  # network / rate limit / etc.
                    in_tok = out_tok = 0
                    passed, reason = False, f"error:{type(e).__name__}:{str(e)[:160]}"
                row = {
                    "model": model["key"],
                    "task": task["id"],
                    "level": task["level"],
                    "weight": task.get("weight", 1),
                    "trial": trial,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "passed": int(passed),
                    "reason": reason,
                    "cost_usd": round(cost(in_tok, out_tok, model["in_price"], model["out_price"]), 6),
                    "cost_usd_standard": round(cost(in_tok, out_tok, model["in_price_std"], model["out_price_std"]), 6),
                }
                raw_rows.append(row)
                print(f"{model['key']:>10} | {task['level']:<12} | "
                      f"trial {trial} | in {in_tok:>5} out {out_tok:>6} | "
                      f"{'PASS' if passed else 'FAIL '+reason} | ${row['cost_usd']:.4f}")
                time.sleep(0.5)  # be polite to rate limits

    with open("results_raw.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(raw_rows[0].keys()))
        w.writeheader()
        w.writerows(raw_rows)

    # Aggregate per (model, task). Token/cost means are over PASSING trials only,
    # so you're comparing the cost of a correct answer, not of a cheap failure.
    summary = []
    seen = {(r["model"], r["task"]) for r in raw_rows}
    for mk, tk in sorted(seen):
        group = [r for r in raw_rows if r["model"] == mk and r["task"] == tk]
        ok = [r for r in group if r["passed"]]
        pool = ok or group  # if nothing passed, report the failing spend so it's visible
        summary.append({
            "model": mk,
            "task": tk,
            "level": group[0]["level"],
            "weight": group[0].get("weight", 1),
            "trials": len(group),
            "pass_rate": round(sum(r["passed"] for r in group) / len(group), 2),
            "mean_input_tokens": round(mean(r["input_tokens"] for r in pool), 1),
            "mean_output_tokens": round(mean(r["output_tokens"] for r in pool), 1),
            "min_cost_usd": round(min(r["cost_usd"] for r in pool), 6),
            "mean_cost_usd": round(mean(r["cost_usd"] for r in pool), 6),
            "max_cost_usd": round(max(r["cost_usd"] for r in pool), 6),
            "mean_cost_usd_standard": round(mean(r["cost_usd_standard"] for r in pool), 6),
        })

    with open("results_summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
        w.writeheader()
        w.writerows(summary)

    print("\nWrote results_raw.csv and results_summary.csv")
    print("Mean cost shown over PASSING trials. Range (min/max) shows variance "
          "from adaptive thinking. Report the range in the post, not just the mean.")


if __name__ == "__main__":
    main()
