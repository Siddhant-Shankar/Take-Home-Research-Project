# Question-Asking Policy for the Simulator (v3, final)

Oct 17, 2026 · @Siddhant Shankar

## Scope and evidence base

This policy tells the agent how to spend its ask budget against the v2 user simulator: how many terms to expect, how to pick them from the public question, how to phrase each ask, and which SQL components to ask about first. It is derived from three dev tasks (fake\_account\_2, solar\_panel\_1, sports\_events\_19), 3 reps per condition, trials 4 and 5 plus the reconstruct runs, all at user\_patience 6. One task produced a clean in-budget pass with an ablation; the other two are a weak pass and an unresolved failure.

What the agent sees: the public question, schema, agent-visible KB, its budget in the first system note, and simulator replies. It never sees annotations, gold SQL, or ambiguity counts. Every rule below is stated in terms of those inputs only.

What this policy does not contain: gold SQL, simulator reply text, or generated SQL. Dev terms were chosen with knowledge of the annotations, so the dev checks in this doc are sanity checks, not validation. Held-out runs are the test.

## Mechanism

The simulator gates per question, not per clause, and the decoder has gold SQL in context whenever it answers. A compound question that earns an AMB label on one clause gets a full decoder reply covering every clause, including the SQL-component slot that would have been refused on its own.

| Label | Encoder output | When it fires | Decoder behaviour |
| --- | --- | --- | --- |
| AMB | `labeled("<term>")` | question touches an annotated ambiguity | answers from annotation, gold SQL in context |
| LOC | `unlabeled("<sql segment>")` | unannotated but judged valuable; encoder copies a gold SQL segment into the label | answers from that segment |
| UNA | `unanswerable()` | neither annotated nor judged necessary | refuses |

Two consequences drive the policy. First, a slot question asked alone depends on the encoder judging it valuable; across 15 control rows, 10 came back all UNA or UNA plus empty. The same slots paired with a term clause were labeled AMB on 45 of 45 reps. Second, a hook can only surface a fact that exists in gold. Where the slot has no gold counterpart (fake tables, solar nulls, solar tables), hooked and alone both returned nothing. UNA-alone is necessary but not sufficient evidence that a hook will pay.

LOC is a second disclosure path that does not need a hook, but it is slot-dependent: reliable for a distinctive WHERE clause (fake exclusions and nulls, 3/3), partial for LIMIT (fake 2/3, solar 1/3), absent for SELECT list, table choice and ORDER BY.

## Rule 1: derive k from the budget

k = (budget − 6 − P) ÷ 2, where budget is read from the first system note and P is the simulator's user\_patience, a run constant the agent is given in its config. The agent never estimates k from the question.

This follows from the budget formula 3 + 3 + 2·(critical + knowledge ambiguities) + user\_patience (main.py:111-135). Dev trials ran at P = 6; the benchmark default is P = 10 (main.py:916). This policy fixes **P = 10 as the official value**: a run that fits at P = 6 also fits at P = 10 with 4 coins to spare, so dev passes stand, and held-out runs use 10 unless the config says otherwise. Dev check at P = 6: 14 → 1, 18 → 3, 16 → 2, all correct.

Edge cases:

- (budget − 6 − P) odd or negative: the formula or P has changed. Log it; do not guess k.
- k = 0: there is no AMB path. Skip definitional clauses. Asks = floor((P − 1) ÷ 2): 2 at P = 6, 4 at P = 10. Spend them on slots asked alone, LOC-reliable first (exclusions, then limit, then nulls, then the rest of Rule 4 order), skipping any slot the question already states (a stated top-N skips the limit slot). \[Refined Oct 17 after stage 1. Earlier text: spend asks on the LOC-reliable slots alone, exclusions first, then limit. Task affected: cold\_chain\_pharma\_compliance\_20, which ran 0/3 under the earlier text with its own recorded deviation; the refined text describes what that run did, so it is not evidence for the rule.\]

k sets the ask count (Rule 3) and tells Rule 2 how many terms to look for, including how many are nested rather than visible.

## Rule 2: term selection from the public question only

Apply these five steps to the public question text, after get\_schema and get\_all\_knowledge\_definitions have returned. No other input is used.

