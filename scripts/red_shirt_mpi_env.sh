#!/bin/bash
# Source this file from a Polaris PBS job before entering Apptainer.
# It resolves host modules and exports the host MPI bridge contract.

if [ -z "${PBS_NODEFILE:-}" ] || [ ! -f "$PBS_NODEFILE" ] || [ ! -r "$PBS_NODEFILE" ]; then
  printf 'ERROR: PBS_NODEFILE is not a readable regular file: %s\n' "${PBS_NODEFILE:-<unset>}" >&2
  return 1 2>/dev/null || exit 1
fi

ml use /soft/modulefiles
ml spack-pe-base
ml apptainer
ml cray-mpich-abi

HOST_MPIEXEC="$(command -v mpiexec)" || {
  printf 'ERROR: mpiexec is unavailable after loading cray-mpich-abi\n' >&2
  return 1 2>/dev/null || exit 1
}
HOST_APPTAINER="$(command -v apptainer)" || {
  printf 'ERROR: apptainer is unavailable after module loading\n' >&2
  return 1 2>/dev/null || exit 1
}
if [ -z "${CRAY_LD_LIBRARY_PATH:-}" ]; then
  printf 'ERROR: CRAY_LD_LIBRARY_PATH is empty after loading cray-mpich-abi\n' >&2
  return 1 2>/dev/null || exit 1
fi

PALS_RUNTIME_DIR=""
for candidate in /var/run/palsd /run/palsd; do
  if [ -d "$candidate" ]; then
    PALS_RUNTIME_DIR="$candidate"
    break
  fi
done
if [ -z "$PALS_RUNTIME_DIR" ]; then
  printf 'ERROR: neither /run/palsd nor /var/run/palsd exists\n' >&2
  return 1 2>/dev/null || exit 1
fi

HOST_MPI_PATH="$PATH"
HOST_MPI_LD_LIBRARY_PATH="${CRAY_LD_LIBRARY_PATH}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export HOST_MPIEXEC HOST_APPTAINER PALS_RUNTIME_DIR HOST_MPI_PATH HOST_MPI_LD_LIBRARY_PATH
