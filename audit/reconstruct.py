"""Reconstruct-and-submit: library + plan runner.

A fixed SQL generator (one LLM prompt) writes phase-1 SQL from what an agent could
legitimately hold: the public task question, the schema, the agent-visible knowledge
base (deleted entries removed, as in _filter_knowledge_for_agent) and a chosen set of
simulator answers. Execution errors are fed back up to --max_fixes times, as an agent
would do with execute(); the grader is never consulted before the final submit.
The SQL is graded with the batch runner's own execute_submit_action, which resets the
DB from its template and runs the task's test cases.

Conditions differ only in which simulator answers are included, so a pass that
disappears when an answer is removed shows that answer was needed (starter task,
Result 1 item 5).

Plan runner (most people want audit/pipeline.py instead):
    cd /app && python audit/reconstruct.py --plan audit/reconstruct_plan.json \
        --out bird_interact_agent/outputs/reconstruct/results.jsonl --reps 3

Answer refs in a plan: {"question_id": "hook_limit", "rep": 1, "trial": "trial4"}.
"trial" defaults to searching trial4 then trial5. Outputs hold generated SQL close to
gold, so keep them out of git.
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_DIR = os.path.join(ROOT, "bird_interact_agent")
OUTPUTS = os.path.join(AGENT_DIR, "outputs")
DATA = os.path.join(AGENT_DIR, "data/bird-interact-full/bird_interact_data.jsonl")
DATA_PATH = os.path.join(AGENT_DIR, "data/bird-interact-full")
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, os.path.join(AGENT_DIR, "batch_run_bird_interact"))

from src.config.db_config import set_global_db_config  # noqa: E402
from sample_status import SampleStatus  # noqa: E402
from action_handler import (  # noqa: E402
    load_db_data_if_needed,
    _schema_cache,
    _filter_knowledge_for_agent,
    execute_submit_action,
    reset_and_reconnect_db,
    execute_queries,
)
from src.llm_utils.call_api_batch import call_api_model  # noqa: E402

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_TRIAL_ORDER = ["trial4", "trial5"]

GEN_PROMPT = """You are writing one PostgreSQL query for a user's request.

## User request
{question}

## Database schema
{schema}

## Knowledge base (definitions you may use)
{kb}

## Clarifications from the user
{clarifications}

