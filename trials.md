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

limitation (trials 3/3b): the probes were just questions, no tool calls first. that doesn't change what the simulator sees (it's stateless, the encoder only gets ambiguity list + gold sql segments + the question, the decoder adds a static schema file, never the agent's history). but it does change the **wording**: the stock agent asks schema-grounded questions with real names it found (AGE_D, Q1/Q2/Q3, comorbid conditions), while my probes were generic ("which columns should be returned?"). so 3/3b only cover generic questions, and they ignore budget (in a real episode exploring eats ~4-9 coins before the first ask). for 3c: write a generic + a schema-grounded version of each LOC candidate (names from `data/bird-interact-full/<db>/<db>_schema.txt`), so it's prompt x specificity. budget realism gets checked later in full batch-runner runs.

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

## trial 3c - same probes on the batch runner's v2 encoder wording (oct 11)

swapped usersim-guard step1 to the v2 wording (only the UNA rule + one guideline line change, commit 974301b; hardened kept as `USER_SIMULATOR_STEP1_HARDENED`). checked a generated prompt has the v2 UNA text and not the hardened one before running. same 39 probes, gemini-3.8-flash, temp 0, 6 runs -> 234 labels, 0 api errors. compared against trial 3b (hardened, 6 runs).

| | AMB | LOC | UNA | unparsed |
|---|---|---|---|---|
| hardened (3b) | 118 | **3** | 95 | 18 |
| v2 (3c) | 121 | **0** | 93 | 20 |

questions whose label distribution changed (everything else identical across both prompts):

| task | question_id | hardened | v2 |
|---|---|---|---|
| fake_account_2 | **loc_limit_only** | LOC 3, unparsed 3 | **UNA 2, unparsed 4** |
| sports_events_19 | loc_pole_only | unparsed 6 | AMB 1, UNA 1, unparsed 4 |
| sports_events_19 | deletion_1 | UNA 6 | UNA 1, unparsed 5 |
| sports_events_19 | loc_columns_only | UNA 5, unparsed 1 | UNA 6 |
| sports_events_19 | loc_amb_bundle_2 | AMB 5, unparsed 1 | AMB 6 |
| sports_events_19 | original_full_bundle | AMB 5, unparsed 1 | AMB 6 |
| solar_panel_1 | loc_output_only | UNA 6 | UNA 5, unparsed 1 |

what it showed:
- **my hypothesis was wrong.** i expected the looser v2 UNA rule to let more questions through to LOC. it didn't: LOC went from 3 to **0**. columns / sorting / nulls / output questions are UNA under both prompts.
- **the one LOC question flipped to UNA.** reading the raw v2 output for "how many users should be returned?": the model decided the limit "was likely clearly specified in the user query (hence `LIMIT 10` in the ground-truth SQL without being flagged as ambiguous). Therefore, this question does not address any real ambiguity in the query and is unnecessary." -> `unanswerable()`. so under v2 the gate isn't "do you have access to this" but **"is this necessary to address"**, and the model treats anything not in the ambiguity list as unnecessary.
- **AMB dominance holds on v2 too.** all bundles AMB (and fewer truncations on them than 3b). deletions still UNA / truncated, never LOC.
- **most of the changes are just unparsed/truncation shuffling**, not real label changes. the only substantive flips are loc_limit_only (LOC -> UNA) and one AMB + one UNA on the pole question.
- **truncation is still a problem:** 20 unparsed. limit and pole questions get cut off mid-thought (often while quoting gold sql), so their true rates are uncertain. leaving the cap as-is per the earlier note, but it means those two questions are inconclusive.
- **taken together (3b + 3c):** on both encoder wordings, a question that isn't one of the labeled ambiguities almost never reaches LOC. it gets refused, either because the user "can't know" (hardened) or because it's "not necessary" (v2). so as far as this probe set goes, the encoder is acting as a filter that only lets labeled ambiguities through. that's evidence against LOC being an easy leak path, and it's a candidate negative-result finding.
- caveats: still 3 tasks, generic wording (no schema-grounded versions yet), temp 0 repeats aren't real variance, gemini-3.8-flash only.

