---
name: scientific-evidence-contract
description: Use when a scientific task must support its claims.
version: 0.1.0
author: Taylor Childers (jtchil0), Hermes Agent
license: Apache-2.0
platforms: [linux]
metadata:
  hermes:
    tags: [science, evidence, provenance, validation]
    related_skills: []
---

# Scientific Evidence Contract

A scientific result is valid only when retained evidence supports every claimed
stage. Pressure, compute cost, and partial success do not lower this standard.

## When to Use

- Building, testing, simulating, analyzing, or reporting scientific software.
- Logs disagree, raw data is missing, or the source tree is dirty.
- A stakeholder requests conclusions before validation is complete.

## Evidence to Preserve

- Exact source revision and the complete dirty tree (`git status` plus diff).
- Exact commands, environment/profile identifier, start/end times, exit codes,
  and unfiltered stdout/stderr paths.
- Configure/cache evidence, build/test logs, executable checksum and link audit.
- Rank, host, GPU mapping, requested versus accepted work, raw output paths and
  checksums, and analysis inputs/parameters/outputs.

Do not stash and do not commit a dirty tree merely to make provenance appear
clean. Record the actual state that produced the run. Never alter prior raw
logs or replace missing raw output with reconstructed or synthetic data.

## Decision Rules

- Contradictory evidence blocks success. A successful coordinator line does not
  override a fatal rank error.
- Missing raw output blocks any numerical claim that depends on it.
- Preserve the first unresolved failure even if later commands partly succeed.
- Separate application success, infrastructure success, and communication
  success. One does not imply another.
- A plot is evidence only when its source file, checksum, event count, cuts,
  bins, units, and transformation are retained.

## Terminal Artifacts

Update `STATUS.json` after each phase. Before exit, write `REPORT.md` and
`RESULT.json`, then exactly one marker: `DONE` only when every acceptance gate
passes, otherwise `FAILED`.

If the outer wrapper must recover from model or process failure, its artifacts
must say `wrapper-generated`; they must not overwrite resident-generated
artifacts or present inferred science as observed output.

## Reporting Under Pressure

Report the run as failed or incomplete, state the conflicting facts, and name
the evidence needed to resolve them. Do not promise a success summary first and
qualify it later. Do not provide numerical values unless retained raw output
supports them.

## Pitfalls

- A process exit zero does not prove every rank completed.
- One log line does not prove global event acceptance.
- A dirty tree is provenance, not something to hide.
- Reconstructed plots cannot repair missing source data.

## Verification

Before `DONE`, read every required artifact back, validate its format, compare
its checksums and counts to raw evidence, confirm exactly one terminal marker,
and resolve every contradiction. Any unresolved discrepancy requires `FAILED`.
