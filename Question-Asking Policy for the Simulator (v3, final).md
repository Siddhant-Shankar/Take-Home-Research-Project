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
- k = 0: there is no AMB path. Skip definitional clauses; spend asks on the LOC-reliable slots alone (exclusions first, then limit).
  - Held-out deviation (recorded Oct 17): cold\_chain\_pharma\_compliance\_20 (k = 0, 4 asks at P = 10) asked exclusions, null handling, output columns and tie handling, each alone. It did not ask about the limit, and tie handling is not in the Rule 4 table.

k sets the ask count (Rule 3) and tells Rule 2 how many terms to look for, including how many are nested rather than visible.

## Rule 2: term selection from the public question only

Apply these five steps to the public question text, after get\_schema and get\_all\_knowledge\_definitions have returned. No other input is used.

1. **Collect candidates by class**, in this priority order:
   1. Compound metric nouns: a noun phrase naming a computed quantity whose head is score, rate, ratio, index, difference, deficit, gap, margin, level, count or share, or a nominalization (downtime, utilization).
   2. Frequency or degree superlatives and vague quantifiers: most, least, often, top, best, high, low, recent, active, popular, frequently.
   3. Unqualified categorization nouns with no stated criteria: groups, categories, tiers, clusters, buckets, segments, bands.
   4. Relative or comparative phrases: for how X they are, based on X, relative to, compared with, adjusted for.
2. **Exclude** any candidate that is (a) a quoted string, treated as a literal value; (b) an exact or near match to a schema table or column name or its column meaning; (c) an exact match to a KB entry name; (d) a plain column reference in the output list (driver IDs).
3. **Merge co-referents.** The Return clause usually re-names quantities already introduced. Collapse phrases denoting the same thing into one candidate and keep the first surface form as the anchor, since that is where the annotation attaches.
4. **Take the top k by class rank.** Candidates below the cut become slot content, not anchors (based on performance → the cut-off slot).
5. **If candidates < k, the remainder are nested.** They live inside the definition of a surface term and appear only in a decoder reply. After each broad definitional ask, scan the reply for terms not in schema or KB and not defined in the reply itself. Those are anchors for follow-up hooks. Expect exactly k − (surface count) of them.
6. **If a candidate is dropped under step 2c (exact match to a visible KB entry name), take anchors from that entry's definition text.** Apply steps 1 to 4 to the definition text instead of the question. Added Oct 17 for hulushows\_14, after the held-out draw and before any of its hooked asks were written (decision recorded in `FIles/README (2).md`, commit a90e1c0).

**Anchor phrasing.** Quote the term as it first appears in the question. A longer containing span is safe: hook\_columns quoted the full clause and was still labeled most often on 3/3 reps. When the span is unclear, quote up to the clause boundary rather than guessing the short form.

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

**Validation before held-out.** On fake\_account\_2 at P = 6 this procedure yields three hooks (columns, cardinality, exclusions) and no broad ask, which is the budget\_min condition that passed 3/3 at 13 coins. That is a reconstruction match, not a run of the frozen procedure: budget\_min's answers came from separate single-turn probes. No dev task has yet been passed by the procedure run end to end. Required order: run the frozen procedure once on fake\_account\_2, confirm it reproduces the pass, commit this note, then choose the held-out tasks.

**Validation result (Oct 17).** Run end to end on fake\_account\_2 with fresh asks (`probes/policy_v3_fake_account_2.jsonl`, plan `audit/policy_v3_plan.json`, anchor 'most often'). All 15 asks (5 slots × 3 reps) labeled AMB. Generator conditions use the rep-1 answers, 3 generator reps each:

| Condition | Asks | Passed | Coins | Budget |
| --- | --- | --- | --- | --- |
| none | 0 | 0/3 | 7 | 14 (P = 6) |
| p6 | 3 | 3/3 | 13 | 14 (P = 6) |
| p6\_minus\_cardinality | 2 | 0/3 | 11 | 14 (P = 6) |
| p10 | 5 | 3/3 | 17 | 18 (P = 10) |
| p10\_minus\_cardinality | 4 | 0/3 | 15 | 18 (P = 10) |

