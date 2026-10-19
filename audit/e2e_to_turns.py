"""
e2e_to_turns.py -- make a reconstruct-readable turns file from a scripted_session transcript.

  python audit/e2e_to_turns.py \
      --transcript bird_interact_agent/outputs/e2e/reverse_logistics_1.jsonl \
      --trial e2e_rl1_r1

writes bird_interact_agent/outputs/<trial>/turns.jsonl with one row per ask, rep 1,
fields: instance_id, question_id, rep, clarification_question, decoder_reply, label.
reconstruct.py then reads it with --trials <trial>.
"""
import argparse, ast, json, os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUTS = os.path.join(ROOT, "bird_interact_agent", "outputs")

def parse_action_arg(action, prefix):
    """scripted_session builds ask(<repr(question)>), so the argument is a Python string literal."""
    m = re.search(rf"{re.escape(prefix)}\((.*)\)", action, re.DOTALL)
    raw = m.group(1).strip() if m else action[len(prefix) + 1:-1]
    try:
        v = ast.literal_eval(raw)
        if isinstance(v, str):
            return v
    except Exception:
        pass
    return raw.strip("'\"")

ap = argparse.ArgumentParser()
ap.add_argument("--transcript", required=True)
ap.add_argument("--trial", required=True)
a = ap.parse_args()

rows, task = [], None
for line in open(a.transcript, encoding="utf-8"):
    r = json.loads(line)
    if "summary" in r:
        task = r["summary"]["task"]
        continue
    if r.get("tag", "").startswith("ask:"):
        rows.append({
            "instance_id": None,
            "question_id": r["tag"].split(":", 1)[1],
            "rep": 1,
            "clarification_question": parse_action_arg(r["action"], "ask"),
            "decoder_reply": r["observation"],
            "label": r.get("encoder_label"),
        })
for row in rows:
    row["instance_id"] = task

out_dir = os.path.join(OUTPUTS, a.trial)
os.makedirs(out_dir, exist_ok=True)
out = os.path.join(out_dir, "turns.jsonl")
with open(out, "w", encoding="utf-8") as f:
    for row in rows:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
print(f"{len(rows)} asks for {task} -> {out}")