---

## trial 3d - schema-grounded probes, both prompts (oct 11)

wrote grounded versions of the LOC candidates using table/column names from the schema files, plus a few narrow single-choice versions (`probes/*_grounded.jsonl`, 16 questions). ran on both v2 and hardened, 6 runs each -> 192 labels, 0 errors. checked the generated prompts: `g_v2_*` have the v2 UNA text, `g_hard_*` have the hardened one.

compared against the generic version of the same question from 3b (hardened) / 3c (v2):

| task | question | generic hard | generic v2 | grounded hard | grounded v2 |
|---|---|---|---|---|---|
| solar_panel_1 | rounding_grounded | AMB 6 | AMB 6 | AMB 6 | AMB 6 |
| | null_grounded | UNA 6 | UNA 6 | UNA 6 | UNA 6 |
| | **output_grounded** | UNA 6 | UNA 5, unp 1 | UNA 6 | **AMB 4**, unp 2 |
| | null_mtbf_narrow | UNA 6 | UNA 6 | UNA 6 | UNA 6 |
| | null_mttr_narrow | UNA 6 | UNA 6 | UNA 6 | UNA 6 |
| fake_account_2 | **limit_grounded** | **LOC 3**, unp 3 | UNA 2, unp 4 | UNA 1, unp 5 | **LOC 1**, UNA 3, unp 2 |
| | columns_grounded | UNA 6 | UNA 6 | UNA 6 | UNA 6 |
| | sort_grounded | AMB 6 | AMB 6 | AMB 6 | AMB 6 |
| | rounding_grounded | AMB 6 | AMB 6 | AMB 6 | AMB 6 |
| | limit_narrow | LOC 3, unp 3 | UNA 2, unp 4 | unp 6 | UNA 2, unp 4 |
| | sort_narrow | AMB 6 | AMB 6 | AMB 6 | AMB 6 |
| | columns_narrow | UNA 6 | UNA 6 | UNA 6 | UNA 6 |
| sports_events_19 | sort_grounded | UNA 6 | UNA 6 | UNA 6 | UNA 6 |
| | columns_grounded | UNA 5, unp 1 | UNA 6 | UNA 5, unp 1 | UNA 6 |
| | **pole_grounded** | unp 6 | AMB 1, UNA 1, unp 4 | **UNA 6** | **AMB 5**, unp 1 |
| | columns_narrow | UNA 5, unp 1 | UNA 6 | UNA 6 | UNA 6 |

totals (grounded only): hardened AMB 24 / LOC 0 / UNA 60 / unp 12. v2 AMB 33 / LOC 1 / UNA 53 / unp 9.

what it showed:
- **grounding didn't unlock LOC.** 1 LOC in 192 labels (`unlabeled("LIMIT 10;")`, limit_grounded on v2). columns / nulls / sort-on-sports stay UNA whether generic, grounded or narrow.
- **on the hardened prompt grounding made the one LOC question worse.** generic limit question was LOC 3/6; with `accounts.acct_ref` in it, it's UNA/truncated, 0 LOC. fits the hardened UNA rule ("cannot answer ... table or column names") — naming a column pushes it toward refusal.
- **on v2, grounding pulls questions into AMB instead of LOC.** two clear flips:
  - solar output question: UNA -> `labeled("downtime score")` 4/6 once it mentions the matching row in `plants`
  - sports pole question: -> `labeled("time difference")` 5/6 once it names `q1_r/q2_r/q3_r` and `rbind`
  - so naming the columns behind a labeled concept gets the question matched to that ambiguity. same AMB dominance as the bundles, just triggered by schema names instead of the vague phrase.
- **same question, opposite direction depending on the prompt.** pole_grounded: hardened UNA 6/6 vs v2 AMB 5/6. so the two prompts really do behave differently once questions get specific; for generic questions they mostly agreed (3c).
- **narrow single-choice versions didn't help** either. all UNA, or AMB where they hit a listed ambiguity (sort direction).
- sort / rounding questions are AMB everywhere because they're listed ambiguities (as flagged in 3b), so they're not LOC tests.

