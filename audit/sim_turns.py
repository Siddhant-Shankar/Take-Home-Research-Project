"""Run single ask turns through the batch runner's own encoder + decoder.

Uses build_user_encoder_prompt / parse_encoder_response / build_user_decoder_prompt
from bird_interact_agent/batch_run_bird_interact, so each turn is exactly what the
a-interact runner does for one `ask` (main.py:667-770), minus the agent.

Run inside the bird_interact_eval container:
    cd /app && python audit/sim_turns.py --probes probes/trial4_amb_path.jsonl \
        --out bird_interact_agent/outputs/trial4/turns.jsonl --reps 3

Defended run (Result 3): same probes, same output format, one stateful simulator per
(instance_id, rep) episode; the probes of an episode are asked in file order (or by a
`turn` field when the probe rows carry one). `decoder_reply` holds the defended reply,
so reconstruct.py grades it unchanged; `decoder_draft` keeps the stock draft and
`defense` holds the per-ask trace (clause labels, parts revealed, parts cut, ledger).
    cd /app && python audit/sim_turns.py --probes probes/trial4_amb_path.jsonl \
        --out bird_interact_agent/outputs/trial4_def0/turns.jsonl --reps 3 --defense --cap 0

Output rows hold gold-derived text (encoder thoughts, decoder replies), so the
output path should stay out of git.
"""
import argparse
import json
import os
import sys
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_DIR = os.path.join(ROOT, "bird_interact_agent")
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, os.path.join(AGENT_DIR, "batch_run_bird_interact"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prompt_utils import (  # noqa: E402
    build_user_encoder_prompt,
    build_user_decoder_prompt,
    parse_encoder_response,
)
from sample_status import SampleStatus  # noqa: E402
from action_handler import load_db_data_if_needed, _schema_cache  # noqa: E402
from src.llm_utils.call_api_batch import call_api_model  # noqa: E402
from defense import DefendedSimulator  # noqa: E402


def call(prompt, model):
    # same call shape as worker_function in call_api_batch.py (max_tokens=6000, temperature 0)
    _, content, usage = call_api_model([{"role": "user", "content": prompt}], model, max_tokens=6000)
    return content, usage


def _context(record, data_path):
    status = SampleStatus(idx=0, original_data=record)
    db_name = record["selected_database"]
    load_db_data_if_needed(db_name, data_path)
    return status, _schema_cache.get(db_name, "Schema not available")


def run_turn(probe, record, data_path, model, rep):
    status, db_schema = _context(record, data_path)
    question = probe["clarification_question"]

    enc_raw, enc_usage = call(build_user_encoder_prompt(question, status, db_schema), model)
    label = parse_encoder_response(enc_raw)
    dec_raw, dec_usage = call(build_user_decoder_prompt(question, label, status, db_schema), model)

    return {
        **probe,
        "rep": rep,
        "model": model,
        "encoder_raw": enc_raw,
        "label": label,
        "decoder_reply": dec_raw,
        "usage": {"encoder": enc_usage, "decoder": dec_usage},
    }


def run_episode(probes, record, data_path, model, rep, cap, splitter):
    """All probes of one (instance, rep), in order, through one DefendedSimulator."""
    status, db_schema = _context(record, data_path)
    sim = DefendedSimulator.from_bird_interact(record, status, db_schema, model, call, cap=cap, splitter=splitter)
    rows = []
    for probe in probes:
        try:
            r = sim.ask(probe["clarification_question"])
            rows.append({
                **probe,
                "rep": rep,
                "model": model,
                "defended": True,
                "cap": cap,
                "turn": r["turn"],
                "encoder_raw": "\n---\n".join(c.get("encoder_raw") or "" for c in r["clauses"]),
                "label": r["label"],
                "answered": r["answered"],
                "decoder_reply": r["reply"],
                "decoder_draft": r["draft"],
                "defense": {k: r[k] for k in ("clauses", "audit_mode", "revealed_allowed", "revealed_privileged",
                                              "blocked", "blocked_text", "ledger")},
                "usage": r["usage"],
            })
        except Exception as e:  # keep the episode going, record the failure
            rows.append({**probe, "rep": rep, "model": model, "defended": True, "cap": cap, "error": repr(e)})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probes", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--data", default=os.path.join(AGENT_DIR, "data/bird-interact-full/bird_interact_data.jsonl"))
    ap.add_argument("--data_path", default=os.path.join(AGENT_DIR, "data/bird-interact-full"))
    ap.add_argument("--model", default="gemini-3.8-flash")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--defense", action="store_true", help="route asks through audit/defense.py (one episode per instance_id x rep)")
    ap.add_argument("--cap", type=int, default=0, help="privileged parts allowed per episode (0 = annotated facts only)")
    ap.add_argument("--splitter", choices=["llm", "heuristic"], default="llm", help="how compound questions are split")
    args = ap.parse_args()

    probes = [json.loads(l) for l in open(args.probes, encoding="utf-8") if l.strip()]
    wanted = {p["instance_id"] for p in probes}
    records = {}
    for line in open(args.data, encoding="utf-8"):
        r = json.loads(line)
        if r["instance_id"] in wanted:
            records[r["instance_id"]] = r
    missing = wanted - records.keys()
    if missing:
        sys.exit(f"records not found: {sorted(missing)}")
    if not all(records[i].get("sol_sql") for i in wanted):
        sys.exit("records lack sol_sql; merge GT first (combine_public_with_gt.py)")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    rows = []
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        if args.defense:
            # in order within an episode, in parallel across episodes
            groups = OrderedDict()
            for p in probes:
                groups.setdefault(p["instance_id"], []).append(p)
            for iid, ps in groups.items():
                if all("turn" in p for p in ps):
                    ps.sort(key=lambda p: p["turn"])
            jobs = [(iid, ps, rep) for rep in range(1, args.reps + 1) for iid, ps in groups.items()]
            futs = {ex.submit(run_episode, ps, records[iid], args.data_path, args.model, rep, args.cap, args.splitter): (iid, rep)
                    for iid, ps, rep in jobs}
            for i, f in enumerate(as_completed(futs), 1):
                iid, rep = futs[f]
                try:
                    ep = f.result()
                except Exception as e:
                    ep = [{**p, "rep": rep, "model": args.model, "defended": True, "cap": args.cap, "error": repr(e)}
                          for p in groups[iid]]
                rows.extend(ep)
                summary = ", ".join(f"{r.get('question_id')}:{r.get('label', 'ERROR')}"
                                    + (f"-cut{len(r['defense']['blocked'])}" if r.get("defense", {}).get("blocked") else "")
                                    for r in ep)
                print(f"[{i}/{len(jobs)}] {iid} rep{rep} cap{args.cap}: {summary}", flush=True)
        else:
            jobs = [(p, rep) for rep in range(1, args.reps + 1) for p in probes]
            futs = {ex.submit(run_turn, p, records[p["instance_id"]], args.data_path, args.model, rep): (p, rep) for p, rep in jobs}
            for i, f in enumerate(as_completed(futs), 1):
                p, rep = futs[f]
                try:
                    rows.append(f.result())
                except Exception as e:  # keep going, record the failure
                    rows.append({**p, "rep": rep, "model": args.model, "error": repr(e)})
                print(f"[{i}/{len(jobs)}] {p['instance_id']} {p['question_id']} rep{rep}: {rows[-1].get('label', 'ERROR')}", flush=True)

    rows.sort(key=lambda r: (r["instance_id"], r.get("turn", 0), r["question_id"], r["rep"]))
    with open(args.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