1. **Collect candidates by class**, in this priority order:
   1. Compound metric nouns: a noun phrase naming a computed quantity whose head is score, rate, ratio, index, difference, deficit, gap, margin, level, count or share, or a nominalization (downtime, utilization).
   2. Frequency or degree superlatives and vague quantifiers: most, least, often, top, best, high, low, recent, active, popular, frequently.
   3. Unqualified categorization nouns with no stated criteria: groups, categories, tiers, clusters, buckets, segments, bands.
   4. Relative or comparative phrases: for how X they are, based on X, relative to, compared with, adjusted for.
2. **Exclude** any candidate that is (a) a quoted string that looks like a data value (an ID pattern, a proper noun, a code), treated as a literal; a quoted common-noun phrase ('uniqueness' score) is a candidate, since quotes there mark vagueness, not a value \[refined Oct 17 after stage 1; earlier text: every quoted string is a literal and excluded; task affected: insider\_trading\_5, which ran with the 'uniqueness' score as an anchor before this refinement, so the refinement makes that run compliant after the fact\]; (b) an exact or near match to a schema table or column name or its column meaning; (c) an exact match to a KB entry name, ignoring case and singular versus plural, with the question's phrase quoted in full; a substring of a KB name is not a match \[clarified Oct 17 after stage 1 to resolve three rulings under one rule: insider\_trading\_5 'high-risk' is a substring and kept; mental\_health\_14 'high-risk patients' matches High-Risk Patient apart from the plural and is excluded; hulushows\_14 'maximum promo saturation ratio' matches Maximum Promo Saturation Ratio and is excluded\]; (d) a plain column reference in the output list (driver IDs).
3. **Merge co-referents.** The Return clause usually re-names quantities already introduced. Collapse phrases denoting the same thing into one candidate and keep the first surface form as the anchor, since that is where the annotation attaches.
4. **Take the top k by class rank.** Candidates below the cut become slot content, not anchors (based on performance → the cut-off slot).
5. **If candidates < k, the remainder are nested.** They live inside the definition of a surface term and appear only in a decoder reply. After each broad definitional ask, scan the reply for terms not in schema or KB and not defined in the reply itself. Those are anchors for follow-up hooks. Expect exactly k − (surface count) of them.

**Step 6 (decision recorded Oct 17 before any held-out simulator call; two clauses refined later, each marked).** When steps 1–4 leave zero surface anchors but k > 0, the ambiguities sit inside the KB entry that caused the exclusion. Read that entry's description and definition fields (both agent-visible) and run steps 1–4 on their text: terms they use that are neither schema names nor other KB entry names become the anchors. Step 6 anchors count as surface anchors for the nested count (nested expected = k − step-6 anchors). Step 3's merge applies to the entry's own text as well: a superlative or degree form of the dropped term found there ('most saturated', 'overloaded') co-refers with it and is dropped with it, not promoted to an anchor \[clarified Oct 17 after phase A was written for other tasks and before any hulushows probe; earlier text read only the definition field and did not apply the merge to the entry's text\]. If that yields nothing, fall to the k = 0 path and record a Rule 2 failure for the task \[fallback clause added Oct 17 after stage 1; not part of the pre-run decision\]. First use: hulushows\_14, where the full question phrase 'maximum promo saturation ratio' is an exact match to the KB entry Maximum Promo Saturation Ratio.

**Anchor phrasing.** Quote the term as it first appears in the question. A longer containing span is safe: hook\_columns quoted the full clause and was still labeled most often on 3/3 reps. When the span is unclear, quote up to the clause boundary rather than guessing the short form. The bare anchor 'most often' was labeled on 15 of 15 reps in the fake\_account\_2 validation, so the short form is safe when Rule 2 gives it.

**Dev sanity check** (circular; dev terms were annotation-informed):

| Task | Candidates after exclusion | Merge | k | Picked | Gold |
| --- | --- | --- | --- | --- | --- |
| fake\_account\_2 | most often (class 2); for how old they are (class 4, likely dropped if column meaning covers AGE\_D) | none | 1 | most often | match |
| solar\_panel\_1 | downtime score (class 1); quoted plant name excluded as literal | none | 3 | downtime score; 2 nested expected | surface match; nested terms (down, System Unavailability) are not in the question |
| sports\_events\_19 | time difference, average deficits (class 1); groups, cluster (class 3); based on performance (class 4) | deficits → time difference; cluster, performance → groups | 2 | time difference, groups | match |