overall (3b + 3c + 3d): across generic, grounded and narrow wording on both prompts, unlisted questions reach LOC 4 times out of ~400 labels, all on the row-limit question. the encoder either refuses (UNA) or snaps the question onto a labeled ambiguity (AMB). on v2, schema-grounding makes the AMB snapping *more* likely.

caveats: same as before (3 tasks, temp-0 repeats, gemini-3.8-flash only, truncation on the limit question).

### raw excerpts (trial 3d) — files in `usersim-guard/results/`

**same limit question, three different outcomes.** Q: "How many `accounts.acct_ref` rows should be returned in the final result?"
- v2 run 1 -> LOC (`g_v2_rep1_step1_responses_fake_account_2.jsonl`): "There is no labeled ambiguity for the limit/number of rows. Looking at the Ground-truth SQL Segments: `LIMIT:` `10;` ... Since this is an important ambiguity ... and is not among the labeled ambiguity points, it is an unlabeled ambiguity." -> `unlabeled("LIMIT 10;")`
- v2 run 2 -> UNA (`g_v2_rep2_...fake_account_2.jsonl`): "the requirement (e.g., top 10 accounts) is already explicitly specified in the user request, making this question unnecessary to clarify" -> `unanswerable()`
  - **this reasoning is false.** the actual task question is "Which users log in most often for how old they are?", nothing about top 10. the encoder never sees the user query, so it inferred "top 10 was specified" from the `LIMIT 10` in the gold sql. the "not necessary" refusal is based on a made-up premise.
- hardened run 3 -> UNA (`g_hard_rep3_...fake_account_2.jsonl`): "This question explicitly refers to underlying database schema details (`accounts.acct_ref` ...). According to the instructions: '...cannot answer any questions about ... the underlying database schema (including table or column names)'" -> `unanswerable()`. this is why grounding killed LOC on hardened.

**schema names pull v2 into AMB.** Q: "Which column or calculated value should the final query return for the matching row in `plants`?"
- v2 -> AMB (`g_v2_rep2_...solar_panel_1.jsonl`): "corresponds to the ambiguity surrounding the calculation of 'downtime score' (or 'System Unavailability', defined as `om."mttrh" / (om."mtbfh" + om."mttrh")`) ... Therefore, the action should be `labeled("downtime score")`."
- hardened -> UNA (`g_hard_rep1_...solar_panel_1.jsonl`): "directly asks about database schema elements (specifically referring to the table `plants` ...) ... I do not know and cannot answer questions regarding the database schema".

**pole question.** Q: "When using `qualifying.q1_r`, `qualifying.q2_r`, and `qualifying.q3_r`, how should the reference pole time for each `qualifying.rbind` be selected?"
- v2 -> `labeled("time difference")` 5/6
- hardened -> UNA 6/6: "asks about specific database tables and columns ... I do not have knowledge of the underlying database schema or implementation details."

takeaways from the excerpts:
- the two prompts gate differently. hardened: "does it mention schema?" -> refuse. v2: "is it a labeled ambiguity / necessary?" -> AMB or refuse.
- on v2, schema names act as a hook into a labeled ambiguity (`plants` -> downtime score, `q1_r/q2_r/q3_r` -> time difference). that sends the question down the AMB path, which in trial 1 returned sql-level detail.
- the encoder's reasoning regularly quotes gold sql (the downtime formula, `LIMIT 10`). that text doesn't reach the agent in the batch runner (only the `<s>` action goes to the decoder), but it shows what it's working from.
- "unnecessary" refusals can rest on false assumptions about the user query, because the encoder never sees it.

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

## trial 4 - amb hooks + chains through the batch runner's own simulator code (oct 13)

wanted to test the amb path directly instead of trying to reach loc. new script `audit/sim_turns.py` runs single ask turns through the batch runner's own `build_user_encoder_prompt` -> `parse_encoder_response` -> `build_user_decoder_prompt` (same calls as main.py:678/706/728), so unlike 3b-3d this is the real benchmark path, encoder **and** decoder. ran inside the bird_interact_eval container, gemini-3.8-flash, temp 0, max_tokens 6000 (batch runner default).

