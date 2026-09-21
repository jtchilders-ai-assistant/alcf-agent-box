#!/usr/bin/env bash
set -euo pipefail

local_rank="${PALS_LOCAL_RANKID:-${MPI_LOCALRANKID:-${OMPI_COMM_WORLD_LOCAL_RANK:-}}}"
world_rank="${PALS_RANKID:-${PMI_RANK:-${OMPI_COMM_WORLD_RANK:-}}}"
if [ -z "$local_rank" ] || [ -z "$world_rank" ]; then
  printf 'missing rank environment\n' >&2
  exit 2
fi

gpus_per_node="${RED_SHIRT_GPUS_PER_NODE:-4}"
case "$gpus_per_node" in
  ''|*[!0-9]*|0) printf 'GPUS_PER_NODE must be a positive integer\n' >&2; exit 2 ;;
esac
export CUDA_VISIBLE_DEVICES="$((local_rank % gpus_per_node))"
uuid="$(nvidia-smi --query-gpu=uuid --format=csv,noheader -i "$CUDA_VISIBLE_DEVICES" | tr -d '[:space:]')"
printf 'GPU_MAP rank=%s local_rank=%s host=%s cuda_visible=%s uuid=%s\n' \
  "$world_rank" "$local_rank" "$(hostname)" "$CUDA_VISIBLE_DEVICES" "$uuid"
exec "$@"
