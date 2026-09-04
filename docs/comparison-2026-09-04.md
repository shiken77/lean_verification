# Exploratory comparison — 4 September 2026

The corrected benchmark wrapper accepts the `clamp` reference proof. Both experiments below retain the original mathematical contracts; they do not claim that the interval-membership theorem specifies all clamp behavior.

Requested model: `deepseek-chat`. The API actually reported `deepseek-v4-flash`. Temperature: 0.2. One independent trajectory per task and mode, rotating mode order by case, with three workers. Each task had a maximum of 20 submissions and an early stop after three identical source/error outcomes. Diagnosis calls are additional calls, not additional submissions.

## After adding verified environment information

Report: `.runs/comparison-20260904T041532611920Z.json` (local, Git-ignored). Every mode received the same environment facts, including the need to use `theorem`, not `lemma`, with this core-only Lean setup.

| Mode | Normal tasks passed | Submissions, including failures | API calls | Total tokens |
|---|---:|---:|---:|---:|
| A — raw errors | 1/5 | 18 | 18 | 13,489 |
| B — diagnosis and history | 4/5 | 15 | 25 | 41,045 |
| C — checkable steps and diagnosis | 5/5 | 34 | 47 | 88,541 |

The ten fixed controls passed (10/10), checked once independently of the three model modes. Counting these shared controls gives 11/15, 14/15, and 15/15 respectively; **the informative model comparison is 1/5, 4/5, and 5/5**.

| Normal task | A | B | C |
|---|---|---|---|
| Add one | PASS on 1 | PASS on 1 | PASS on 3 |
| Maximum | Stopped after 4 | PASS on 3 | PASS on 8 |
| Minimum | Stopped after 4 | Stopped after 6 | PASS on 7 |
| Absolute value | Stopped after 4 | PASS on 2 | PASS on 10 |
| Clamp | Stopped after 5 | PASS on 3 | PASS on 6 |

All “Stopped” entries above stopped due to repeated identical source/error outcomes, not success and not the 20-submission cap. C counts successfully checked intermediate submissions, so a larger submission count is not necessarily more failed repairs.

## Earlier pilot, retained rather than discarded

Report: `.runs/comparison-20260904T041217687281Z.json`. This run preceded the explicit core-only helper-declaration guidance. Some generated `lemma` declarations failed to parse; the diagnostic model sometimes incorrectly blamed whitespace. A local test confirmed that the same trivial declaration fails with `lemma` and succeeds with `theorem` in the installed environment.

| Mode | Normal tasks passed | Submissions | API calls | Total tokens |
|---|---:|---:|---:|---:|
| A | 2/5 | 13 | 13 | 8,813 |
| B | 5/5 | 12 | 19 | 18,717 |
| C | 3/5 | 35 | 58 | 78,342 |

Total usage across the two development experiments: **180 model calls and 248,947 reported tokens**. The two runs used different prompt versions and must not be pooled as repeated measurements of an identical system. This usage excludes earlier work outside these two experiments.

## Interpretation and limits

- In the later run, diagnosis improved observed completion compared with raw errors; it did not reduce API calls or token cost.
- Staged execution completed all five tasks in that run but was the most expensive option. These simple tasks do not automatically benefit from forced decomposition.
- There was only one trajectory per task and mode per version, with independent first answers. This is an engineering smoke comparison, not a statistically reliable estimate of a causal effect or evidence of generalization.
- The same small benchmark informed the environment fix. A research claim needs independent repetitions and held-out tasks that were not used to tune prompts.
- Full prompts, responses, failures, per-call usage, source fingerprints and exact contracts are retained locally. No API key is included in the reports.