The procedure reproduces the pass within budget at both patience values, and dropping the cardinality answer removes it at both; slots 4-5 do not compensate. Still one task, where the deciding fact also leaks alone via LOC, so this shows the procedure reproduces, not that it generalizes.

## Untested or weakly supported elements

Each row names the element, how much dev evidence it has, and the cheapest test that would settle it.

| Element | Support | Risk | Test |
| --- | --- | --- | --- |
| Frozen procedure end to end | Done on fake\_account\_2: 3/3 at P = 6 and P = 10, ablation 0/3 at both (see Validation result) | Only one task | Held-out runs |
| P = 10 allocation (k + 4 asks) | All dev evidence at P = 6 | Extra slots untested; no evidence they hurt either | First held-out runs at P = 10 |
| Rule 2 term selection | Passes all three dev tasks, but dev terms were annotation-informed | Merge step (3) is the fragile part; may over- or under-count | Held-out runs; compare picked terms against labels in the first encoder output |
| Nested-term detection (Rule 2 step 5) | Not exercised: solar's broad reply resolved down and System Unavailability with no follow-up | Reply may not name nested terms explicitly | A task with k > surface count where hooks alone do not pass |
| Standalone broad ask (procedure step 5) | One task (solar chain\_1, 3/3) whose gold is a single NULL row | Weakest element; a hook may do as well | Run solar hooks-only (filter, limit, columns) without chain\_1; not yet run as a condition |
| Merged exclusions + nulls slot | Untested; the two were one WHERE clause on fake only | Could return UNA or answer only one half | Run on fake\_account\_2, where each half discloses 3/3 alone, so a half-answer is detectable |
| Always hook | No case where hooking reduced disclosure; 45/45 AMB labels | None observed | Carry forward |
| Source table slot | 3/3 on sports, nothing on fake or solar | Low expected yield | Keep at priority 5 |
| Slot priority order | Fit to trials 4 and 5 | Over-fit to three tasks | Held-out pass rate by slot dropped |

## Per-task verdicts and open items

**fake\_account\_2** (k = 1, budget 14 at P = 6). Certificate candidate. budget\_min (hooked columns, limit, filter) passed 3/3 at 13 coins; dropping the limit hook fell to 0/3 at 11. The deciding fact is also reachable alone via LOC (2/3), so the hook made disclosure reliable rather than possible. Caveats: term chosen with knowledge of the annotation; slots chosen after seeing trials 4 and 5.

**solar\_panel\_1** (k = 3, budget 18 at P = 6). Honest pass but a weak test. amb\_only (one broad question) passed 3/3 at 8 coins; gold is a single NULL row. Two of the three labeled terms are nested and not in the question; the broad reply resolved them without follow-up.

