"""
scripted_session.py -- one continuous end-to-end session per task under the frozen policy v3.

Reuses the batch runner's own pieces so the session is graded and charged exactly as a live
agent run would be:
  environment ....... SampleStatus + calculate_initial_budget + update_budget   (batch_run_bird_interact/main.py)
  actions ........... execute_env_action, execute_submit_action                 (batch_run_bird_interact/action_handler.py)
  user simulator .... build_user_encoder_prompt -> parse_encoder_response -> build_user_decoder_prompt
                      with call_api_model (encoder_decoder mode, prompt v2)     (batch_run_bird_interact/prompt_utils.py)
  SQL generator ..... GEN_PROMPT / FIX_PROMPT / sample_sql                      (audit/reconstruct.py)

Session: get_schema -> get_all_knowledge_definitions -> fixed asks (exact probe wording, in order)
         -> generator -> execute (error fixes only, <= --max_executes) -> submit (phase 1)
         -> phase 2 with whatever coins remain -> transcript.

Every action goes through update_budget before it runs, so the coin column in the transcript
is the harness's own accounting, including WRONG_FORMAT_COST and force_submit behaviour.

Run from /app (inside the eval container):
  python audit/scripted_session.py --task reverse_logistics_1 \
      --asks v3_slot1 v3_slot2 v3_slot3 v3_slot4 v3_slot5 v3_slot6 \
      --probes probes/policy_v3_heldout_all.jsonl \
      --out bird_interact_agent/outputs/e2e/reverse_logistics_1.jsonl

  python audit/scripted_session.py --task fake_account_2 \
      --asks v3_slot1 v3_slot2 v3_slot3 v3_slot4 v3_slot5 \
      --probes probes/policy_v3_fake_account_2.jsonl \
      --out bird_interact_agent/outputs/e2e/fake_account_2.jsonl

  python audit/scripted_session.py --task crypto_exchange_19 \
      --asks v3_slot1 v3_slot2 v3_slot3 v3_step5 v3_slot4 v3_slot5 \
      --probes probes/policy_v3_heldout_all.jsonl probes/policy_v3_heldout_phaseB.jsonl \
      --out bird_interact_agent/outputs/e2e/crypto_exchange_19.jsonl

Transcript lines hold simulator replies and generated SQL (gold-derived): keep them out of git.
The summary line is safe to share minus generated_sql_* and the observation fields.
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_DIR = os.path.join(ROOT, "bird_interact_agent")
DATA = os.path.join(AGENT_DIR, "data/bird-interact-full/bird_interact_data.jsonl")
DATA_PATH = os.path.join(AGENT_DIR, "data/bird-interact-full")
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, os.path.join(ROOT, "audit"))

from src.config.db_config import set_global_db_config, reset_global_db_config  # noqa: E402
from src.llm_utils.call_api_batch import call_api_model  # noqa: E402
from batch_run_bird_interact.sample_status import SampleStatus  # noqa: E402
from batch_run_bird_interact.main import calculate_initial_budget, update_budget  # noqa: E402
from batch_run_bird_interact.action_handler import (  # noqa: E402
    execute_env_action, execute_submit_action, load_db_data_if_needed, _schema_cache,
    _filter_knowledge_for_agent, close_db_connection,
)
from batch_run_bird_interact.prompt_utils import (  # noqa: E402
    build_user_encoder_prompt, build_user_decoder_prompt, parse_encoder_response,
)
import reconstruct  # noqa: E402  (GEN_PROMPT, FIX_PROMPT, sample_sql, kb_text, clean_reply)


# ---------------------------------------------------------------------------
# helpers

def sql_literal(s: str) -> str:
    """Wrap a SQL string as a Python literal so parse_action_arg (ast.literal_eval) recovers it exactly."""
    return repr(s)

def load_record(task_id):
    for line in open(DATA, encoding="utf-8"):
        r = json.loads(line)
        if r["instance_id"] == task_id:
            return r
    sys.exit(f"task {task_id} not in {DATA}")

def load_asks(probe_files, task_id, ask_ids):
    rows = {}
    for f in probe_files:
        for line in open(f, encoding="utf-8"):
            r = json.loads(line)
            if r["instance_id"] == task_id:
                rows[r["question_id"]] = r
    missing = [q for q in ask_ids if q not in rows]
    if missing:
        sys.exit(f"asks not found for {task_id}: {missing}")
    return [rows[q] for q in ask_ids]


class Session:
    def __init__(self, record, patience, user_model, gen_model, max_tokens, regen):
        self.rec = record
        self.db = record["selected_database"]
        total, remaining = calculate_initial_budget(record, patience)
        self.status = SampleStatus(idx=0, original_data=record, remaining_budget=remaining, total_budget=total)
        self.user_model, self.gen_model, self.max_tokens, self.regen = user_model, gen_model, max_tokens, regen
        self.log = []
        load_db_data_if_needed(self.db, DATA_PATH)

    # --- one charged action, exactly as main.py charges it ---
    def _charge(self, action_str):
        self.status.parsed_action = action_str
        depleted = update_budget(self.status)
        return depleted

    def _record(self, tag, action_str, obs, before, extra=None):
        row = {"i": len(self.log), "tag": tag, "phase": self.status.current_phase, "action": action_str,
               "observation": obs, "coins_before": before, "coins_after": self.status.remaining_budget,
               "cost": round(before - self.status.remaining_budget, 2), "force_submit": self.status.force_submit}
        if extra:
            row.update(extra)
        self.log.append(row)
        return obs

    def env(self, action_str, tag):
        before = self.status.remaining_budget
        self._charge(action_str)
        obs, ok = execute_env_action(action_str, self.status, DATA_PATH)
        return self._record(tag, action_str, obs if isinstance(obs, str) else str(obs), before, {"success": ok})

    def ask(self, question, tag):
        """encoder_decoder user simulator, as in main.py. Label is logged for audit; it is not agent-visible."""
        action_str = f"ask({sql_literal(question)})"
        before = self.status.remaining_budget
        self._charge(action_str)
        schema = _schema_cache.get(self.db, "Schema not available")
        enc_prompt = build_user_encoder_prompt(question, self.status, schema)
        _, enc_raw, _ = call_api_model([{"role": "user", "content": enc_prompt}], self.user_model)
        label = parse_encoder_response(enc_raw)
        self.status.last_encoder_action = label
        dec_prompt = build_user_decoder_prompt(question, label, self.status, schema)
        _, reply, _ = call_api_model([{"role": "user", "content": dec_prompt}], self.user_model)
        reply = reply if isinstance(reply, str) else str(reply)
        self.status.last_user_response = reply
        return self._record(tag, action_str, reply, before, {"encoder_label": label, "encoder_raw": enc_raw})

    def submit(self, sql, tag):
        action_str = f"submit({sql_literal(sql)})"
        before = self.status.remaining_budget
        forced = self.status.force_submit
        self._charge(action_str)
        obs, reward, p1, p2, finished = execute_submit_action(sql, self.status, DATA_PATH)
        obs = obs if isinstance(obs, str) else str(obs)
        self.status.phase1_completed, self.status.phase2_completed = p1, p2
        self.status.last_reward = reward or 0.0
        self.status.task_finished = True if forced else finished
        follow_up = None
        if (not self.status.task_finished and p1 and not p2 and self.status.current_phase == 1):
            self.status.current_phase = 2
            follow_up = (self.rec.get("follow_up") or {}).get("query")
            if not follow_up:
                self.status.task_finished = True
        self._record(tag, action_str, obs, before, {"reward": reward, "phase1_completed": p1,
                                                    "phase2_completed": p2, "task_finished": self.status.task_finished})
        return obs, follow_up

    # --- generator (reconstruct's prompt and sampler; executes charged through env()) ---
    def gen_prompt(self, question, qa_pairs, extra_note=None):
        schema = _schema_cache.get(self.db, "")
        kb = reconstruct.kb_text(_filter_knowledge_for_agent(self.db, self.rec))
        clar = "\n".join(f"Q: {q}\nA: {a}" for q, a in qa_pairs) or "(none)"
        if extra_note:
            clar += "\n" + extra_note
        return reconstruct.GEN_PROMPT.format(question=question, schema=schema, kb=kb, clarifications=clar)

    def write_sql(self, prompt, max_executes, tag):
        sql, info = reconstruct.sample_sql(prompt, self.gen_model, self.max_tokens, self.regen)
        executes = 0
        while executes < max_executes and not self.status.force_submit:
            obs = self.env(f"execute({sql_literal(sql)})", f"{tag}_execute")
            executes += 1
            if not obs.lower().startswith(("sql execution error", "error", "sql execution timed out")):
                break
            fix = reconstruct.FIX_PROMPT.format(prompt=prompt, sql=sql, error=obs)
            sql, fix_info = reconstruct.sample_sql(fix, self.gen_model, self.max_tokens, self.regen)
            info["samples"] += fix_info["samples"]
        return sql, executes, info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--asks", nargs="+", required=True, help="question_ids, in order")
    ap.add_argument("--probes", nargs="+", required=True)
    ap.add_argument("--patience", type=int, default=10)
    ap.add_argument("--max_executes", type=int, default=2, help="per phase, error fixes only")
    ap.add_argument("--user_model", default="gemini-3.8-flash")
    ap.add_argument("--gen_model", default=reconstruct.DEFAULT_MODEL)
    ap.add_argument("--max_tokens", type=int, default=6000)
    ap.add_argument("--regen", type=int, default=2)
    ap.add_argument("--db_host", default="bird_interact_postgresql_full")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    set_global_db_config(host=a.db_host, port=5432)
    rec = load_record(a.task)
    asks = load_asks(a.probes, a.task, a.asks)
    s = Session(rec, a.patience, a.user_model, a.gen_model, a.max_tokens, a.regen)
    budget0 = s.status.total_budget
    t0 = time.time()
    try:
        # 1. setup
        s.env("get_schema()", "get_schema")
        s.env("get_all_knowledge_definitions()", "get_all_knowledge_definitions")

        # 2. frozen asks
        qa = []
        for r in asks:
            reply = s.ask(r["clarification_question"], f"ask:{r['question_id']}")
            qa.append((r["clarification_question"], reconstruct.clean_reply(reply)))

        # 3. phase 1: generate -> executes -> submit
        p1_prompt = s.gen_prompt(rec["amb_user_query"], qa)
        sql1, ex1, _ = s.write_sql(p1_prompt, a.max_executes, "phase1")
        coins_before_submit = s.status.remaining_budget
        p1_obs, follow_up = s.submit(sql1, "phase1_submit")
        coins_after_p1 = s.status.remaining_budget

        # 4. phase 2 with what is left: no asks; generator on the follow-up; executes only if coins allow
        p2_obs, sql2, ex2 = None, None, 0
        if follow_up and not s.status.task_finished:
            note = f"Phase 1 query already accepted as correct:\n```sql\n{sql1}\n```"
            p2_prompt = s.gen_prompt(follow_up, qa, extra_note=note)
            # leave 3 coins for submit; each execute costs 1
            affordable = max(0, int(s.status.remaining_budget - 3))
            sql2, ex2, _ = s.write_sql(p2_prompt, min(a.max_executes, affordable), "phase2")
            p2_obs, _ = s.submit(sql2, "phase2_submit")

        summary = {
            "task": a.task, "patience": a.patience, "budget": budget0,
            "asks": a.asks, "n_asks": len(asks),
            "executes_phase1": ex1, "coins_used_through_phase1_submit": round(budget0 - coins_after_p1, 2),
            "coins_before_phase1_submit": coins_before_submit,
            "phase1_passed": bool(s.status.phase1_completed),
            "phase1_within_budget": coins_after_p1 >= 0,
            "executes_phase2": ex2, "phase2_passed": bool(s.status.phase2_completed),
            "coins_left_end": s.status.remaining_budget, "total_reward": s.status.last_reward,
            "encoder_labels": [x.get("encoder_label") for x in s.log if x["tag"].startswith("ask:")],
            "secs": round(time.time() - t0, 1),
            "phase1_observation": p1_obs, "phase2_observation": p2_obs,
            "generated_sql_phase1": sql1, "generated_sql_phase2": sql2,
        }
    finally:
        close_db_connection(s.db)
        reset_global_db_config()

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        for x in s.log:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
        f.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")

    shareable = {k: v for k, v in summary.items()
                 if k not in ("generated_sql_phase1", "generated_sql_phase2", "phase1_observation", "phase2_observation")}
    print(json.dumps(shareable, indent=2))
    print("\nstep  tag                              cost  coins_after")
    for x in s.log:
        print(f"{x['i']:>4}  {x['tag']:<32} {x['cost']:>5}  {x['coins_after']:>6}")
    print("\nphase1:", str(p1_obs)[:100])
    if p2_obs:
        print("phase2:", str(p2_obs)[:100])


if __name__ == "__main__":
    main()
