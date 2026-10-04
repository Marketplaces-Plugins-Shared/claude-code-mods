---
name: mods
description: Controls recording safeguards, file collisions, goals, usage and handoff. Use when managing the Codex adapter for Claude mods.
---


# Mods

Use the user's request as the task input.

---

## Step 1: Select the operation

Resolve `../../scripts/mods.py` relative to this SKILL.md. Use that absolute path as RUNTIME below. This package adapts the four Claude mods to Codex; read the installed package README for its capability limits before a safety claim.

```bash
python3 "$RUNTIME" status
python3 "$RUNTIME" rec on
python3 "$RUNTIME" rec off
python3 "$RUNTIME" guard status
python3 "$RUNTIME" board
```

Turn recording or the collision guard off only when the user requests it. `rec status` reports `screen_masking: false`. Recording safeguards block shell, read, image and external tools; they do not mask previously visible text, prompts or replies. While recording is on, avoid private information in replies and tool arguments. Only the exact control commands status, board, rec and guard are permitted through shell hooks. Goal changes must wait until recording is off.

---

## Step 2: Track a goal

Use the active session ID from the host. When the runtime cannot infer it, pass `--session ID` before the subcommand. Do not start a native autonomous goal unless the user asked for one. A mod task list alone is not a native goal.

```bash
python3 "$RUNTIME" goal set 'Ship the fix' --tasks '[{"title":"Reproduce","size":"S"},{"title":"Fix and check","size":"L"}]'
python3 "$RUNTIME" goal start 1
python3 "$RUNTIME" goal done 1
python3 "$RUNTIME" goal show
python3 "$RUNTIME" goal clear
```

S, M and L count as 1, 2 and 3. Mark done only after the work is checked. Completing the first task in this example yields 25 percent. The runtime prints a progress bar; it does not add a native screen bar or invent an ETA. Clear removes the plan. SessionEnd removes that session's plan and collision claims.

---

## Step 3: Read usage or hand off

`board` reports sessions seen by this adapter. `usage_status: unavailable` is missing evidence, not zero. `cached_input_tokens` measures past reuse; it does not prove a warm cache now. No price, TTL or cache keep-warm is available. Do not send background keep-warm requests.

For a handoff, give a concise chat summary with the user's objective, verified changes, exact source paths, checks, open PRs and issues, remaining work, and the next action. Separate merged, installed and tested states. Never clear or end the session automatically. Save to a file only if the user requests it.

---

## Evaluation Criteria

### Scorecard

| Criterion | Weight | Pass condition |
|---|---|---|
| Control commands | 35 | Uses the bundled runtime with the correct session and subcommand |
| Capability honesty | 35 | Never claims screen masking, TTL, prices or keep-warm |
| Goal evidence | 30 | Marks tasks done only after verification; reports measured tokens accurately |

### Anti-patterns

- Calling recording safeguards screen masking.
- Inventing a cache expiry or price.
- Marking unchecked work done.

### Scoring

Weights total 100. Pass requires 70 and no false safety claim.