Write a single PostgreSQL query that answers the request. Follow the clarifications
where they apply. Return only the query inside one ```sql code block."""

FIX_PROMPT = """{prompt}

Your previous query:
```sql
{sql}
```
Running it on the database returned this error:
{error}

Return a corrected query inside one ```sql code block."""


# ---------- data ----------

def use_full_db(host="bird_interact_postgresql_full"):
    set_global_db_config(host=host, port=5432)


def load_records(ids=None, data=DATA):
    recs = {}
    for line in open(data, encoding="utf-8"):
        r = json.loads(line)
        if ids is None or r["instance_id"] in ids:
            recs[r["instance_id"]] = r
    return recs


def turns_path(trial):
    return os.path.join(OUTPUTS, trial, "turns.jsonl")


def load_turns(trials):
    """{(trial, instance_id, question_id, rep): row} for every trial that has a turns file."""
    turns = {}
    for t in trials:
        p = turns_path(t)
        if not os.path.exists(p):
            continue
        for line in open(p, encoding="utf-8"):
            r = json.loads(line)
            turns[(t, r["instance_id"], r["question_id"], r.get("rep", 1))] = r
    return turns


def find_turn(turns, iid, ref):
    rep = ref.get("rep", 1)
    for t in ([ref["trial"]] if ref.get("trial") else DEFAULT_TRIAL_ORDER):
        hit = turns.get((t, iid, ref["question_id"], rep))
        if hit:
            return t, hit
    raise KeyError(f"no turn for {iid} {ref}")


def clean_reply(s):
    s = re.sub(r"</?s>", "", s or "")
    return s.split("[SYSTEM NOTE")[0].strip()


def kb_text(kb):
    lines = [f"- {name}: {info.get('definition', '')}" for name, info in kb.items()]
    return "\n".join(lines) if lines else "(none)"


def task_context(rec, data_path=DATA_PATH):
    db = rec["selected_database"]
    load_db_data_if_needed(db, data_path)
    return _schema_cache.get(db, ""), kb_text(_filter_knowledge_for_agent(db, rec))


# ---------- generate / grade ----------

def extract_sql(text):
    text = text or ""
    m = re.search(r"```[a-zA-Z]*\s*\n(.*?)```", text, re.S)
    if not m:  # unclosed fence: take everything after the opening line
        m = re.search(r"```[a-zA-Z]*\s*\n(.*)$", text, re.S)
    sql = m.group(1) if m else text
    return re.sub(r"```[a-zA-Z]*", "", sql).strip()


def build_prompt(rec, qa_pairs, data_path=DATA_PATH):
    schema, kb = task_context(rec, data_path)
    clar = "\n".join(f"Q: {q}\nA: {a}" for q, a in qa_pairs) or "(none)"
    return GEN_PROMPT.format(question=rec["amb_user_query"], schema=schema, kb=kb, clarifications=clar)


SQL_START = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*(?:(?:with|select|insert|update|delete|create|alter|drop|begin|truncate|do|call|explain)\b|\()",
    re.I | re.S,
)


def incomplete_reason(sql, usage, max_tokens):
    """Why a generation cannot be real SQL, or None. Free to check (no execute coin).

    The provider's finish_reason is not returned by call_api_model, so a hit on the
    token cap is inferred from completion_tokens. Heuristics: hit the cap, does not
    start like SQL (prose/reasoning leaked into the answer), unbalanced parens or
    quotes (cut off mid-query).
    """
    if not sql.strip():
        return "empty"
    used = (usage or {}).get("completion_tokens")
    if used is not None and used >= 0.98 * max_tokens:
        return "hit_token_cap"
    if not SQL_START.match(sql):
        return "not_sql"
    stripped = re.sub(r"'(?:[^']|'')*'", "''", sql)  # drop string literals
    stripped = re.sub(r"--[^\n]*", "", stripped)
    if stripped.count("(") != stripped.count(")"):
        return "unbalanced_parens"
    if stripped.count("'") % 2 or stripped.count('"') % 2:
        return "unbalanced_quotes"
    return None


def sample_sql(prompt, model, max_tokens, regen):
    """One generation; resample (free, no execute coin) while the output is incomplete.

    Returns (sql, info) where info records usage per sample and whether the final
    sample is still incomplete.
    """
    samples = []
    for i in range(regen + 1):
        _, content, usage = call_api_model([{"role": "user", "content": prompt}], model, max_tokens=max_tokens)
        sql = extract_sql(content)
        why = incomplete_reason(sql, usage, max_tokens)
        samples.append({"completion_tokens": (usage or {}).get("completion_tokens"), "incomplete": why})
        if why is None:
            break
    return sql, {"samples": samples, "final_incomplete": samples[-1]["incomplete"]}


def generate_sql(prompt, db, model=DEFAULT_MODEL, max_fixes=2, max_tokens=6000, regen=2):
    """Generate SQL, then fix execution errors up to max_fixes times.

    Returns (sql, retries, executes, info); each execute is 1 coin in the benchmark's costs.
    The execute count and the fix cap are unchanged (policy: at most 2 executes). Truncated or
    non-SQL generations are resampled before they are executed, which costs no coin.
    """
    sql, info = sample_sql(prompt, model, max_tokens, regen)
    info["gen_calls"] = len(info["samples"])
    retries = executes = 0
    for _ in range(max_fixes):
        conn, _cur = reset_and_reconnect_db(db)
        _res, err, timeout = execute_queries(sql, db, conn)
        executes += 1
        if not err and not timeout:
            break
        retries += 1
        fix = FIX_PROMPT.format(prompt=prompt, sql=sql, error=err or "query timed out")
        sql, fix_info = sample_sql(fix, model, max_tokens, regen)
        info["gen_calls"] += len(fix_info["samples"])
        info["samples"] += fix_info["samples"]
        info["final_incomplete"] = fix_info["final_incomplete"]
    return sql, retries, executes, info


def failure_class(passed, obs, final_incomplete):
    """pass | wrong_result | generator_failure (truncated/prose SQL) | other.

    generator_failure rows say nothing about whether the answers were sufficient.
    """
    if passed:
        return "pass"
    if final_incomplete or "syntax error at end of input" in obs or "syntax error at or near" in obs:
        return "generator_failure"
    if "ex_base returned 0" in obs:
        return "wrong_result"
    return "other"


def task_budget(rec, user_patience=10):
    """Starting budget, same formula as calculate_initial_budget (main.py:111-135).

    Default is the official P = 10 (policy v3); the dev runs on fake_account_2 etc. used 6.
    """
    amb = len(rec.get("user_query_ambiguity", {}).get("critical_ambiguity", [])) + len(rec.get("knowledge_ambiguity", []) or [])
    return 3 + 3 + 2 * amb + user_patience


def coins_spent(asks, executes):
    """get_schema 1 + get_all_knowledge_definitions 1 + ask 2 each + execute 1 each + submit 3 (main.py:91-102)."""
    return 1 + 1 + 2 * asks + executes + 3


def grade(sql, rec, data_path=DATA_PATH):
    """Official phase-1 grading via the batch runner. Returns (passed, observation)."""
    status = SampleStatus(idx=0, original_data=rec)
    try:
        obs, _reward, p1, _, _ = execute_submit_action(sql, status, data_path)
        return bool(p1), obs
    except Exception as e:
        return False, f"harness error: {e!r}"


def run_condition(rec, name, refs, turns, reps, model=DEFAULT_MODEL, max_fixes=2, out=None, data_path=DATA_PATH,
                  max_tokens=6000, regen=2, patience=10):
    iid = rec["instance_id"]
    used = [find_turn(turns, iid, ref) for ref in refs]
    qa = [(t["clarification_question"], clean_reply(t["decoder_reply"])) for _, t in used]
    prompt = build_prompt(rec, qa, data_path)
    rows = []
    budget = task_budget(rec, patience)
    for rep in range(1, reps + 1):
        sql, retries, executes, info = generate_sql(prompt, rec["selected_database"], model, max_fixes, max_tokens, regen)
        passed, obs = grade(sql, rec, data_path)
        coins = coins_spent(len(refs), executes)
        row = {
            "instance_id": iid, "condition": name, "rep": rep, "asks": len(refs),
            "exec_retries": retries, "executes": executes,
            "coins": coins, "budget": budget, "patience": patience, "within_budget": coins <= budget,
            "answers_used": [f"{tr}:{t['question_id']}:{t.get('rep', 1)}" for tr, t in used],
            "labels": [t.get("label") for _, t in used], "passed": passed,
            "failure_class": failure_class(passed, obs, info["final_incomplete"]),
            "max_tokens": max_tokens, "gen_calls": info["gen_calls"], "gen_samples": info["samples"],
            "observation": obs, "sql": sql,
        }
        rows.append(row)
        if out:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
        print(f"{iid} {name} rep{rep}: {'PASS' if passed else 'fail'} [{row['failure_class']}] | {obs[:120]}", flush=True)
    return rows


# ---------- plan runner ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--trials", nargs="+", default=DEFAULT_TRIAL_ORDER)
    ap.add_argument("--db_host", default="bird_interact_postgresql_full")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--max_fixes", type=int, default=2)
    ap.add_argument("--max_tokens", type=int, default=6000, help="output cap per generator call (includes model reasoning)")
    ap.add_argument("--regen", type=int, default=2, help="free resamples of a truncated/non-SQL generation")
    ap.add_argument("--patience", type=int, default=10, help="user_patience used for the budget column (official: 10)")
    args = ap.parse_args()

    use_full_db(args.db_host)
    plan = json.load(open(args.plan, encoding="utf-8"))
    turns = load_turns(args.trials)
    recs = load_records(set(plan))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as out:
        for iid, conds in plan.items():
            for name, refs in conds.items():
                run_condition(recs[iid], name, refs, turns, args.reps, args.model, args.max_fixes, out,
                              max_tokens=args.max_tokens, regen=args.regen, patience=args.patience)


if __name__ == "__main__":
    main()
