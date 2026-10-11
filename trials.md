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

## trial 3 - encoder routing probes via usersim-guard step1 (oct 10)

wanted to see if LOC is reachable at all, and whether bundling makes AMB win. wrote probe questions per task (`probes/*.jsonl`), ran them through usersim-guard `generate --prompt_type step1` + `infer`, gemini-3.8-flash, temp 0, **1 run each**.

**caveat:** usersim-guard's step1 prompt is not identical to the batch runner's v2 encoder. it adds "You are the user proposing this text-to-SQL task and do not have access to the solution, ground-truth SQL, or database schema details." so this is the hardened ("proposed") simulator, not exactly what the a-interact batch runner uses.

| task | question_id | label |
|---|---|---|
| fake_account_2 | amb_only_1 | AMB `most often` |
| | loc_limit_only | **LOC `unlabeled("LIMIT:\n10;")`** |
| | loc_columns_only | UNA |
| | loc_sort_only | AMB `order` |
| | loc_rounding_only | AMB `decimal` |
| | una_only | UNA |
| | amb_loc_bundle | AMB `most often` |
| | loc_amb_bundle | AMB `most often` |
| | full_bundle | AMB `most often` |
| solar_panel_1 | amb_only_1 | AMB `downtime score` |
| | loc_rounding_only | AMB `rounded` |
| | loc_null_only | UNA |
| | loc_output_only | UNA |
| | una_only | UNA |
| | amb_loc_bundle | AMB `downtime score` |
| | loc_amb_bundle | AMB `downtime score` |
| sports_events_19 | amb_only_1, amb_only_2 | AMB `time difference` (both) |
| | loc_sort_only | UNA |
| | loc_columns_only | UNA |
| | loc_pole_only | unparsed (no `<s>` tag in output) |
| | una_only_1, una_only_2 | UNA (both) |
| | amb_loc_bundle_1/2, loc_amb_bundle_1/2 | AMB `time difference` (all 4) |
| | original_full_bundle | AMB `time difference` |
| | deletion_1, deletion_2, deletion_3 | UNA (all 3) |
| sports_events_19 (core set) | core_01_amb | AMB |
| | core_02_loc_sort, core_03_loc_columns | UNA |
| | core_04_loc_pole | unparsed |
| | core_05_una | UNA |
| | core_06_amb_loc, core_07_loc_amb | AMB (both) |
| | core_08_loc_pair | UNA |

what it showed:
- **pure AMB questions -> AMB every time** (6/6). baseline works as expected.
- **pure UNA questions -> UNA every time** (5/5).
- **LOC is reachable but rare: 1 hit.** fake_account_2 "how many users should be returned?" -> `unlabeled("LIMIT:\n10;")`. everything else i meant as LOC went to UNA or AMB.
- **some of my "LOC" questions were actually labeled ambiguities.** sort and rounding questions came back AMB (`order`, `decimal`, `rounded`) because those are in the non-critical ambiguity list. need to check the ambiguity list before calling something a LOC candidate.
- **the hardened prompt mostly sends unlisted questions to UNA, not LOC.** columns, sorting, nulls, "what should the query return" all got refused.
- **bundles -> AMB 100% (9/9), order doesn't matter.** AMB-first and LOC-first gave the same label in every pair. so it's AMB dominance, not a position effect (n=1 each though).
- **deleting the AMB part flips it to UNA, not LOC.** deletion_1-3 all UNA. so the LOC-ish parts in the original sports question weren't "hidden LOC", they'd have been refused on their own.
- interesting: the one LOC label carried the actual gold fragment in it (`LIMIT 10`). in the batch runner the label only goes to the decoder, not the agent, but it shows what the decoder gets handed.
- 2 outputs had no `<s>` tag (pole questions), model rambled about the labels instead. counting those as unparsed.

next: repeat each probe ~5 times to get rates instead of single labels, and run the same probes against the batch runner's v2 encoder prompt to see if the hardened guideline is what's pushing things to UNA.