Sports passes only because of step 3. Without the merge, class rank picks two metric nouns and misses groups. That is where the rule is expected to break first on held-out tasks.

## Rule 3: question template and ask count

Every ask is a two-part compound: a definitional clause anchored on a term, plus exactly one SQL-component slot.

> How exactly should '〈term〉' be measured, and 〈slot question〉?

The definitional half earns the AMB label; the slot half is the information needed. One slot per ask. The simulator already over-delivers on a single slot (the columns slot returned rounding and sort direction unprompted), and a double-slot question has not been tested.

**Always hook.** An ask costs 2 coins whether or not it carries a term clause, and no run showed hooking reducing disclosure. The agent cannot know in advance whether a slot would fire LOC on its own, so the hook is the default for every slot.

**The term clause carries the definition.** Every hook asks how the term is measured, so the definition arrives with the first hook reply. A standalone broad ask (What does '〈term〉' mean to you?) is not on the default path. It is used only when k exceeds the surface count and the first hook reply on each surface term has not surfaced the nested terms (Rule 2 step 5). This branch is the weakest element of the policy: it rests on solar alone, where chain\_1 passed 3/3 against a gold answer that is a single NULL row. Narrow sub-questions that decompose a term are wasted either way: fake\_account\_2 amb\_only spent 11 coins on two chain questions and passed 0/3.

**Ask count.** After get\_schema (1), get\_all\_knowledge\_definitions (1) and submit (3), the remaining budget is 1 + 2k + P. Reserving 2 executes for generator error fixes gives asks = floor((P − 1 + 2k) ÷ 2): k + 2 at P = 6, k + 4 at P = 10. The dev-tested allocation is k + 2. At P = 10 the extra asks go to the next slots in Rule 4 order, since no run showed an added hooked slot reducing the pass rate; that allocation is itself untested.

| k | Budget at P = 6 | Asks at P = 6 | Budget at P = 10 | Asks at P = 10 |
| --- | --- | --- | --- | --- |
| 1 | 14 | 3 | 18 | 5 |
| 2 | 16 | 4 | 20 | 6 |
| 3 | 18 | 5 | 22 | 7 |

fake\_account\_2 budget\_min used exactly the P = 6 allocation: 3 asks, 2 executes, 13 of 14 coins, 3/3 pass.

**Asks are independent.** The simulator keeps no memory of earlier questions; user\_patience is only a term in the starting budget (main.py:111-135), not a counter. Ask order does not affect labels, and the single-turn dev runs are representative of a continuous transcript.

## Rule 4: slot priority

Fill slots in this order until the ask count is spent. Priority reflects two things: how much a reply disclosed when it did pay, and how often the slot had a gold counterpart at all.

| Priority | Slot | Example wording | Alone (trial 5) | Hooked (trial 4) | Evidence of necessity |
| --- | --- | --- | --- | --- | --- |
| 1 | Output columns | which columns should the result show? | UNA on all three tasks (fake 1/3 disclosed via empty label) | 3/3 on fake and solar; one reply covered columns, rounding and sort direction | Highest yield per ask; sports not reported |
| 2 | Cardinality: row limit, or one value vs. per-record | how many users do you want back? / should I return one value or one per record? | fake 2/3 (LOC), solar 2/3 (LOC 1, empty 1) | 3/3 | Decisive on fake: budget\_min\_minus\_limit fell from 3/3 to 0/3 |
| 3 | Exclusions | should any accounts be excluded? | fake 3/3 (LOC), solar plant-match 0/3, sports 0/3 | 3/3 on all three | Fake and solar replies carried a gold fact; sports reply was no exclusions |
| 4 | Null and zero handling | what should happen for rows whose X is zero or missing? | fake 3/3 (LOC, same WHERE as exclusions); solar 0/3; sports AMB alone (part of annotation) | fake 3/3; solar nothing either way | Redundant with slot 3 on fake; no gold fact on solar |
| 5 | Source table or granularity | should this come from the account records or the session records? | UNA on all three | sports 3/3; fake and solar nothing either way | Mixed: pays only when table choice is a real gold decision |
| 6 | Sort order | how should the output be sorted? | sports UNA label, reply leaked 1/3 | 3/3 | Lowest yield; sports reply was no sorting |

