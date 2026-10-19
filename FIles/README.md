# Policy v3 held-out run package

Probes and plan for the ten held-out tasks fixed in the policy doc (seeded draw 20261017). P = 10.

## Files
- probes/policy_v3_<task>.jsonl    one file per task: phase A hooked asks (family policy_v3) + amb_only broad asks (policy_v3_amb_only)
- probes/policy_v3_heldout_all.jsonl   all ten merged, for one sim_turns call (59 probes, 177 turns at 3 reps)
- audit/policy_v3_heldout_plan.json    conditions per task: none, frozen_p10, amb_only, frozen_minus_<each answer>

## Before running
1. Exclusion steps (b, c): drop any anchor that matches a schema column/table name or a KB entry name. If an anchor is dropped, promote the next Rule-2 candidate or treat the term as nested.
2. Replace "[adjust to table names after get_schema]" in the table slot with the actual table names.
3. cold_chain diag_term is a k = 0 check only; never put it in a graded condition.

## Phases
- Phase A (now): slots 1-3 on surface anchors for tasks with nested terms expected (hulushows_14, planets_data_14, exchange_traded_funds_18, crypto_exchange_19); all slots for the rest.
- Phase B (after reading phase A replies): remaining slots for the four nested tasks, anchored on the nested term if one surfaced, else cycling surface anchors; v3_extra asks per the beyond-six-slots rule. Replace phase_b_pending entries in the plan with real question_ids.

## Stages
- Stage 1: sim_turns on heldout_all; reconstruct none + frozen_p10 for all ten.
- Stage 2: amb_only + frozen_minus_* only where frozen_p10 passed 3/3.
- Certificate #2: frozen_p10 3/3 within budget, amb_only < 3/3, at least one ablation < 3/3.

## Commands (assumed entry points)
docker exec -it bird_interact_eval bash -c "cd /app && python audit/sim_turns.py --probes probes/policy_v3_heldout_all.jsonl --out bird_interact_agent/outputs/policy_v3_heldout/turns.jsonl --reps 3"
docker exec -it bird_interact_eval bash -c "cd /app && python audit/reconstruct.py --plan audit/policy_v3_heldout_plan.json --stage 1 --out bird_interact_agent/outputs/reconstruct/policy_v3_heldout_results.jsonl"
