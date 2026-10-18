# Policy v3 held-out run package (rev 3)

Ten tasks fixed by seeded draw 20261017; see the policy doc, section "Held-out set". P = 10 official; reconstruct.py computes the budget at --patience 10 by default.

## Files
probes/policy_v3_heldout_all.jsonl              merged phase A probes for nine tasks
probes/policy_v3_heldout_phaseB.jsonl           phase B for planets_data_14, exchange_traded_funds_18, crypto_exchange_19; phase A for hulushows_14 and mental_health_14
audit/policy_v3_heldout_plan_stage1.json        flat {task: {condition: [refs]}}: none for all tasks; frozen_p10 for the tasks whose asks are all prewritten
audit/policy_v3_heldout_plan_stage2_ALL.json    amb_only + frozen_minus_* for the prewritten tasks; copy only the passing tasks into the plan you run
POLICY_V3_BATCH_ADDENDUM.md                     the policy doc's "Batch addendum" section, exported verbatim for commit alongside this README

## Decisions recorded before running
- hulushows_14: anchor 'maximum promo saturation ratio' dropped (exact KB match, Rule 2 step 2c). Step 6 on KB entry 82's description and definition gives one anchor, 'promotional messages'; 'most saturated' and 'overloaded' merge into the dropped term. Gets the phase A/B split (addendum decision 8). Cannot be a certificate: none passes 3/3.
- robot_fault_prediction_11 reported invalid (gold answers a different question); replaced by first alternate mental_health_14 (k = 3, budget 22, 7 asks). Rule 2 anchors: 'go-to' experts, 'most complex patient cases'; 'high-risk patients' excluded as KB match. Surface count 2 < k, so it gets the phase A/B split. Its probes follow the same addendum.
- Table slot (slot 5) uses schema-free wording: "which table or records should it be computed from?"
- cold_chain_pharma_compliance_20 (k = 0): asks exclusions, nulls, columns, tie handling; limit skipped because the question states top 3; tie handling is tagged slot order in the probe file and is treated as a non-policy answer in any ablation.

## Phase B
Phase B probes were drafted after the policy's "Batch addendum" was final and are committed after it: scan rep 1 replies to hooked (policy_v3) asks only, never broad (amb_only) replies. Round 1 (probes/policy_v3_heldout_phaseB.jsonl): phase B for planets_data_14, exchange_traded_funds_18, crypto_exchange_19; phase A for hulushows_14 and mental_health_14. Round 2: asks 4 onward for hulushows_14 and mental_health_14, after their round 1 rep-1 replies are read.

Each round writes to its own trial folder, because sim_turns.py overwrites its --out file and reconstruct.py reads outputs/<trial>/turns.jsonl:
- phase A: policy_v3_heldout
- round 1: policy_v3_heldout_b
- round 2: policy_v3_heldout_c

## Order
1. DONE (a90e1c0): commit this folder and the policy doc export.
2. DONE: sim_turns on the merged probes:
   docker exec -it bird_interact_eval bash -c "cd /app && python audit/sim_turns.py --probes probes/policy_v3_heldout_all.jsonl --out bird_interact_agent/outputs/policy_v3_heldout/turns.jsonl --reps 3"
3. DONE: Stage 1 reconstruct (prewritten tasks + none everywhere):
   docker exec -it bird_interact_eval bash -c "cd /app && python audit/reconstruct.py --plan audit/policy_v3_heldout_plan_stage1.json --trials policy_v3_heldout --out bird_interact_agent/outputs/reconstruct/policy_v3_heldout_stage1.jsonl"
4. Round 1 sim_turns:
   docker exec -it bird_interact_eval bash -c "cd /app && python audit/sim_turns.py --probes probes/policy_v3_heldout_phaseB.jsonl --out bird_interact_agent/outputs/policy_v3_heldout_b/turns.jsonl --reps 3"
   then the stage 1b plan (frozen_p10 for planets, etf, crypto) with --trials policy_v3_heldout policy_v3_heldout_b.
5. Round 2 for hulushows_14 and mental_health_14 into policy_v3_heldout_c; their frozen_p10 runs after it.
6. Stage 2 on tasks with frozen_p10 3/3: amb_only + ablations.
Certificate #2: frozen_p10 3/3 within P = 10 budget, amb_only < 3/3, at least one ablation < 3/3.
