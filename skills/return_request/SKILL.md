---
id: return_request
version: v0
---

# Return-request workflow

- Gather the order identifier and the user's verification code when they are missing.
- Retrieve the applicable return policy and the order or return-eligibility result before deciding.
- Apply the tool results and the policy to the request; if required information is missing or conflicting, ask a concise question or stop without writing.
- For an eligible return, explain the specific target and the relevant conditions, then ask for explicit confirmation of that exact operation.
- Submit only after the user confirms the current operation. If the user declines, changes the target, or changes a material parameter, do not write.
- After submission, report the tool result accurately, including an idempotent replay; never claim a state change that the tool did not make.