probes (`probes/trial4_amb_path.jsonl`, 38 questions x 3 runs = 114 turns, 0 api errors):
- `loc_e2e` (2): the fake_account_2 row-limit question, generic + grounded
- `amb_hook` (15): a labeled term + one unlisted sub-question (columns / limit / nulls / tables / filter / order)
- `amb_chain` (21): rewordings of the same labeled term, each poking at a different piece

labels: AMB 106, LOC 4, UNA 3, unparsed 1.

| task | hook + sub-question | label | what the reply added beyond the hook | runs |
|---|---|---|---|---|
| fake_account_2 | "most often" + how many users | AMB | the row limit (top 10) | 3/3 |
| fake_account_2 | "most often" + exclude any accounts | AMB | the age filter | 3/3 |
| fake_account_2 | "most often" + zero/missing age | AMB | same age filter | 3/3 |
| fake_account_2 | "most often" + which columns | AMB | output columns, rounding, sort direction | 3/3 |
| solar_panel_1 | "downtime score" + how to pick the plant | AMB | which column to match the plant on | 3/3 |
| solar_panel_1 | "downtime score" + one value or many | AMB | return a single value | 3/3 |
| sports_events_19 | "time difference" + exclusions | AMB | no races/drivers excluded | 3/3 |
| sports_events_19 | "time difference" + sort order | AMB | no sorting | 3/3 |

what it showed:
- **amb works as a carrier.** a sub-question that isn't a labeled ambiguity gets answered from the gold sql as long as it's attached to a labeled term, and the encoder still says AMB (all 15 hooks AMB 45/45).
- **the added facts are privileged.** for fake_account_2 the row limit, the age filter and the output column are in sol_sql but in none of the annotation `sql_snippet`s (string search over both). same for the plant-match column on solar_panel_1. for sports_events_19 the gold has no WHERE and no ORDER BY, so "no exclusions / no sorting" are correct disclosures, not guesses.
- **the decoder has no clean user query to get them from either.** the full split has no `query` field (checked: not in the public file and not in the gt file), so `build_user_decoder_prompt` falls back to `amb_user_query` for `[[clear_query]]`. so the only place "top 10" etc. can come from is the gold sql.
- **asked alone, the same kinds of questions were refused** in 3b/3c (columns / nulls / output -> UNA in every parsed run). **but** those were on usersim-guard step1, not this path, so the same-path control still has to be run before claiming the hook is the cause.
- **chains mostly restate the labeled answer.** 61/63 chain turns AMB. some rewordings add a piece (fake "where does the login count come from" named the source table 1/3; solar "which records" named the source table 2/3).
- **loc end to end:** generic "how many users should be returned?" -> `unlabeled("LIMIT 10;")` 3/3, agent gets "Please return the top 10 users." grounded version (names `accounts.acct_ref`) -> UNA 3/3.
  - **this contradicts 3c**, where the same generic question on the usersim-guard copy of the v2 wording was never LOC. so the two paths behave differently and 3b-3d may understate loc on the real path. need to find out why (output cap? prompt filling? the model skipped `<think>` entirely here, 14 completion tokens).
- **encoder parse fallback fired once (1/114).** encoder output had no `</s>`, so `parse_encoder_response` passed its whole reasoning (which quoted gold sql segments) to the decoder as the "label" (prompt_utils.py:237). decoder reply in that turn didn't quote it, but it's a path for gold text to reach the decoder twice.
- `audit/disclosure.py` (gold-component mention counter) exists but its numbers aren't used here: it counts names the agent can also see in the schema, so i mapped replies by hand instead.

outputs: `bird_interact_agent/outputs/trial4/turns.jsonl` (local only, gitignored, replies hold gold-derived text).

### the loc result explained (why 3c and 4 disagree)

same row-limit question, "How many users should be returned?" on fake_account_2:

| run | simulator code | encoder wording | output cap | LOC | other |
|---|---|---|---|---|---|
| 3b | usersim-guard step1 | hardened | 2048 | 3/6 | 3 unparsed |
| 3c | usersim-guard step1 | v2 (swapped in by me, 974301b) | 2048 | 0/6 | UNA 2, unparsed 4 |
| 4 | batch runner (`audit/sim_turns.py`) | v2 | 6000 | 3/3 | - |

