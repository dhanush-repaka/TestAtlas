# Review feedback, conflict explanations and initial setup

2 November 2026

## User-reported findings

The inventory case's two disagreements were already stored, but the UI left feedback at the bottom of the case and gave little indication of a changed review state. AI findings must remain separate from human disagreement: disagreeing does not imply the opposite assessment is true.

The reported release finding cited an acceptance criterion saying release increases availability, then merely repeated it as a contradiction. That did not demonstrate opposing expectations. The full case had another problem: it initialized empty inventory, reserved 4, released 2 and reserved 1, yet expected 1 remaining. The inspected implementation gives 0 - 4 + 2 - 1 = -3. Release still increased availability by 2. The story also separately prohibits reserving more than available; that is a different criterion and needs its own citation and comparison.

The assessor's prompt had omitted description and preconditions. Those are now supplied, with explicit instructions to trace state and separate requirement alignment from implementation behaviour. This improves evidence presented to the model; it is not a semantic correctness guarantee.

## Changes

- Reviews return a separate review_state. The list and expanded case immediately show Disputed by reviewer; a Disputed reviews filter and matching exports use that state. AI status is preserved. Reviews predating a newer assessment are identified as belonging to an earlier assessment.
- New contradicted findings require exact left/right excerpts grounded in the actual test/story/source and an explicit difference. Missing, fabricated or identical excerpts downgrade the finding to unknown. Existing saved findings without a recorded contrast remain historical and display a warning that the conflict was not explained. No user assessment was silently rewritten.
- Code, requirement, document and journey generators share a mandatory setup object: specified starting point, initial state and action/expected setup steps. The server prepends validated setup steps. UI prompts require evidenced entry/login/navigation; API/library prompts require initialization and explicit fixtures. Missing or ambiguous setup is rejected, rather than inventing routes. The validation checks structure; humans must still verify whether navigation and setup accurately reflect the product.
- Login itself stays in scenario steps when authentication is what is being tested. Setup does not execute the behaviour under test, inflate behavioural-depth checks or inherit journey requirement statuses.
- Module regeneration preserves the existing suite when no valid cases are returned. Existing cases are not rewritten automatically; the new setup rule applies to new generation.

## Validation

522 Python tests pass, JavaScript syntax checks pass, presentation tests include four new review-state assertions, and dashboard trend checks pass. Regressions cover saved unknown/disputed states, old reviews, the inventory arithmetic, unexplained and unsupported conflicts, setup/navigation order, non-UI setup, missing setup, shared prompts, behavioural-depth accounting, and preserving existing cases after rejected generation.

Browser testing used disposable data and controlled findings, with real review routes and storage. Disagree visibly updates both the row and expanded card, disputed filtering selects the case, legacy conflict warnings appear, setup steps precede scenario steps, and the mobile page width equals its 390px viewport. No paid AI call was made on real user cases. Existing real user review records and case content were preserved.

The original real-project developer/QA semantic accuracy evaluation remains pending. Exact quotes and mandatory setup do not prove that the model understood a contradiction or navigation flow correctly.