**sports\_events\_19** (k = 2, budget 16 at P = 6). Fails under every condition run, including full at 22 coins. Withdrawn hypotheses: cluster naming (generated SQL already used the gold's three names) and Q2/Q3 fallback (generated SQL already falls back Q3 → Q2 → Q1). The expected gap is row filtering and pole-time computation. Two observations to check locally against gold:

- chain\_6 (how pole time is worked out) was in every failing condition. Either the answer was insufficient or the generator misapplied it.
- hook\_filter returned no races or drivers excluded. If the gap is in filtering, the decoder under-disclosed relative to gold's effective row set (for example gold dropping \\N or empty times through casting rather than an explicit WHERE). That would be a simulator finding, not a policy miss.

Open runs: amb\_only + chain\_4 (cheap, clean); budget\_min, full\_minus\_tables and full\_minus\_columns (not run, credits ran out); a diff of generated vs. gold on the WHERE and pole subquery (local, no rerun).

## Held-out run (Oct 17)

**Draw.** Query-category tasks only, excluding the seven dev and trial tasks and every database they come from (268 candidates, 15 databases). Seed 20261017: shuffle the databases, one task per database. First ten used; alternates in order: mental\_health\_14, cross\_border\_1, households\_7, archeology\_scan\_10, cybermarket\_pattern\_2. P = 10.

**Anchors fixed before running** (from `probes/policy_v3_heldout_all.jsonl`, commit a90e1c0):

| Task | k | Anchors |
| --- | --- | --- |
| hulushows\_14 | 4 | 'promo saturation ratio' dropped (exact KB name, step 2c); anchors pending under step 6 |
| robot\_fault\_prediction\_11 | 3 | 'failure rates', 'high reliability risk', 'high-wear' jobs |
| planets\_data\_14 | 3 | 'type of light filter' (nested terms pending) |
| exchange\_traded\_funds\_18 | 2 | 'value funds' (nested term pending) |
| crypto\_exchange\_19 | 3 | 'arbitrage opportunity', 'significant' (nested term pending) |
| insider\_trading\_5 | 2 | 'uniqueness' score, 'high-risk' |
| virtual\_idol\_9 | 1 | 'top fans' |
| disaster\_relief\_11 | 1 | 'overall efficiency' |
| reverse\_logistics\_1 | 2 | 'overall handling cost', 'major fee components' |
| cold\_chain\_pharma\_compliance\_20 | 0 | none (k = 0 path; 'lane risk potential' used only in an ungraded diagnostic ask) |

Other decisions recorded before running: the table slot uses schema-free wording ("which table or records should it be computed from?"); robot\_fault\_prediction\_11 leaves its 7th ask unspent.

**Generator handling.** In stage 1, insider\_trading\_5 and virtual\_idol\_9 had runs whose generated SQL was prose or cut off mid-query (8 of 48 rows). Those rows were classed as generator failures, not evidence. Both tasks were rerun, all conditions, with `reconstruct.py` resampling any generation that does not look like complete SQL before executing it (no execute coin). In the rerun, the rejected samples were about 640 completion tokens of prose, well under the token cap, so the cause is the model writing prose in place of SQL, not output truncation by the cap.

**Results so far** (3 generator reps each; coins at P = 10):

| Task | No answers | Frozen procedure | Notes |
| --- | --- | --- | --- |
| reverse\_logistics\_1 | 0/3 | 3/3, 18 of 20 | Stage 2: amb\_only 0/3; dropping slot 1, 2 or 5 gives 1/3, 2/3, 2/3; dropping 3, 4 or 6 gives 3/3 |
| insider\_trading\_5 | 0/3 (rerun) | 2/3, 19 of 20 (rerun) | Below the 3/3 rule for stage 2 |
| virtual\_idol\_9 | 0/3 (rerun) | 0/3, 16 of 18 (rerun) | Wrong result each time |
| disaster\_relief\_11 | 0/3 | 0/3, 16 of 18 | Gold joins through a table the answers never named |
| cold\_chain\_pharma\_compliance\_20 | 0/3 | 0/3, 14 to 15 of 16 | Output column missing; the columns ask came back UNA |
| hulushows\_14 | 3/3 | not run | Passes with no answers: an honest pass, not a certificate |
| robot\_fault\_prediction\_11 | 0/3 | 0/3, 18 of 22 | Invalid task: the gold SQL answers a different question from the public question and its annotations (same in the upstream ground-truth file). Replaced by the first alternate, mental\_health\_14; reported both ways |
| planets\_data\_14, exchange\_traded\_funds\_18, crypto\_exchange\_19 | 0/3 | pending phase B | |

## Separate finding: parser pass-through

The label parser (prompt\_utils.py:232-243) takes whatever sits between `<s>` and `</s>` with no check that it is one of the three labels. Two failure modes reach the decoder:

- Empty label: 3 of 45 trial-5 turns (about 7%). The decoder still answered in 2 of those 3.
- Unclosed label: 1 of 114 trial-4 labels. The whole encoder output, including reasoning that can quote gold SQL, is passed to the decoder as the label.

This accounts for the empty-label disclosures in the control rows (fake columns 1/3, solar plant-match, solar one-value). It is a low-rate simulator bug, not a policy lever, and should be reported separately from the hook finding. The policy does not depend on it.
