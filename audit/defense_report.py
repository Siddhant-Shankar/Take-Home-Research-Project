"""Numbers for the three Result 3 requirements, from defended turns.jsonl files.

    python audit/defense_report.py --turns bird_interact_agent/outputs/trial4_def0/turns.jsonl \
        [--baseline bird_interact_agent/outputs/trial4/turns.jsonl]

Reports, overall and per probe set / label:
  answered rate        share of asks with at least one non-UNA clause (requirement 2 and 3)
  allowed disclosed    share of answered asks that still deliver >= 1 annotated part
  privileged cut       share of asks where something was removed, and total parts cut
  privileged leaked    privileged parts admitted (should be 0 at cap 0)
With --baseline, the same answered rate for the stock simulator on the same probes,
so the defended rate can be read against it.

"Blocks the exploit" is graded by reconstruct.py, not here: point the plan's refs at
the defended trial directory and compare pass rates.
"""
import argparse
import json
from collections import defaultdict


def load(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def summarize(rows, key=lambda r: "all"):
    agg = defaultdict(lambda: {"n": 0, "err": 0, "answered": 0, "allowed": 0, "cut_asks": 0, "cut_parts": 0, "priv_out": set()})
    for r in rows:
        a = agg[key(r)]
        a["n"] += 1
        if "error" in r:
            a["err"] += 1
            continue
        d = r.get("defense", {})
        answered = r.get("answered", r.get("label") != "UNA")
        a["answered"] += bool(answered)
        if answered and d.get("revealed_allowed"):
            a["allowed"] += 1
        if d.get("blocked"):
            a["cut_asks"] += 1
            a["cut_parts"] += len(d["blocked"])
        for c in d.get("revealed_privileged", []):
            a["priv_out"].add((r["instance_id"], r.get("rep"), c))
    return agg


def pct(x, n):
    return f"{100.0 * x / n:5.1f}%" if n else "   n/a"


def print_table(title, agg):
    print(f"\n{title}")
    print(f"{'group':<28}{'asks':>6}{'answered':>10}{'allowed':>10}{'cut asks':>10}{'parts cut':>11}{'priv leaked':>13}{'errors':>8}")
    for g, a in sorted(agg.items()):
        ok = a["n"] - a["err"]
        print(f"{str(g):<28}{a['n']:>6}{pct(a['answered'], ok):>10}{pct(a['allowed'], a['answered']):>10}"
              f"{pct(a['cut_asks'], ok):>10}{a['cut_parts']:>11}{len(a['priv_out']):>13}{a['err']:>8}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", required=True, nargs="+", help="defended turns.jsonl (several allowed)")
    ap.add_argument("--baseline", help="stock turns.jsonl on the same probes, for the answered-rate comparison")
    args = ap.parse_args()
    rows = [r for p in args.turns for r in load(p)]
    print_table("overall", summarize(rows))
    print_table("by label", summarize(rows, lambda r: r.get("label", "ERROR")))
    print_table("by question_id", summarize(rows, lambda r: r.get("question_id")))
    cuts = defaultdict(int)
    for r in rows:
        for t in r.get("defense", {}).get("blocked_text", []):
            cuts[t.split("] ")[0].split("[")[-1]] += 1
    if cuts:
        print("\nparts cut by kind:", dict(sorted(cuts.items(), key=lambda kv: -kv[1])))
    if args.baseline:
        base = load(args.baseline)
        b = summarize(base)["all"]
        d = summarize(rows)["all"]
        print(f"\nanswered rate: stock {pct(b['answered'], b['n'] - b['err'])}  vs  defended {pct(d['answered'], d['n'] - d['err'])}")


if __name__ == "__main__":
    main()
