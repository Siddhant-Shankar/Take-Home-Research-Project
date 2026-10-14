"""Measure which gold-SQL components each simulator reply mentions.

Gold components come from parsing the task's phase-1 sol_sql with sqlglot:
tables, columns, functions, literals, plus LIMIT value and ORDER BY direction.
A component counts as disclosed in a reply when its name/value appears in the
reply text (case-insensitive, word-bounded). Verbatim = the reply contains a
>=15-char run of a labeled ambiguity's sql_snippet (whitespace-normalised).

Prints counts only by default; --show also prints component names (gold-derived,
keep that output out of git).

Run inside the bird_interact_eval container (needs sqlglot 26.16.4):
    cd /app && python audit/disclosure.py --turns bird_interact_agent/outputs/trial4/turns.jsonl
"""
import argparse
import json
import os
import re
from collections import defaultdict

import sqlglot
from sqlglot import exp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "bird_interact_agent/data/bird-interact-full/bird_interact_data.jsonl")
KINDS = ["table", "column", "function", "literal", "limit", "order_dir"]


def gold_components(sql):
    sqls = sql if isinstance(sql, list) else [sql]
    comps = set()
    for s in sqls:
        for tree in sqlglot.parse(s, read="postgres"):
            if tree is None:
                continue
            ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
            for t in tree.find_all(exp.Table):
                if t.name and t.name.lower() not in ctes:
                    comps.add(("table", t.name.lower()))
            for c in tree.find_all(exp.Column):
                if c.name:
                    comps.add(("column", c.name.lower()))
            for f in tree.find_all(exp.Func):
                name = f.sql_name() if not isinstance(f, exp.Anonymous) else f.name
                if name:
                    comps.add(("function", name.lower()))
            for lit in tree.find_all(exp.Literal):
                v = lit.this.strip()
                if v and not (lit.parent and isinstance(lit.parent, exp.Limit)):
                    comps.add(("literal", v.lower()))
            for lim in tree.find_all(exp.Limit):
                comps.add(("limit", lim.expression.sql().lower()))
            for o in tree.find_all(exp.Ordered):
                comps.add(("order_dir", "desc" if o.args.get("desc") else "asc"))
    return comps


def mentioned(comp, text):
    kind, val = comp
    t = text.lower()
    if kind == "order_dir":
        words = {"desc": r"\b(desc|descending|highest|largest|top)\b", "asc": r"\b(asc|ascending|lowest|smallest)\b"}
        return re.search(words[val], t) is not None
    pat = r"(?<![a-z0-9_])" + re.escape(val) + r"(?![a-z0-9_])"
    return re.search(pat, t) is not None


def snippets(record):
    out = []
    uqa = record.get("user_query_ambiguity", {})
    for group in ("critical_ambiguity", "non_critical_ambiguity"):
        out += [a.get("sql_snippet", "") for a in uqa.get(group, [])]
    out += [a.get("sql_snippet", "") for a in record.get("knowledge_ambiguity", [])]
    return [re.sub(r"\s+", " ", s).strip().lower() for s in out if s]


def verbatim(reply, snips, n=15):
    r = re.sub(r"\s+", " ", reply).lower()
    for s in snips:
        for i in range(0, max(1, len(s) - n + 1)):
            if len(s) >= n and s[i:i + n] in r:
                return True
    return False


def label_kind(label):
    l = (label or "").strip().lower()
    return "AMB" if l.startswith("labeled") else "LOC" if l.startswith("unlabeled") else "UNA" if l.startswith("unanswerable") else "OTHER"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", required=True)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.turns, encoding="utf-8") if l.strip()]
    ids = {r["instance_id"] for r in rows}
    recs = {}
    for line in open(DATA, encoding="utf-8"):
        r = json.loads(line)
        if r["instance_id"] in ids:
            recs[r["instance_id"]] = r
    gold = {i: gold_components(recs[i]["sol_sql"]) for i in ids}
    snips = {i: snippets(recs[i]) for i in ids}

    print("== gold component counts per task ==")
    for i in sorted(ids):
        c = defaultdict(int)
        for k, _ in gold[i]:
            c[k] += 1
        print(i, {k: c[k] for k in KINDS})
        if args.show:
            print("   ", sorted(gold[i]))

    print("\n== per question (union over reps) ==")
    print("task,question_id,family,labels,reply_chars_max,verbatim_reps," + ",".join(KINDS))
    union_by = defaultdict(set)
    by_q = defaultdict(list)
    for r in rows:
        by_q[(r["instance_id"], r["question_id"])].append(r)
    for (i, q), rs in sorted(by_q.items()):
        hit = set()
        labels = defaultdict(int)
        vb = 0
        maxlen = 0
        for r in rs:
            if "error" in r:
                labels["ERROR"] += 1
                continue
            reply = r.get("decoder_reply") or ""
            labels[label_kind(r.get("label"))] += 1
            maxlen = max(maxlen, len(reply))
            vb += verbatim(reply, snips[i])
            hit |= {c for c in gold[i] if mentioned(c, reply)}
        fam = rs[0].get("family", "")
        union_by[(i, fam)] |= hit
        union_by[(i, "ALL")] |= hit
        counts = {k: sum(1 for kk, _ in hit if kk == k) for k in KINDS}
        lab = " ".join(f"{k}{v}" for k, v in sorted(labels.items()))
        print(f"{i},{q},{fam},{lab},{maxlen},{vb}/{len(rs)}," + ",".join(str(counts[k]) for k in KINDS))
        if args.show:
            print("   ", sorted(hit))

    print("\n== cumulative union per task and family (mentioned / gold) ==")
    for (i, fam), hit in sorted(union_by.items()):
        tot = defaultdict(int)
        got = defaultdict(int)
        for k, _ in gold[i]:
            tot[k] += 1
        for k, _ in hit:
            got[k] += 1
        print(i, fam, {k: f"{got[k]}/{tot[k]}" for k in KINDS if tot[k]})
        if args.show:
            print("    missing:", sorted(gold[i] - hit))


if __name__ == "__main__":
    main()
