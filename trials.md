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

_running_

---

## open questions / todo
- does the exploit have to fit inside the official budget? (assuming yes)
- pick the 10 held-out tasks (disjoint from dev tasks)
- read appendix N of the bird-interact paper and check it against the code
- note: not putting gold sql in this file since it gets pushed (dataset asks not to leak gt)