Slots 3 and 4 were the same WHERE clause on fake\_account\_2. A merged slot (should any records be excluded, including ones with missing or zero values?) would save 2 coins there, but a double-slot question has not been run and could return UNA or a half-answer. Keep them separate until the merged form is tested (see untested elements).

When the ask count is smaller than the number of slots with a plausible gold counterpart, drop from the bottom of this table first.

**When asks exceed the six slots** (k ≥ 3 at P = 10): after slot 6, remaining asks go first to a standalone broad definitional ask for each surface term that has not yet anchored a hook, then to hooks anchored on nested terms (slots 4–6 only; see batch addendum decision 6), then stop. Unspent coins are fine; a seventh slot is not invented.

## Batch addendum: later asks after the first replies (Oct 17, 2026)

Written after stage 1 results for six prewritten held-out tasks and after the phase A replies for three others were read, and before any phase B question was written. It settles how the procedure runs when asks are sent as single-turn batches instead of one live conversation. It names no task-specific terms.

**Why a split exists.** Procedure step 4 assumes the agent reads each reply before choosing the next ask. The batch simulator takes all of a task's questions up front, so for tasks where k exceeds the surface count the asks are written in two phases: phase A is slots 1–3 (columns, cardinality, exclusions) hooked on the surface anchors; phase B is every remaining ask, written after the phase A replies are read. Tasks where k equals the surface count have all asks written up front and are unaffected.

**Decisions.**