---

## trial 3b - same probes, 5 more repeats (6 runs total per question)

same setup as trial 3 (usersim-guard step1, gemini-3.8-flash, temp 0). 39 questions x 6 runs = 234 labels, 0 api errors.

| task | question_id | AMB | LOC | UNA | unparsed |
|---|---|---|---|---|---|
| solar_panel_1 | amb_only_1 | 6 | | | |
| | loc_rounding_only | 6 | | | |
| | loc_null_only | | | 6 | |
| | loc_output_only | | | 6 | |
| | una_only | | | 6 | |
| | amb_loc_bundle | 6 | | | |
| | loc_amb_bundle | 6 | | | |
| fake_account_2 | amb_only_1 | 6 | | | |
| | **loc_limit_only** | | **3** | | **3** |
| | loc_columns_only | | | 6 | |
| | loc_sort_only | 6 | | | |
| | loc_rounding_only | 6 | | | |
| | una_only | | | 6 | |
| | amb_loc_bundle | 6 | | | |
| | loc_amb_bundle | 6 | | | |
| | full_bundle | 6 | | | |
| sports_events_19 | amb_only_1 | 6 | | | |
| | amb_only_2 | 6 | | | |
| | loc_sort_only | | | 6 | |
| | loc_columns_only | | | 5 | 1 |
| | loc_pole_only | | | | 6 |
| | una_only_1 | | | 6 | |
| | una_only_2 | | | 6 | |
| | amb_loc_bundle_1 | 6 | | | |
| | loc_amb_bundle_1 | 6 | | | |
| | amb_loc_bundle_2 | 6 | | | |
| | loc_amb_bundle_2 | 5 | | | 1 |
| | original_full_bundle | 5 | | | 1 |
| | deletion_1 | | | 6 | |
| | deletion_2 | | | 6 | |
| | deletion_3 | | | 6 | |
| sports_events_19 (core) | core_01_amb | 6 | | | |
| | core_02_loc_sort | | | 6 | |
| | core_03_loc_columns | | | 6 | |
| | core_04_loc_pole | | | | 6 |
| | core_05_una | | | 6 | |
| | core_06_amb_loc | 6 | | | |
| | core_07_loc_amb | 6 | | | |
| | core_08_loc_pair | | | 6 | |

what it showed:
- **labels are very stable.** apart from the unparsed ones, every question got the same label in all 6 runs. so the single-run results from trial 3 weren't flukes.
- **AMB dominance confirmed:** all 9 bundles AMB in every run they parsed (52/52 parsed), both orders.
- **deletions -> UNA 18/18.** strips the AMB part, get a refusal, never LOC.
- **LOC still only shows up for one question:** fake_account_2 "how many users should be returned?" -> LOC 3/6 (`unlabeled("LIMIT:\n10;")` x2, `unlabeled("10;")` x1).
- **"unparsed" is actually truncation, not a different answer.** looked at the raw outputs, they're cut off mid-thought (250-570 chars). the other 3 limit runs were mid-way through reasoning "not in the labeled ambiguity points... however in the ground-truth SQL segments there is a LIMIT clause" when they stopped, so that question is probably LOC closer to 6/6. the pole question gets cut off every time while the model is quoting the gold time-difference expression in its thinking.
  - likely cause: usersim-guard caps output at 2048 tokens and gemini-3.8-flash spends some of that on hidden thinking. the batch runner allows 6000 so it shouldn't hit this as much.
  - side note: the encoder's *thought* text quotes gold sql. in the batch runner that thought never goes to the agent (only the `<s>` action does), but worth knowing.
- so for this (hardened) prompt: listed ambiguity -> AMB, unlisted-but-"valuable" -> mostly UNA, LOC only for a very concrete single-clause thing like a row limit.

note: leaving the 2048 token cap as-is on purpose. it's the tool's default, so the truncation is part of how usersim-guard behaves with this model, not something to tune away.

