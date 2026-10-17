# Policy v3 held-out run package (rev 2)

Ten tasks fixed by seeded draw 20261017. P = 10 official; reconstruct.py checks coins at P = 6, so recompute budgets when tabulating.

## Files
probes/policy_v3_<task>.jsonl          per task: phase A hooked asks (family policy_v3) + amb_only broad asks (policy_v3_amb_only)
probes/policy_v3_heldout_all.jsonl     merged phase A probes for nine tasks (hulushows_14 held out pending step-6 anchors)
audit/policy_v3_heldout_plan_stage1.json        flat {task: {condition: [refs]}}: none for all ten; frozen_p10 for the six tasks whose asks are all prewritten
audit/policy_v3_heldout_plan_stage1b_TEMPLATE.json   frozen_p10 for the four nested tasks, filled after phase B probes exist
audit/policy_v3_heldout_plan_stage2_ALL.json    amb_only + frozen_minus_* for the six complete tasks; copy only the passing tasks into the plan you run

## Decisions recorded before running
- hulushows_14: anchor 'promo saturation ratio' dropped (exact KB name match, Rule 2 step 2c). Anchors to be chosen by Rule 2 step 6 from the KB entry's definition text. Its hooked probes are not in the merged file.
- Table slot (slot 5) uses schema-free wording: "which table or records should it be computed from?"
- robot_fault_prediction_11: 7th ask left unspent (all surface terms anchored, no nested expected).
- cold_chain diag_term: k = 0 check only; never in a graded condition.

## Order
1. Commit this folder and the policy doc export (anchors + probes, no gold text).
2. sim_turns on the merged probes:
   docker exec -it bird_interact_eval bash -c "cd /app && python audit/sim_turns.py --probes probes/policy_v3_heldout_all.jsonl --out bird_interact_agent/outputs/policy_v3_heldout/turns.jsonl --reps 3"
3. Stage 1 reconstruct (six complete tasks + none everywhere):
   docker exec -it bird_interact_eval bash -c "cd /app && python audit/reconstruct.py --plan audit/policy_v3_heldout_plan_stage1.json --trials policy_v3_heldout --out bird_interact_agent/outputs/reconstruct/policy_v3_heldout_stage1.jsonl"
4. Read phase A replies for planets_data_14, exchange_traded_funds_18, crypto_exchange_19 (and hulushows once its anchors exist): write phase B probes, sim_turns them, fill the stage1b plan, run it.
5. Stage 2 on tasks with frozen_p10 3/3: amb_only + ablations from the stage2 file.
Certificate #2: frozen_p10 3/3 within P=10 budget, amb_only < 3/3, at least one ablation < 3/3.