1. **Which replies to scan:** rep 1 only. The frozen conditions use rep 1 answers, and a live agent sees one reply per ask. Reps 2 and 3 are not read for anchors.
2. **Which asks to scan:** hooked asks (family policy\_v3) only. The broad `v3_broad*` asks belong to the amb\_only condition in stage 2; a live agent on the frozen procedure would not have asked them, so terms found only there never become frozen anchors.
3. **What counts as surfaced:** a term in a rep-1 hooked reply that is not a schema table, column or JSON field name (a near match such as a spaced or lower-cased column name counts as the column and is excluded), not a field named in the column meanings, not a visible KB entry name, and not defined in the same reply (a formula or criterion given alongside it means it is defined; solar's system unavailability is the precedent). Terms that fail any test are slot content, not anchors.
4. **Count mismatch:** if more terms surface than k − surface, keep them in order of first appearance across slots 1, 2, 3, up to k − surface; the rest are slot content. If fewer surface, including none, the remaining slots cycle the surface anchors.
5. **Which slots a nested anchor gets:** the remaining Rule 4 slots in order (nulls, table, sort). In a live run the agent would have hooked a nested term as soon as it surfaced, possibly on slot 2 or 3; the batch split gives nested anchors only the lower slots. This is accepted and recorded as a deviation rather than allowing a higher slot to repeat, so that every held-out task runs under one procedure. The hook's term clause elicits the definition whatever slot carries it.
6. **Broad asks:** procedure step 5 and the beyond-six rule use the same mechanism. Step 5 fires when nested terms are expected and none surfaced under decisions 1–3: the next ask is a broad question on the surface anchor most likely to contain them, with id `v3_step5`, family policy\_v3, taking the place of the lowest-priority remaining slot. Beyond-six asks take ids `v3_extra1`, `v3_extra2`, family policy\_v3, and go: branch 1, a broad ask for a surface term that has not anchored a hook; branch 2, a nested-term hook on the highest-priority slot from 4–6 that anchor has not yet had, never slots 1–3; if it has had all of 4–6, fall to branch 3; branch 3, unspent. Stage 2 `v3_broad*` ids are never reused for frozen asks.
7. **Skipped slots:** a slot whose answer the rep-1 hooked replies have already given in full is skipped (the fake\_account\_2 precedent: a columns reply carrying sort direction). The freed ask goes to step 5 if it applies, else to the next Rule 4 slot, else unspent.
8. **Zero-surface tasks (Rule 2 step 6)** get the same split: step 6 anchors take slots 1–3 in phase A, and phase B is written after those replies are read. Step 6 reads the KB entry's description and definition fields, both of which are agent-visible, and a surface candidate that co-refers with the dropped term (a superlative form of it, say) is merged into it under Rule 2 step 3 and dropped with it, so the task counts as zero-surface; the same merge applies to superlative or degree forms of the dropped term found in the entry's own text, which are not promoted to anchors. **Tasks whose Rule 2 surface count is below k** get the split; tasks where it equals k do not.

**Caveats for the write-up.** The claim in Rule 3 that single-turn runs represent a continuous transcript holds for the prewritten tasks outright, and for phase B tasks only because decisions 1 and 2 restrict the scan to what a live agent would have seen. Decision 5 means nested anchors never occupy slots 1–3 in the batch, which a live agent might have done; this is the one place the batch procedure is weaker than the live one.

## Procedure

1. Read the budget from the first system note. k = (budget − 6 − P) ÷ 2 with P = 10 unless the run config says otherwise. Asks = floor((P − 1 + 2k) ÷ 2).
2. Call get\_schema and get\_all\_knowledge\_definitions (2 coins). Skip the per-column and per-knowledge lookups.
3. Run Rule 2 on the public question. Record surface terms (up to k), leftover candidates as slot content, and the expected nested count k − surface.
4. Spend asks on hooked slots in Rule 4 order. Anchor each on a surface term, cycling through the terms when k > 1 so every term gets at least one hook. Scan the first reply for each term for nested terms (not in schema or KB, not defined in the reply) and add them as anchors.
5. Only if k exceeds the surface count and no reply has surfaced the nested terms after one hook per surface term: spend one ask on a standalone broad question (What does '〈term〉' mean to you?) for the term most likely to contain them.
6. Generate SQL once, after all asks, from question, schema, KB and the collected answers.
7. Execute only to fix generator errors, up to 2 times.
8. Submit.

Stop condition: 2 × asks + executes ≤ budget − 5 at all times. If a reply resolves a lower-priority slot unprompted (a columns reply carrying sort direction), skip that slot and move down the list.

**Validation on fake\_account\_2 (Oct 17, 2026): passed.** The frozen procedure was run end to end with the bare anchor 'most often' and slots 1–5 (`probes/policy_v3_fake_account_2.jsonl`, `audit/policy_v3_plan.json`). All 15 simulator reps were labeled `most often`. Results, rep-1 answers, 3 generator reps each:

| Condition | P | Budget | Asks | Passed | Coins |
| --- | --- | --- | --- | --- | --- |
| none | 6 | 14 | 0 | 0/3 | 7 |
| p6 (slots 1–3) | 6 | 14 | 3 | 3/3 | 13 |
| p6 minus cardinality | 6 | 14 | 2 | 0/3 | 11 |
| p10 (slots 1–5) | 10 | 18 | 5 | 3/3 | 17 |
| p10 minus cardinality | 10 | 18 | 4 | 0/3 | 15 |

Removing the cardinality answer (slot 2) drops the pass to 0/3 at both P values, so this is certificate #1 under the frozen procedure, not a reconstruction match. The P = 10 allocation (k + 4 asks) passed within budget on this task. Caveats carried from dev: the row limit also leaks via LOC when asked alone; one task.

## Held-out set (fixed before any run)

Seeded draw 20261017, one Query task per eligible database, dev tasks and their databases excluded. Rule 2 applied to the public question only; exclusion steps (b) and (c) were run against schema and KB before any probe was placed. P = 10. The anchors column was fixed before each task's first run (for mental\_health\_14, the alternate, that was after stage 1); the last column records what the phase A scan showed afterwards and is not part of the pre-run record.

| # | Task | k | Budget | Asks | Rule-2 anchors (fixed before the task's first run; exclusion b, c applied) | Nested expected | Phase A scan, after run (addendum decisions 1–3) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | hulushows\_14 | 4 | 24 | 8 | 'maximum promo saturation ratio' DROPPED (exact KB match, step 2c); 'most promo-saturated' merges into it and is dropped. Step 6 on KB entry 82 description and definition: 'promotional messages'; 'most saturated' and 'overloaded' merge into the dropped term. One step-6 anchor. Cannot be a certificate: none passes 3/3. | 3 | not yet run |
| 2 | mental\_health\_14 (alternate 1; robot\_fault\_prediction\_11 reported invalid: gold answers a different question) | 3 | 22 | 7 | 'go-to' experts (merges most experience); 'most complex patient cases' (merges these high-risk patients). 'high-risk patients' excluded: matches KB High-Risk Patient apart from the plural. | 1 | not yet run |
| 3 | planets\_data\_14 | 3 | 22 | 7 | 'type of light filter' (merges group the filters together properly) | 2 | nothing surfaced: the one candidate appears only in reps 2–3 and the broad reply; 'photometric band' is the Photo\_Band field |
| 4 | exchange\_traded\_funds\_18 | 2 | 20 | 6 | 'value funds' | 1 | nothing surfaced: every reply term is a schema field or KB name |
| 5 | crypto\_exchange\_19 | 3 | 22 | 7 | 'arbitrage opportunity' (substring of KB High-Quality Arbitrage Opportunity: kept); 'significant' | 1 | nothing surfaced; labeled as one merged term, significant arbitrage, 15/15 |
| 6 | insider\_trading\_5 | 2 | 20 | 6 | the 'uniqueness' score (quoted common noun, kept under 2a as refined after the run); 'high-risk' (substring of a KB name: kept) | 0 | n/a, all asks prewritten |
| 7 | virtual\_idol\_9 | 1 | 18 | 5 | 'top fans'; leftover cohort analysis, milestones → slot content | 0 | n/a |
| 8 | disaster\_relief\_11 | 1 | 18 | 5 | 'overall efficiency' (merges cost per person) | 0 | n/a |
| 9 | reverse\_logistics\_1 | 2 | 20 | 6 | 'overall handling cost'; 'major fee components' | 0 | n/a |
| 10 | cold\_chain\_pharma\_compliance\_20 | 0 | 16 | 4 | none (k = 0 path) | 0 | n/a |

Phase B asks for planets, etf and crypto, and the phase A asks for hulushows and mental\_health, were drafted after the batch addendum text above was final and are committed after it (`probes/policy_v3_heldout_phaseB.jsonl`). Derivation under the addendum: planets `v3_step5`, `v3_slot5`, `v3_slot6` (nulls skipped, decision 7; ask 7 unspent); etf `v3_step5`, `v3_slot4`, `v3_slot5` (sort skipped, single count); crypto `v3_step5`, `v3_slot4`, `v3_slot5` (sort skipped, single label; ask 7 unspent); hulushows and mental\_health slots 1–3 plus amb\_only broad asks, with asks 4 onward written after their rep-1 replies are read.

Round 2 (`probes/policy_v3_heldout_round2.jsonl`, asks 4 onward, from rep-1 hooked replies only, encoder labels withheld): hulushows `v3_slot4`, `v3_step5` (nothing surfaced; table skipped because the replies name the tiernotices column, which exists only in promo\_info; sort skipped because the replies ask for a single maximum value; the second freed ask and both extras unspent, branch 3); mental\_health `v3_slot4` (anchor B), `v3_slot5` (anchor A), `v3_step5` (nothing surfaced; sort skipped, resolved in the rep-1 replies; extra 1 unspent, branch 3). The hulushows table skip reads "given in full" more strictly than the fake\_account\_2 precedent (sort stated outright); the task cannot be a certificate either way. No third round: a nested term surfacing in a round 2 reply gets no follow-up hook, the same batch limit as decision 5. The first-round question briefs (phase B for planets, etf, crypto) showed encoder labels and all three reps; no nested anchor came from them, and the round 2 briefs withheld labels, reps 2–3 and broad replies.

No anchor matches a schema table or column name.

Stress cases named in advance: #7 (class rank picks top fans over cohort analysis and milestones; if the label is one of those, Rule 2 step 1 needs an analysis-noun class), #1 (zero surface anchors; first use of step 6), #3 (surface count far below k; tests step 5), #10 (no AMB path). Observation from phase A, recorded but not acted on mid-run: crypto's label was the merged phrase significant arbitrage, so Rule 2 step 3 should also merge a degree adjective into the noun it modifies; both separate anchors were still labeled, so no disclosure was lost.

Recorded deviations in the stage-1 probes: the table slot uses schema-free wording (which table or records should it be computed from?) so no probe waits on get\_schema. cold\_chain\_pharma\_compliance\_20 asks exclusions, nulls, columns, then tie handling; the limit slot is skipped because the question states top 3, and tie handling is tagged slot order in the probe file and is an ordering sub-question not in the Rule 4 table as written. If cold\_chain passes, the tie answer is treated as a non-policy answer in the ablation. Hook wording: the planets, etf and crypto hooks say "determined" and the hulushows hooks say "counted" in place of "measured"; the term clause is otherwise the template.

**Run order.** Stage 1, all 10 tasks: none and frozen P = 10. Stage 2, only tasks where frozen passed 3/3: amb\_only (one broad ask per surface anchor, no slots) and one ablation per answer. Stage 3, optional, certificate candidates only: frozen at P = 6. Certificate #2 = frozen pass within budget, amb\_only below 3/3, and at least one ablation below 3/3.

Alternates, in order, only for a broken task: mental\_health\_14, cross\_border\_1, households\_7, archeology\_scan\_10, cybermarket\_pattern\_2.

## Held-out results (after runs)

Recorded from the stage 1 and stage 2 reconstruct outputs. Generator handling as on dev: gemini-3.8-flash writes SQL from question, schema, agent-visible KB and the rep-1 answers of the condition; executes are used only for its own error fixes, up to 2; graded by execute\_submit\_action phase 1; 3 generator reps per condition; coins computed at P = 10. Stage 1 rows whose generated SQL was prose or cut off mid-query (insider\_trading\_5 and virtual\_idol\_9, 8 of 48 rows) were classed as generator failures, not evidence; both tasks were rerun, all conditions, with `reconstruct.py` resampling any generation that does not look like complete SQL before executing it (no execute coin).

| Task | none | frozen\_p10 | amb\_only | ablations | Verdict |
| --- | --- | --- | --- | --- | --- |
| reverse\_logistics\_1 | 0/3 (6) | 3/3, 18 of 20 | 0/3 (10–11) | minus slot 1: 1/3; slot 2: 2/3; slot 3: 3/3; slot 4: 3/3; slot 5: 2/3; slot 6: 3/3 (16 each) | Meets the certificate #2 rule; ablation drops are small at n = 3, 10-rep run in progress |
| insider\_trading\_5 | 0/3 (rerun) | 2/3, 19 of 20 (rerun) | — | — | Below 3/3; no stage 2. Stage 1 gave 1/3 with generator failures, which is why it was rerun |
| virtual\_idol\_9 | 0/3 (rerun) | 0/3, 16 of 18 (rerun) | — | — | Fail, wrong result 3/3 |
| disaster\_relief\_11 | 0/3 | 0/3, 16 of 18 | — | — | Fail |
| cold\_chain\_pharma\_compliance\_20 | 0/3 | 0/3, 14–15 of 16 | n/a (k = 0) | — | Fail under the k = 0 path as then written |
| hulushows\_14 | 3/3 (7) | not run | — | — | Passes with no answers; cannot be a certificate |
| robot\_fault\_prediction\_11 | 0/3 | 0/3, 18 of 22 (6 asks, 7th unspent) | — | — | INVALID: gold answers a different question; replaced by mental\_health\_14 |
| planets\_data\_14, exchange\_traded\_funds\_18, crypto\_exchange\_19 | 0/3 each (6) | pending phase B | — | — | pending |
| mental\_health\_14 | not run | not run | — | — | pending |

Figures from `bird_interact_agent/outputs/reconstruct/policy_v3_heldout_stage1.jsonl`, `policy_v3_heldout_rerun.jsonl` and `policy_v3_heldout_stage2_reverse.jsonl`; the 10-rep reverse\_logistics\_1 run will replace the n = 3 ablation figures when it finishes.

## Untested or weakly supported elements

Each row names the element, how much dev evidence it has, and the cheapest test that would settle it.

| Element | Support | Risk | Test |
| --- | --- | --- | --- |
| Frozen procedure end to end | Passed on fake\_account\_2 (Oct 17): p6 3/3, p10 3/3, ablations 0/3 | One task, k = 1 | Held-out set |
| P = 10 allocation (k + 4 asks) | Passed within budget on fake\_account\_2 (17 of 18 coins) | One task; slots 4–5 added nothing the pass needed | Held-out tasks with k ≥ 2 |
| Rule 2 term selection | Passes all three dev tasks, but dev terms were annotation-informed; bare anchor confirmed 15/15 AMB on fake | Merge step (3) is the fragile part; may over- or under-count | Held-out runs; compare picked terms against labels in the first encoder output |
| Nested-term detection (Rule 2 step 5) | Not exercised: solar's broad reply resolved down and System Unavailability with no follow-up | Reply may not name nested terms explicitly | Held-out #1 and #3 (surface count far below k) |
| Standalone broad ask (procedure step 5) | One task (solar chain\_1, 3/3) whose gold is a single NULL row | Weakest element; a hook may do as well | Run solar hooks-only (filter, limit, columns) without chain\_1; not yet run as a condition |
| Asks beyond six slots rule | Untested; drafted in the plan file before stage 1 (not committed), added to this doc Oct 17 after stage 1 | Broad asks on already-anchored terms may add nothing | Held-out tasks with k ≥ 3 |
| Merged exclusions + nulls slot | Untested; the two were one WHERE clause on fake only | Could return UNA or answer only one half | Run on fake\_account\_2, where each half discloses 3/3 alone, so a half-answer is detectable |
| Always hook | No case where hooking reduced disclosure; 60/60 AMB labels across dev and validation | None observed | Carry forward |
| Source table slot | 3/3 on sports, nothing on fake or solar | Low expected yield | Keep at priority 5 |
| Slot priority order | Fit to trials 4 and 5; order held on fake validation | Over-fit to three tasks | Held-out pass rate by slot dropped |

## Per-task verdicts and open items

**fake\_account\_2** (k = 1, budget 14 at P = 6, 18 at P = 10). **Certificate #1.** Dev reconstruction budget\_min passed 3/3 at 13 coins and the frozen procedure reproduced it end to end on Oct 17 (p6 3/3 at 13, p10 3/3 at 17; both ablations without the cardinality answer 0/3). The deciding fact is also reachable alone via LOC (2/3), so the hook made disclosure reliable rather than possible. Caveats: term chosen with knowledge of the annotation on dev; the bare anchor was then confirmed 15/15 in validation.

**solar\_panel\_1** (k = 3, budget 18 at P = 6). Honest pass but a weak test. amb\_only (one broad question) passed 3/3 at 8 coins; gold is a single NULL row. Two of the three labeled terms are nested and not in the question; the broad reply resolved them without follow-up.

**sports\_events\_19** (k = 2, budget 16 at P = 6). Fails under every condition run, including full at 22 coins. Withdrawn hypotheses: cluster naming (generated SQL already used the gold's three names) and Q2/Q3 fallback (generated SQL already falls back Q3 → Q2 → Q1). The expected gap is row filtering and pole-time computation. Two observations to check locally against gold:

- chain\_6 (how pole time is worked out) was in every failing condition. Either the answer was insufficient or the generator misapplied it.
- hook\_filter returned no races or drivers excluded. If the gap is in filtering, the decoder under-disclosed relative to gold's effective row set (for example gold dropping \\N or empty times through casting rather than an explicit WHERE). That would be a simulator finding, not a policy miss.

Open runs: amb\_only + chain\_4 (cheap, clean); budget\_min, full\_minus\_tables and full\_minus\_columns (not run, credits ran out); a diff of generated vs. gold on the WHERE and pole subquery (local, no rerun).

## Separate finding: parser pass-through

The label parser (prompt\_utils.py:232-243) takes whatever sits between `<s>` and `</s>` with no check that it is one of the three labels. Two failure modes reach the decoder:

- Empty label: 3 of 45 trial-5 turns (about 7%). The decoder still answered in 2 of those 3.
- Unclosed label: 1 of 114 trial-4 labels. The whole encoder output, including reasoning that can quote gold SQL, is passed to the decoder as the label.
- Markdown-wrapped label: 1 of 54 held-out round 1 turns (crypto\_exchange\_19) came back as `**labeled("significant arbitrage")**` and was passed through unchecked.

This accounts for the empty-label disclosures in the control rows (fake columns 1/3, solar plant-match, solar one-value). It is a low-rate simulator bug, not a policy lever, and should be reported separately from the hook finding. The policy does not depend on it.
