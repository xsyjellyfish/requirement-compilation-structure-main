"""Summarize compiler.log evidence: stage durations, model calls, rounds and check runs.

Usage:
    python scripts/analyze_run_log.py output/demo/compiler.log output/demo2/compiler.log

The script only reads recorded evidence. It never guesses a number that the log does
not contain, and it never makes a model or network call.
"""

"""Summarize compiler.log evidence: stage durations, model calls, tokens and check runs.

Usage:
    python scripts/analyze_run_log.py output/demo/compiler.log output/demo2/compiler.log

The script only reads recorded evidence. It never guesses a number that the log does not
contain, and it never makes a model or network call.
"""

import re
import sys
from pathlib import Path

STAGE = re.compile(r"\[START\] Stage (\d\d) \| ([^\u2502\n]+)")
DURATION = re.compile(r"Completed in ([\d.]+)\s*s")
TOKENS = re.compile(r"Reported tokens \| input: ([\d,]+)(?: \| cached input: ([\d,]+))? "
    r"\| output: ([\d,]+) \| total: ([\d,]+)")
LATENCY = re.compile(r"Round-trip latency: ([\d.]+) s")
ROUND = re.compile(r"\| ([^|\n]*?) \| Edit round (\d+)/(\d+)")
CHECKS = {"typecheck (tsc)": "typescript/bin/tsc",
    "playwright e2e/acceptance": "Playwright E2E | real backend",
    "npm install": "Installing dependencies in",
    "npm run build": "run build"}


def summarize(path: Path) -> dict:
    """Attribute durations, model usage and check runs to the stage that produced them."""
    text = path.read_text(encoding="utf-8", errors="replace")
    stages, order, current = {}, [], None
    totals = {"calls": 0, "input": 0, "cached": 0, "output": 0, "latency": 0.0}
    for line in text.splitlines():
        started = STAGE.search(line)
        if started:
            number = started.group(1)
            if number in stages:
                current = stages[number]
            else:
                current = stages[number] = {"name": started.group(2).strip(), "seconds": 0.0,
                    "calls": 0, "input": 0, "cached": 0, "output": 0, "latency": 0.0,
                    **{name: 0 for name in CHECKS}}
                order.append(number)
            continue
        if current is None:
            continue
        if match := DURATION.search(line):
            current["seconds"] += float(match.group(1))
        if match := TOKENS.search(line):
            current["calls"] += 1
            current["input"] += int(match.group(1).replace(",", ""))
            current["cached"] += int((match.group(2) or "0").replace(",", ""))
            current["output"] += int(match.group(3).replace(",", ""))
        if match := LATENCY.search(line):
            current["latency"] += float(match.group(1))
        for name, needle in CHECKS.items():
            if needle in line:
                current[name] += 1
    rounds = {}
    for label, number, limit in ROUND.findall(text):
        key = label.strip()
        rounds[key] = max(rounds.get(key, (0, 0))[0], int(number)), int(limit)
    print(f"== {path} ==")
    header = ("stage", "task", "time(s)", "calls", "input", "cached", "output", "tsc", "e2e", "install", "build")
    print("{:5s} {:44s} {:>8s} {:>5s} {:>9s} {:>8s} {:>8s} {:>4s} {:>4s} {:>7s} {:>5s}".format(*header))
    for number in order:
        stage = stages[number]
        print("{:5s} {:44.44s} {:8.1f} {:5d} {:9,d} {:8,d} {:8,d} {:4d} {:4d} {:7d} {:5d}".format(
            number, stage["name"], stage["seconds"], stage["calls"], stage["input"],
            stage["cached"], stage["output"], stage["typecheck (tsc)"],
            stage["playwright e2e/acceptance"], stage["npm install"], stage["npm run build"]))
        for key, target in (("calls", "calls"), ("input", "input"), ("cached", "cached"),
                ("output", "output"), ("latency", "latency")):
            totals[target] += stage[key]
    print("{:5s} {:44s} {:8.1f} {:5d} {:9,d} {:8,d} {:8,d}".format("all", "total",
        sum(stages[number]["seconds"] for number in order), totals["calls"], totals["input"],
        totals["cached"], totals["output"]))
    print(f"model round-trip latency: {totals['latency']:.1f} s across {totals['calls']} calls"
        f" | mean input per call: {totals['input'] / max(totals['calls'], 1):,.0f} tokens")
    print("edit rounds per task (round/limit):")
    for label, (number, limit) in sorted(rounds.items()):
        print(f"  {number:>2}/{limit:<2}  {label}")
    print()
    return totals


def main(argv: list[str]) -> int:
    for argument in argv or ["output/demo/compiler.log", "output/demo2/compiler.log"]:
        path = Path(argument)
        if not path.is_file():
            print(f"missing log: {path}", file=sys.stderr)
            continue
        summarize(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