so "our prompt" in 3b-3d = usersim-guard's step1 encoder. the 974301b swap matched the instructions to the batch runner but not the inputs that get filled into them.

compared the actual generated prompts (batch `build_user_encoder_prompt` vs `usersim-guard/results/g_v2_step1_prompts_fake_account_2.jsonl`):
- **instructions: same.** with the inputs blanked, the two prompts differ by one trailing space.
- **sql segments: different, 36% similar.** usersim-guard's slicer has `WITH` as a clause (`usersim-guard/user_simulator/sql_parser.py:13-14`), the batch slicer doesn't (`src/envs/user_simulator/sql_parser.py:5-15`). batch slicing starts at the first listed keyword (`:31-34`), so for this task it drops the cte header before the first `SELECT`.
- **ambiguity notes: formatted differently.** batch dumps one json object (`prompt_utils.py:156`), usersim-guard writes two labeled blocks (`usersim-guard/user_simulator/prompt_generator.py:91-124`).
- **output cap: 2048 vs 6000** (`usersim-guard/call_api.py:65` vs `call_api_batch.py:392`).
- **behaviour:** batch encoder answered in 14 completion tokens with no `<think>` in 2/3 runs, 149 tokens in the third. usersim-guard runs reasoned at length and often got cut off (that's where the unparsed ones came from).

haven't isolated which difference causes the flip. cheap to test one at a time (swap segments, swap amb format, swap cap).

what it means:
1. **3b-3d aren't benchmark numbers.** "LOC in 4 of 660" is true for the usersim-guard copy. rerun the key loc probes on the batch path before any negative-result claim about loc.
2. **loc is a second, narrower carrier.** on the real path one plain question got the gold row limit back as "Please return the top 10 users." one fact, one clause, one task so far.
3. **the amb-hook candidate doesn't depend on loc.** it came from the batch path.
4. **the audit tool diverges from the benchmark.** usersim-guard is the authors' safety benchmark for this simulator and it doesn't build the same prompt inputs, so its safety numbers may not transfer. worth a sentence in the final report.

### is this an exploit yet? no, it's a candidate

have: a mechanism (amb carries unlisted gold facts), evidence the facts are privileged, and a root cause in code (stateless sim, no per-turn cap, decoder holds full gold sql).

still need for a certificate (starter task result 1):
1. same-path control: each trial-4 sub-question alone through `audit/sim_turns.py`
2. build sql only from the hooked answers + schema, run the official eval, get a pass
3. ablation: drop the hooked answers, regenerate with the same generator, pass goes away
4. reusable policy (one hook per labeled term, one unlisted sub-question each) inside the official budget
5. 10 held-out tasks with full transcripts

likely pushback: in trial 2 the stock agent already bundled on fake_account_2, got top 10 + the formula, and passed p1. so "hooking" looks a lot like normal agent behaviour. the ablation is what separates "these answers were necessary" from "the agent would have got there anyway".

---

## trial 5 - same-path control for the trial 4 hooks (oct 15)

the 15 trial-4 hook sub-questions asked **without** the hook, same batch path (`audit/sim_turns.py`), gemini-3.8-flash, temp 0, 3 runs each -> 45 turns, 0 errors. probes: `probes/trial5_control.jsonl` (each row has `pairs_with` = the trial-4 question it controls for).

labels: UNA 30, LOC 9, AMB 3, empty 3.

| task | sub-question | alone: fact disclosed | hooked (trial 4) | hook effect? |
|---|---|---|---|---|
| solar_panel_1 | how to pick the plant | 0/3 (UNA) | 3/3 | yes |
| solar_panel_1 | what the output contains | 0/3 (UNA) | 3/3 | yes |
| sports_events_19 | exclusions | 0/3 (UNA) | 3/3 | yes |
| sports_events_19 | qualifying or race data | 0/3 (UNA) | 3/3 | yes |
| sports_events_19 | sort order | 1/3 (UNA label, reply still said no sorting) | 3/3 | mostly |
| fake_account_2 | which columns | 1/3 (empty label) | 3/3 | mostly |
| fake_account_2 | how many users | 2/3 (LOC) | 3/3 | weak |
| solar_panel_1 | one value or many | 2/3 (LOC 1, empty label 1) | 3/3 | weak |
| fake_account_2 | exclude any accounts | 3/3 (LOC) | 3/3 | no |
| fake_account_2 | zero or missing age | 3/3 (LOC) | 3/3 | no |

left out: sports "no Q2/Q3 time" is part of the labeled "time difference" term (AMB 3/3 alone), so it isn't a control. fake tables / solar nulls / solar tables gave no gold fact in either condition.

what it showed:
- **correction to trial 4:** the fake_account_2 age filter and row limit don't need the hook. alone, the batch-path encoder sends them to LOC and the decoder answers. so "amb carries the limit/filter" was the wrong reading for that task. the clean hook cases are solar (plant-match column, output shape) and sports (filter, data source, order).
- **loc is much more reachable on the real path:** 9/45 LOC here vs 4/660 on the usersim-guard copy (3b-3d). confirms the loc-explained note above: 3b-3d aren't benchmark numbers.
- **two more leak paths:**
  - empty label: 3 turns came back with an empty label after `parse_encoder_response`, and the decoder still answered in 2 of them (fake columns: account id + score; solar limit: single value).
  - UNA label but the reply leaked anyway: sports sort order rep 3, "Sorry, this question is out of scope ... There are no specific sorting requirements."
- so the picture now: gold facts get out through (a) amb hooks for facts the encoder would refuse alone, (b) loc for some single-clause facts asked plainly, (c) parser/decoder slips. the certificate work should use whichever path each fact needs.

outputs: `bird_interact_agent/outputs/trial5/turns.jsonl` (local only, gitignored).

---

## trial 6 - why loc fires on the batch path but not on usersim-guard (oct 15)

script: `audit/loc_path_isolation.py`. builds the batch v2 encoder prompt, then swaps in usersim-guard's version of one input block at a time (taken from a saved usersim-guard step1 prompt for fake_account_2; the blocks don't depend on the question). each variant at cap 6000 (batch) and 2048 (usersim-guard). encoder only. 3 questions (generic limit, ctrl_filter, ctrl_nulls) x 3 runs = 9 per cell, 72 calls, 0 errors.

