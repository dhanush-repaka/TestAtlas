# TestAtlas assessment combinations

Order: Test ↔ requirements, Test ↔ code, Code ↔ requirements. S = supported, C = contradicted, U = unknown. These are source assessment results, not test execution results.

Every combination has an explicit label and recommended action. Validated conflicts remain visible when another comparison is unknown. Combined diagnoses require the same acceptance criterion. Earlier unexplained contradiction claims require reassessment and are not upgraded into validated conflicts.

| Combination | Display | Next step |
|---|---|---|
| SSS | All three comparisons align | Run the test to validate runtime behaviour; source alignment alone is not execution proof. |
| SSC | Implementation conflict · comparisons need review | The code conflicts with requirements although the test is reported to match both. Check the exact behaviour and comparison scope; preserve the implementation conflict. |
| SSU | Test aligns · implementation evidence incomplete | Test alignment is supported, but direct code-to-requirement evidence is missing. Inspect that comparison before drawing an overall conclusion. |
| SCS | Test and code conflict · comparisons need review | The test and code conflict although both are reported to align with requirements. Review scenario, state and comparison scope; preserve the test-to-code conflict. |
| SCC | Implementation conflicts with the expected behaviour | The test matches requirements; code conflicts with both. Investigate the implementation and preserve the required expectation. |
| SCU | Required test expectation conflicts with code | The test matches requirements and conflicts with code. Investigate that mismatch; direct implementation-to-requirement evidence remains unresolved. |
| SUS | Requirements align · test-to-code evidence missing | Both test and code align with the cited requirement, but their direct comparison is unresolved. Assess whether code produces the test expectation. |
| SUC | Implementation conflicts with requirements | The test matches requirements and code conflicts with them. Investigate the implementation; the direct test-to-code comparison remains unresolved. |
| SUU | Test matches requirements · code assessment incomplete | Retain the required expectation. Gather evidence for test-to-code and code-to-requirement comparisons. |
| CSS | Test requirement conflict · comparisons need review | The test conflicts with requirements although code is reported to match both. Review the exact expectations and comparison scope; preserve the test requirement conflict. |
| CSC | Code and test conflict with requirements | The test matches current code, but both conflict with the requirement. Review the implementation and test expectation together. |
| CSU | Test matches code but conflicts with requirements | Review the test expectation against requirements. Direct code-to-requirement evidence is unresolved; do not assume its result. |
| CCS | Test expectation conflicts with code and requirements | Code matches requirements, while the test conflicts with both. Review a test expectation update against the cited requirement. |
| CCC | Conflicts found in all three comparisons | Review all three opposing expectations. Multiple conflicts do not require clarification by themselves; establish the intended behaviour and address each mismatch. |
| CCU | Test conflicts with requirements and code | Review both test mismatches. Code-to-requirement alignment is unresolved, so do not assume whether code or test should change. |
| CUS | Test conflicts with requirements · code aligns | Review the test requirement conflict. Assess the missing direct test-to-code comparison before attributing that mismatch. |
| CUC | Test and code each conflict with requirements | Review both requirement conflicts independently. Their direct relationship remains unresolved; neither conflict is cancelled by that gap. |
| CUU | Test conflicts with requirements · code evidence missing | Review the test expectation against requirements. Obtain code evidence separately; its absence does not remove the test requirement conflict. |
| USS | Code aligns · direct test requirement evidence missing | Code matches requirements and the test matches code. Assess the test directly against requirements rather than inferring coverage through code. |
| USC | Code conflicts with requirements · test matches code | Investigate the implementation conflict. Assess the test directly against requirements before deciding how its expectation should change. |
| USU | Test matches code · requirement evidence incomplete | Current behaviour matches the test, but its business correctness is unresolved. Gather requirement evidence for both comparisons. |
| UCS | Test conflicts with code · code matches requirements | Review the test-to-code mismatch. Assess the test directly against requirements before deciding whether its expectation is wrong. |
| UCC | Code conflicts with test and requirements | Investigate both implementation mismatches. Direct test-to-requirement evidence remains unresolved; do not assume the test is correct. |
| UCU | Test conflicts with code · requirements unresolved | Investigate the observed expectation mismatch. Gather requirements before deciding which behaviour is intended. |
| UUS | Code matches requirements · test assessment incomplete | Assess the test against requirements and code. Code alignment alone does not establish test sufficiency. |
| UUC | Code conflicts with requirements · test assessment incomplete | Investigate the implementation conflict. Assess the test separately; missing test evidence does not remove the code requirement conflict. |
| UUU | Insufficient evidence in all three comparisons | Gather the required story, code and scenario evidence identified below. No comparison currently supports a definitive conclusion. |

Saved assessments receive updated presentation without rewriting their original findings, test content, or review history.
