"""Which input difference makes LOC fire on the batch path but not on UserSim-Guard?

Builds the batch runner's v2 encoder prompt (build_user_encoder_prompt), then swaps in
UserSim-Guard's version of one input block at a time, taken from a saved UserSim-Guard
step-1 prompt for the same task (the blocks do not depend on the question):
  - batch          : batch prompt as the benchmark builds it
  - ug_segments    : batch prompt with UserSim-Guard's SQL-segment block
  - ug_amb         : batch prompt with UserSim-Guard's ambiguity block
  - ug_both        : both swapped (close to the UserSim-Guard prompt)
Each variant runs at the batch cap (6000) and the UserSim-Guard cap (2048). Encoder only.

Run inside the bird_interact_eval container:
    cd /app && python audit/loc_path_isolation.py \
        --out bird_interact_agent/outputs/loc_isolation/labels.jsonl --reps 3
"""
import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_DIR = os.path.join(ROOT, "bird_interact_agent")
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, os.path.join(AGENT_DIR, "batch_run_bird_interact"))

from prompt_utils import build_user_encoder_prompt, parse_encoder_response  # noqa: E402
from sample_status import SampleStatus  # noqa: E402
from src.llm_utils.call_api_batch import call_api_model  # noqa: E402

AMB = ("Labeled Ambiguity Points (Not visible to the AI)|>", "<|The End of All Labeled")
SEG = ("Ground-truth SQL Segments (Not visible to the AI)|>", "<|The End of Ground-truth SQL Segments")
QUESTIONS = {
    "loc_limit_generic": "How many users should be returned?",
    "ctrl_filter": "Should any accounts be excluded?",
    "ctrl_nulls": "What should happen for accounts whose age is zero or missing?",
}


def block(p, marks):
    m = re.search(re.escape(marks[0]) + r"(.*?)" + re.escape(marks[1]), p, re.S)
    return m.group(1)


def swap(p, marks, new):
    return p.replace(block(p, marks), new, 1)


def kind(l):
    l = (l or "").strip().lower()
    return "AMB" if l.startswith("labeled") else "LOC" if l.startswith("unlabeled") else "UNA" if l.startswith("unanswerable") else "UNPARSED"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--model", default="gemini-3.8-flash")
    ap.add_argument("--ug_prompts", default=os.path.join(ROOT, "usersim-guard/results/g_v2_step1_prompts_fake_account_2.jsonl"))
    ap.add_argument("--data", default=os.path.join(AGENT_DIR, "data/bird-interact-full/bird_interact_data.jsonl"))
    args = ap.parse_args()

    rec = next(json.loads(l) for l in open(args.data, encoding="utf-8") if json.loads(l)["instance_id"] == "fake_account_2")
    ug = json.loads(open(args.ug_prompts, encoding="utf-8").readline())["prompt"]
    ug_amb, ug_seg = block(ug, AMB), block(ug, SEG)

    jobs = []
    for qid, q in QUESTIONS.items():
        base = build_user_encoder_prompt(q, SampleStatus(idx=0, original_data=rec), "")
        variants = {
            "batch": base,
            "ug_segments": swap(base, SEG, ug_seg),
            "ug_amb": swap(base, AMB, ug_amb),
            "ug_both": swap(swap(base, SEG, ug_seg), AMB, ug_amb),
        }
        for vname, prompt in variants.items():
            for cap in (6000, 2048):
                for rep in range(1, args.reps + 1):
                    jobs.append((qid, vname, cap, rep, prompt))

    def run(job):
        qid, vname, cap, rep, prompt = job
        try:
            _, content, usage = call_api_model([{"role": "user", "content": prompt}], args.model, max_tokens=cap)
            label = parse_encoder_response(content or "")
            return {"question_id": qid, "variant": vname, "cap": cap, "rep": rep, "label": label,
                    "kind": kind(label), "completion_tokens": (usage or {}).get("completion_tokens"),
                    "has_think": "<think>" in (content or "")}
        except Exception as e:
            return {"question_id": qid, "variant": vname, "cap": cap, "rep": rep, "error": repr(e), "kind": "ERROR"}

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(run, jobs))
    with open(args.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    from collections import Counter, defaultdict
    table = defaultdict(Counter)
    for r in rows:
        table[(r["question_id"], r["variant"], r["cap"])][r["kind"]] += 1
    for k in sorted(table):
        print(k, dict(table[k]))


if __name__ == "__main__":
    main()