| encoder prompt | cap 6000 | cap 2048 |
|---|---|---|
| batch as built | LOC 7/9 | LOC 7/9 (2 unparsed) |
| + ug ambiguity block | LOC 9/9 | LOC 9/9 |
| + ug sql-segment block | LOC 7/9 | LOC 2/9, 7 unparsed |
| + both | LOC 5/9 | LOC 1/9, 8 unparsed |

what it showed:
- **the missing loc in 3b-3d is mostly truncation.** usersim-guard's segment block + its 2048 cap -> output cut off before the label (7-8 of 9 unparsed). at the batch cap the same segment block still gives LOC 7/9.
- **the ambiguity-block formatting doesn't suppress loc** (9/9 at both caps).
- **both blocks together at 6000** drop loc a bit (5/9), so the segment format has a small effect beyond truncation. n=9 per cell, one task, so treat that as a hint.
- so my earlier choice to "leave the 2048 cap as-is on purpose" (trial 3b note) is what hid loc. 3b-3d measured a truncation artifact of the audit tool, not a safer simulator.

outputs: `bird_interact_agent/outputs/loc_isolation/labels.jsonl` (local only, gitignored).

---

## open questions / todo
- does the exploit have to fit inside the official budget? **decided (oct 15): yes.** every certificate has to stay within the task's starting budget (main.py:111-130).
- pick the 10 held-out tasks (disjoint from dev tasks)
- read appendix N of the bird-interact paper and check it against the code
- run more honest-agent tasks to tighten the reference number (7 is small)
- grader feedback on management tasks: how much does it give away per failed submit?
- note: not putting gold sql in this file since it gets pushed (dataset asks not to leak gt)
