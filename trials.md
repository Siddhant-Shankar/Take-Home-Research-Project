# trials log

running notes on each run of the bird-interact harness + what i took away from it.
setup: full split (600 tasks), a-interact batch runner, encoder/decoder user sim w/ prompt v2, patience budget 6.
models: gemini-3.8-flash for both agent and user sim (2.0-flash from the paper is retired now).

---

## trial 0 - getting it to run at all (oct 10)

- first two attempts died on api errors, not the benchmark
  - google's openai-compatible endpoint rejects `frequency_penalty` / `presence_penalty` -> stopped sending them
  - also rejects `stop=None` (only hit this once the agent made its first `ask`, since the user sim calls don't pass stop sequences) -> same fix
- then hit the free tier cap (20 requests/day/model). one task is ~20-40 calls so free tier is useless for this. added prepaid credit.

**takeaway:** the harness retries forever on 4xx errors, so a bad request looks like a hang. watch the console log for `Error code: 4xx`.

---

## trial 1 - solar_panel_1, stock agent (oct 10)

task: "What's the downtime score for 'Solar Plant West Davidport'?"
hidden: "downtime score" = system unavailability (formula from MTBF + MTTR), that knowledge entry is deleted from the agent's kb. also a "rounded" ambiguity.
budget: 18

| turn | action | what came back |
|---|---|---|
| 1-5 | get_schema, kb names, 2 kb definitions, 1 column meaning | availability = 1 - unavailability, but unavailability itself isn't in the kb |
| 6-9 | 4x execute, poking at the plant's rows | plant has one snapshot, mtbf is NULL |
| 10 | **ask** "how is the downtime score defined or calculated?" | encoder: `labeled("downtime score")` (AMB) |
| 11-14 | 3x execute testing the formula | returns NULL (because mtbf is null) |
| 15 | submit | **phase 1 passed (+0.7)** |
| 16-17 | 2x execute for the follow-up | second one errored, budget down to 0.5 |
| 18 | forced submit | **phase 2 failed**, ended at -2.5 budget |

what it showed:
- the stock agent burns ~6 coins exploring before it asks anything. its one question went straight at the hidden concept, which is what the benchmark wants.
- **the simulator's answer to that one honest question already included the column names and the actual expression** (mttrh over mtbfh + mttrh), plus the rounding even though rounding wasn't asked. so even the "legit" AMB path gives back sql-level detail, not just plain english. the decoder prompt says not to give gt sql segments, so this is the guideline not holding.
- **scoring bug:** results_metrics.json says avg_reward 0.0 even though phase 1 passed. a passed submit adds to the running reward but a failed submit sets it to 0 and overwrites it (action_handler.py ~511 vs ~533). so pass p1 + fail p2 = 0. real score here is 0.7. going forward i'm scoring off the phase1/phase2 completed flags.
- grader feedback on the p2 fail was just "ex_base returned 0 but expected 1", doesn't give much away.

output: `bird_interact_agent/outputs/batch_runs/20261011_020419_baseline/` (not committed, local only)

---

## trial 2 - 6 more tasks, stock agent (oct 10)

picked one task from 6 different dbs (seed 7), 4 query + 2 management:
fake_account_2, sports_events_19, polar_equipment_4, organ_transplant_9, labor_certification_applications_M_10, museum_artifact_M_1

goal: see if trial 1's sql-in-the-reply thing is common or a one-off, and get an honest-agent reference number.

| task | cat | budget | asks | encoder label(s) | p1 | p2 |
|---|---|---|---|---|---|---|
| fake_account_2 | query | 14 | 1 | AMB | pass | fail |
| sports_events_19 | query | 16 | 3 | AMB, AMB, AMB | fail | - |
| polar_equipment_4 | query | 20 | 1 | AMB | fail | - |
| organ_transplant_9 | query | 18 | 1 | AMB | fail | - |
| labor_certification_applications_M_10 | mgmt | 16 | 1 | **UNA** | fail | - |
| museum_artifact_M_1 | mgmt | 14 | 1 | AMB | pass | fail |

honest-agent number so far (trials 1+2, 7 tasks): **p1 3/7, p2 0/7**. every task ended with the budget at or below 0.

what it showed:
- **zero LOC classifications.** 7 AMB, 1 UNA across 8 asks. the stock agent's questions always land on the labeled ambiguities, so LOC basically never gets exercised by an honest agent.
- **the stock agent already bundles questions**, even though its prompt says one at a time. most asks were 2-3 questions in one ("how is X calculated, how many rows, which columns?"). and the simulator answers all the parts:
  - fake_account_2: one ask got back the metric formula, the rounding, top 10, which columns to return, and the sort order. that's most of the query spec for 2 coins.
  - polar_equipment_4: one ask got the reliance formula, rounding, the class labels and their thresholds.
  - so the "one question at a time" rule isn't enforced anywhere and bundling is normal agent behaviour, not something exotic.
- **the one UNA looked like over-refusal.** labor_cert asked how the holding indicator should be calculated, which is literally the ambiguous part of the task, and got "out of scope". agent then failed. so the encoder has false refusals too, not just leaks.
- **grader feedback on management tasks leaks a lot more than on query tasks.** query tasks just say "ex_base returned 0 but expected 1". the custom test cases on labor_cert said:
  - "The retention_rate column was not added" -> tells you the expected column name
  - "Mismatch for TECHIE BRAINS INCORPORATED" -> tells you a specific row that's wrong
  - that's a second channel separate from the simulator. 3 coins per submit though.
- agent also wasted turns on badly formatted actions (things like a bare code fence or a raw `ORDER BY ...` line got parsed as the action), 0.2 coins each.
- p1 passes here weren't because of leaks as far as i can tell, mostly the agent just got the formula right after asking.

output: `bird_interact_agent/outputs/batch_runs/20261011_022447_baseline/` (local only)

---

## observed agent behaviour (trials 1-2, 7 tasks)

went back through all 7 stock-agent transcripts to see what it actually does. pretty consistent:

1. **same opening every time.** all 7 start with `get_schema()` then `get_all_external_knowledge_names()`, then pull individual kb definitions.
   ```
   solar_panel_1      schema > kb_names > kb_def x2 > col_meaning > execute x4 > ASK (t10)
   fake_account_2     schema > kb_names > kb_def x3 > col_meaning x2 > execute x2 > ASK (t10)
   sports_events_19   schema > kb_names > kb_def x7 > execute > ASK (t11) > execute > ASK > ASK
   polar_equipment_4  schema > kb_names > kb_def x9 > execute x2 > ASK (t14)
   organ_transplant_9 schema > kb_names > kb_def > col_meaning x2 > execute x6 > col_meaning > ASK (t13)
   labor_cert_M_10    schema > kb_names > kb_def > col_meaning > kb_def x2 > ASK (t7)
   museum_artifact_M1 schema > kb_names > kb_def x5 > execute > kb_def x2 > execute x2 > kb_def > ASK (t14)
   ```
2. **asks late.** first ask is at turn 7-14, after ~4-9 coins of exploring. by then it knows the real table/column names and which kb entries exist.
3. **finds the gap by reading around it.** looks up the definitions next to the hidden concept (e.g. "System Availability" when "System Unavailability" is the deleted one) and then asks about the piece the kb doesn't define.
4. **execute is for checking, not answering.** row counts, looking at one row, checking for NULLs, then testing the formula after the user replies.
5. **one question template.** "could you clarify how/whether [vague phrase from the task] ... [its own guess or options] ... and [rows / columns / sorting]?"
6. **short verify-then-submit loop after the reply.** 1-3 executes then submit. p1 passed 3/7. p2 always got cut off by budget.
7. **always runs out of budget.** every task ended at or below 0, so the up-front exploring directly limits how many asks/submits are left.
8. **format-error turns.** 4 times the reply got parsed as a junk action (bare code fence, stray `ORDER BY ...`, a "Wait, if ..." sentence), 0.2 coins each.

full transcripts: transcripts/trials_1_2.md

---

## open questions / todo
- does the exploit have to fit inside the official budget? (assuming yes)
- pick the 10 held-out tasks (disjoint from dev tasks)
- read appendix N of the bird-interact paper and check it against the code
- run more honest-agent tasks to tighten the reference number (7 is small)
- grader feedback on management tasks: how much does it give away per failed submit?
- note: not putting gold sql in this file since it gets pushed (dataset asks not to leak gt)
