"""Run single ask turns through the batch runner's own encoder + decoder.

Uses build_user_encoder_prompt / parse_encoder_response / build_user_decoder_prompt
from bird_interact_agent/batch_run_bird_interact, so each turn is exactly what the
a-interact runner does for one `ask` (main.py:667-770), minus the agent.

Run inside the bird_interact_eval container:
    cd /app && python audit/sim_turns.py --probes probes/trial4_amb_path.jsonl \
        --out bird_interact_agent/outputs/trial4/turns.jsonl --reps 3

Output rows hold gold-derived text (encoder thoughts, decoder replies), so the
output path should stay out of git.
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_DIR = os.path.join(ROOT, "bird_interact_agent")
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, os.path.join(AGENT_DIR, "batch_run_bird_interact"))

from prompt_utils import (  # noqa: E402
    build_user_encoder_prompt,
    build_user_decoder_prompt,
    parse_encoder_response,
)
from sample_status import SampleStatus  # noqa: E402
from action_handler import load_db_data_if_needed, _schema_cache  # noqa: E402
from src.llm_utils.call_api_batch import call_api_model  # noqa: E402


def call(prompt, model):
    # same call shape as worker_function in call_api_batch.py (max_tokens=6000, temperature 0)
    _, content, usage = call_api_model([{"role": "user", "content": prompt}], model, max_tokens=6000)
    return content, usage


def run_turn(probe, record, data_path, model, rep):
    status = SampleStatus(idx=0, original_data=record)
    db_name = record["selected_database"]
    load_db_data_if_needed(db_name, data_path)
    db_schema = _schema_cache.get(db_name, "Schema not available")
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probes", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--data", default=os.path.join(AGENT_DIR, "data/bird-interact-full/bird_interact_data.jsonl"))
    ap.add_argument("--data_path", default=os.path.join(AGENT_DIR, "data/bird-interact-full"))
    ap.add_argument("--model", default="gemini-3.8-flash")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--threads", type=int, default=8)
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
    jobs = [(p, rep) for rep in range(1, args.reps + 1) for p in probes]
    rows = []
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs = {ex.submit(run_turn, p, records[p["instance_id"]], args.data_path, args.model, rep): (p, rep) for p, rep in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            p, rep = futs[f]
            try:
                rows.append(f.result())
            except Exception as e:  # keep going, record the failure
                rows.append({**p, "rep": rep, "model": args.model, "error": repr(e)})
            print(f"[{i}/{len(jobs)}] {p['instance_id']} {p['question_id']} rep{rep}: {rows[-1].get('label', 'ERROR')}", flush=True)

    rows.sort(key=lambda r: (r["instance_id"], r["question_id"], r["rep"]))
    with open(args.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
