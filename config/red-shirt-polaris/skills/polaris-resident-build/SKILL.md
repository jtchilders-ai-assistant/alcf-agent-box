---
name: polaris-resident-build
description: Use when building scientific software on Polaris.
version: 0.1.0
author: Taylor Childers (jtchil0), Hermes Agent
license: Apache-2.0
platforms: [linux]
metadata:
  hermes:
    tags: [polaris, build, cmake, cuda, kokkos]
    related_skills: []
---

# Polaris Resident Build

Use the exposed host bridge to discover and exercise a coherent toolchain while
keeping application dependency choices and build work under resident control.

## When to Use

- Configuring or building CMake/C++, MPI, CUDA, or Kokkos software on Polaris.
- A module is visible but compatibility with the complete application is unknown.
- A requested CMake option may differ from what configuration actually detected.

## Procedure

1. Read `AGENTS.md` and `ENV.md`. Treat environment entries as observations,
   never compatibility proof.
2. Preserve the clean source revision and complete dirty-tree state before work.
   Install dependencies only below the declared task root.
3. Use the declared bridge action for host work. Do not source host library paths
   globally into Hermes; keep them inside the bridged build or rank process.
4. Probe one candidate stack through increasing evidence levels:
   - compiler and language feature;
   - MPI compile/link and rank launch;
   - CUDA runtime resolution;
   - required Kokkos options and architecture;
   - minimal Kokkos CUDA execution;
   - application configure, build, tests, then execution.
5. Preserve the first failing diagnostic before changing one variable. Update
   `STATUS.json` after every phase.
6. Patch application sources only for an application defect supported by evidence.
   Never patch around an unproven host/container boundary. Preserve the full diff.

## Evidence Rules

Keep these distinct:

- **Requested:** command-line options supplied.
- **Detected:** configure/cache output confirms the feature.
- **Compiled/linked:** target and link audit prove the selected libraries.
- **Executed:** production-equivalent run exits successfully with expected output.

Finding `KokkosConfig.cmake` or passing `-DKokkos_ENABLE_CUDA=ON` proves neither
CUDA enablement nor application viability.

## Time Pressure

Reserve the final ten minutes for evidence and terminal artifacts. If the full
acceptance ladder cannot finish, stop and write `FAILED` with completed stages,
the first unresolved error, logs, source status, and a precise next action.

## Pitfalls

- Do not guess module versions from prior Polaris configurations.
- Do not search all of `/soft` when `ENV.md` supplies candidate paths.
- Do not use a successful configure as proof of a successful build or run.
- Do not hide a dirty tree behind a commit or stash; record it as run provenance.

## Verification

A successful build claim requires retained configure/build/test logs, exact
source state, executable checksum and link audit, required cache settings, and a
successful production-equivalent execution. Missing any required layer means
partial progress, not success.
