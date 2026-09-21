---
name: polaris-mpi-apptainer
description: Use when launching MPI ranks through Apptainer on Polaris.
version: 0.1.0
author: Taylor Childers (jtchil0), Hermes Agent
license: Apache-2.0
platforms: [linux]
metadata:
  hermes:
    tags: [polaris, mpi, apptainer, pals, gpu]
    related_skills: []
---

# Polaris MPI Through Apptainer

Use the allocation's host PALS/Cray MPI launcher to start one containerized
application process per rank. A container-local `mpiexec` or one-rank smoke is
not proof of multi-node capability.

## When to Use

- Running containerized MPI across more than one Polaris node.
- Mapping ranks to A100 GPUs.
- Diagnosing a native-MPI-pass/container-MPI-fail boundary.

## Required Launch Boundary

Start from host `mpiexec --no-transfer` with the exact `$PBS_NODEFILE`, then
invoke `apptainer exec` for each rank. Use only the bridge/profile documented in
`ENV.md`; do not reconstruct versioned Cray paths from memory.

The production interface must preserve rank-local PALS and PMI state, mount the
discovered writable PALS runtime directory, and expose required Cray, NVIDIA,
libfabric, and CXI libraries without replacing the container's system libc.
Do not add `--cleanenv` when the accepted profile requires inherited rank state.

## Acceptance Ladder

1. Verify the preserved nodefile checksum and exactly two allocated hosts.
2. Compile and link a native MPI hello-world; retain a link audit.
3. Run a two-rank native test with one rank per node.
4. Run the same two-rank executable through the host-to-Apptainer boundary.
5. Run an eight-rank GPU probe with four ranks per node.
6. Require global ranks 0–7, local rank 0–3 once per host, and eight distinct GPU UUIDs.
7. Only after those gates pass, launch the application through the same path.

## GPU Assignment

Use the accepted rank wrapper to map the scheduler-provided local rank to one
visible GPU. Record global rank, local rank, short hostname,
`CUDA_VISIBLE_DEVICES`, and GPU UUID before application startup. Do not infer
mapping from rank number alone.

## Failure Localization

- Native MPI failure: repair host compiler/module/PALS setup.
- Native pass but container two-rank failure: repair ABI, binds, library paths,
  or PMI/PALS propagation.
- Two-rank pass but eight-rank mapping failure: repair placement or rank wrapper.
- Bridge pass but application failure: preserve evidence and debug the application.

Never patch application sources to compensate for an unproven launcher boundary.

## Pitfalls

- Container-local `mpiexec` being present does not make it scheduler-aware.
- A nodefile line count may reflect slots; independently verify distinct hosts.
- A visible GPU count does not prove ranks use distinct devices.
- Read-only PALS runtime binds can fail when rank startup requires writable state.

## Verification

Retain the exact launch command, nodefile and checksum, module/profile record,
link audit, stdout/stderr, exit status, and per-rank host/GPU map. Claim
multi-node GPU success only when all expected ranks ran on exactly the allocated
hosts with distinct devices and the application exited successfully.