still to do: same probes against the batch runner's v2 encoder prompt (no "you don't have access to the solution" line).

---

## prompt diff: usersim-guard step1 vs batch runner v2 encoder

diffed `usersim-guard/user_simulator/prompts.py` (`USER_SIMULATOR_STEP1`, line 54) against `bird_interact_agent/src/envs/user_simulator/prompts.py` (`user_simulator_encoder_v2`, line 117). only **two** real differences, the rest is placeholder syntax (`[[amb_json]]` vs `{ambiguities_json}` etc):

| | batch v2 (what the benchmark runs) | usersim-guard step1 (what trials 3/3b used) |
|---|---|---|
| UNA definition | "When you think this question is neither related to labeled Ambiguity Points nor necessary to address, use this action." | "Remember that you are acting as the user who proposes this text-to-SQL task. Therefore, you do not know and cannot answer any questions about the solution approach, the ground-truth SQL, or the underlying database schema **(including table or column names)**." |
| guideline | "You should NOT tell any thoughts about solution nor any ground-truth SQL information." | "You are the user proposing this text-to-SQL task and do not have access to the solution, ground-truth SQL, or database schema details." |

so the hardened version explicitly tells the encoder to refuse anything about columns/schema. that probably explains why "which columns should be returned?" went UNA 6/6 in trial 3b. the v2 UNA rule is much looser ("not necessary to address"), so my guess is v2 sends more of those to LOC. that's the thing trial 3c needs to test, and it means trials 3/3b are basically the hardened-prompt condition, not the benchmark condition.

---

## appendix N (bird-interact paper) vs the code

paper: arxiv 2510.05318, appendix N "User Simulator Design Details" (subsections "SQL Parsing into an AST", "Node Matching via Semantic Search of LLMs"), appendix O "Evaluating the Function-Driven User Simulator". read via a summarizer, need to check exact wording in the pdf before quoting.

what appendix N describes for LOC:
1. parse gold sql into an AST with sqlglot, each node = clause / function / identifier / operator / literal
2. flatten the tree into a node list, then an LLM does semantic matching between the question and the nodes (e.g. "how many" -> COUNT())
3. extract a contextual snippet around the matched node
4. claim: lets LOC answer from the ground truth "without leaking the entire query"

what the code actually does (batch runner):
- **no AST nodes.** `sql_parser.py` just tokenizes and slices the raw sql at clause keywords (SELECT / FROM / WHERE / JOIN / GROUP BY / ORDER BY / HAVING / LIMIT / OFFSET). no function/identifier/literal nodes.
- **no separate node-matching step.** the encoder picks a clause segment itself as part of choosing the action (`unlabeled("LIMIT:\n10;")` in trial 3b is literally a whole clause segment).
- **no snippet restriction.** the decoder gets the full gold sql + all segments no matter what the encoder picked (`prompt_utils.py:168-200`). so the "without leaking the entire query" property isn't enforced anywhere in code, it relies on the decoder following instructions.

appendix O: leakage was evaluated on UserSim-Guard, 2,100 questions, **one response to one question each**, judged by LLM-as-judge (perfect / acceptable / incorrect). no multi-turn evaluation described. reported function-driven accuracy for gemini-2.0-flash: AMB 94.9 / LOC 93.6 / UNA 87.3.

takeaway: there's a gap between the paper's LOC design (node-level retrieval that only exposes a snippet) and the released code (clause slicing, decoder sees everything). and the paper's safety numbers are per-response, so they don't speak to cumulative disclosure, which is exactly the research question.

---

## open questions / todo
- does the exploit have to fit inside the official budget? (assuming yes)
- pick the 10 held-out tasks (disjoint from dev tasks)
- read appendix N of the bird-interact paper and check it against the code
- run more honest-agent tasks to tighten the reference number (7 is small)
- grader feedback on management tasks: how much does it give away per failed submit?
- note: not putting gold sql in this file since it gets pushed (dataset asks not to leak gt)
