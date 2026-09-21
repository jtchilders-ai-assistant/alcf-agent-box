# Pepper 8-rank GPU task

Read `AGENTS.md` and `ENV.md` before acting. Treat environment entries as observations, not compatibility proof.

## Scientific goal

Use Pepper source revision `295050cfa38e465fd8c299749d39e995d22dfba1` to generate a Drell-Yan `ppee` sample at 13.6 TeV using a fixed random seed, with at least 20,000 accepted events. Execute the production simulation with exactly 8 MPI ranks across 2 allocated nodes and 4 A100 GPUs per node, one GPU per local rank.

You own dependency discovery and installation, configuration, compilation, tests, execution, and scientific analysis. Choose a coherent compiler, MPI, CUDA, and Kokkos stack based on evidence from this allocation. Do not change Pepper merely to compensate for an infrastructure defect. If a source or build-file change is scientifically necessary, preserve a complete patch and report the tree as dirty.

## Required evidence

Preserve exact source SHA and dirty-tree inventory, commands, versions, module observations, dependency origins and checksums, configure/build/test/run logs, application cache/configuration output, executable checksum and link audit, hostfile checksum, timing, requested and accepted event counts, and rank/host/GPU mapping.

Keep requested, detected, compiled/linked, and runtime evidence distinct. Contradictory evidence blocks success. Do not fabricate, interpolate, or reconstruct missing scientific output.

## Required outputs

Preserve nonempty raw event output. Derive all analysis from those actual event records and produce labeled PNG figures plus matching CSV or JSON histogram tables for:

1. dilepton invariant mass in GeV;
2. leading-lepton transverse momentum in GeV;
3. dilepton rapidity.

Record cuts, bin edges, counts or weights, units, source filename and checksum, event count, and sanity checks in `analysis/metrics.json`.

## Completion

Update `STATUS.json` at each phase checkpoint. Maintain a ten-minute finalization reserve: once it begins, start no new build or simulation.

Before exit, write `REPORT.md` and `RESULT.json`, then exactly one marker: `DONE` only if every acceptance requirement passed; otherwise `FAILED` with the first unresolved failure and the next reproducible action. `RESULT.json` must include build MPI/CUDA evidence, test status, simulation exit code and accepted-event count, rank/host/GPU evidence, raw-data path and checksum, and plot/table paths.
