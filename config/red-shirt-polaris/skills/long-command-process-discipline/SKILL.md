---
name: long-command-process-discipline
description: Use when commands may outlive one tool call.
version: 0.1.0
author: Taylor Childers (jtchil0), Hermes Agent
license: Apache-2.0
platforms: [linux]
metadata:
  hermes:
    tags: [processes, timeout, polling, recovery]
    related_skills: []
---

# Long Command Process Discipline

Long builds and simulations are single operations whose identity and terminal
result must survive tool-call boundaries. Duplicate execution is a correctness
failure, not a harmless retry.

## When to Use

- A build, test, simulation, transfer, or analysis may run longer than one call.
- A tool reports that it promoted or started a background process.
- Walltime is low and a process has not yet produced a terminal result.

## Rules

- Terminal timeouts are seconds.
- A launch acknowledgement proves only that work started.
- Record the exact process handle, command, start time, log paths, and expected
  completion evidence immediately after launch.
- Poll or wait on that exact process handle until it exits. Never rerun the
  command because output is delayed or the remaining time is short.
- Record the real exit code and preserve stdout/stderr before interpreting the
  outcome.
- Do not use process-name searches as ownership evidence and do not signal a
  PID that was not obtained from the launch operation.

## Procedure

1. Estimate whether the command fits in the remaining walltime and finalization
   reserve. Prefer foreground execution only when it fits the tool's foreground
   limit.
2. If background execution is required, launch once and immediately persist its
   handle and evidence paths in `STATUS.json`.
3. Poll or wait using the process-management interface. If the handle is absent,
   inspect durable logs and terminal markers; do not infer success or failure.
4. When the process exits, record its exit code and verify its artifacts before
   moving to the next phase.
5. If five minutes remain without a terminal result, stop starting work. Write
   honest terminal artifacts describing the still-running or indeterminate
   operation and the evidence needed to resume safely.

## Recovery

After transport or model failure, recover from the persisted handle, scheduler
record, logs, and markers. An indeterminate acknowledgement requires inspection,
not retry. If ownership cannot be established, report the command as
indeterminate and do not create a duplicate.

## Pitfalls

- Lowering a second timeout does not make an existing process finish faster.
- A successful launcher exit does not prove the child succeeded.
- Piping a decisive command can replace its exit code with the consumer's.
- An empty process list does not prove a prior process succeeded.

## Verification

Completion requires the original command's terminal exit code, retained raw
logs, and verified expected artifacts. Until those exist, claim only that the
command was launched or that its state is indeterminate.
