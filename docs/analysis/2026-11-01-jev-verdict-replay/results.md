# jev verdict on earlier debates (2026-11-01)

English | [한국어](results.ko.md)

> Point-in-time record from 2026-11-01; the code may change after it.

Design: `../../design/jev-verdict.md` (J2, run on earlier data before shadow records exist). Scripts: `jev_replay.py`
(question set 1, the deployed `jev_verdict.py`) and `jev_replay_v2.py` (question set 2), run on the hub with the
release's code. Per-turn rows (no debate text): `rows-q1.json`, `rows-q2.json`.

| Item | Value |
|---|---|
| Data | Every LLM judge turn in the hub's debate jobs, 2026-10-03 to 2026-11-01, without canned answers: 60 turns, 52 approved and 8 rejected by the LLM judge |
| Material | The judge material cut from the saved judge prompt (39 turns), else rebuilt from the job's transcript (21, mostly early October); median 13 800 characters, 2 cut at 24 000 |
| Model | `jev-1.14.0` (`jev-latest`), System One API, about 0.2 s per call, no errors |
| Reading | The LLM judge is a reference, not ground truth: a disagreement is not by itself a jev mistake |

## Question set 1 (the deployed J1 question)

"Should this proposal be accepted as the final decision? Yes: every blocking problem the critique raises is answered."

| | LLM approve (52) | LLM reject (8) |
|---|---|---|
| jev approve (p ≥ 0.8) | 0 | 0 |
| jev reject (p ≤ 0.2) | 48 | 8 |
| uncertain | 4 | 0 |

Median `p_approve` 0.06 (LLM approved) and 0.05 (rejected); ranking AUC 0.68. jev rejected nearly everything: in a
round the critique comes after the proposal, so the proposal never answers it, and jev answered exactly that
(`p_blocking` median 0.91 even where the LLM approved). The question did not fit the debate.

## Question set 2 (written for the debate's structure)

Asked in one request so the phrasings compare on the same material:

- `decide_now`: can the judge write the final decision now, adopting what is valid in the critique (yes: a sound base,
  the critique can be settled inside the decision; no: wrong at its core, so redraft first)?
- `fundamental`: does the critique show a flaw that cannot be fixed within a decision?
- `next_step`: choice between approve and redraft.

| Question | Median, LLM approve / reject | AUC |
|---|---|---|
| `decide_now` | 0.74 / 0.66 | 0.69 |
| `fundamental` (lower means approve) | 0.38 / 0.54 | 0.77 |
| `next_step` (approve probability) | 0.43 / 0.41 | 0.54 |
| evidence score | 1.69 / 1.25 | 0.67 |

`decide_now` with the design's thresholds 0.8 / 0.2:

| | LLM approve (52) | LLM reject (8) |
|---|---|---|
| jev approve | 13 | 0 |
| jev reject | 3 | 0 |
| uncertain (-> LLM judge) | 36 | 8 |

At 0.7 / 0.3: 34 approvals (2 where the LLM rejected), 4 rejections (1 where the LLM rejected), 22 uncertain.
`decide_now` does not separate the jev gate's later `PASSED` from `DISPUTED` (AUC 0.41).

## Reading it

- **No false approval at 0.8.** The 13 rounds jev approved surely were all approved by the LLM; it said "unsure" rather
  than guess.
- **It does not reject.** None of the 8 LLM rejections was a sure jev rejection. Lower thresholds buy coverage with
  wrong approvals.
- **Little to save.** Under design A an LLM judge turn is saved only when jev rejects (an approval still needs the
  writer), so with these numbers A would rarely save a call.
- **Limits.** 8 rejections only, 5 of them rebuilt from early-October transcripts; question set 2 was written and
  measured on the same data, which flatters it.

## Decision

The shadow mode switches to question set 2 (`decide_now` as the deciding `approve`, `fundamental`, evidence; records
carry `questions: 2`, the report counts only those). J3 is not started on this evidence: J2 is judged on new shadow
records from production debates.
