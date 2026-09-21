#!/usr/bin/env bash
set -euo pipefail
umask 077

stage="${1:-}"
case "$stage" in
  cxx20_concepts|mpi_native_two_rank|mpi_gtl_link_resolution|cuda_runtime_compatibility|kokkos_required_features|kokkos_cuda_production_rank|pepper_configure_features) ;;
  *) printf 'unsupported toolchain stage: %s\n' "$stage" >&2; exit 64 ;;
esac

WORK="${RED_SHIRT_PROBE_WORKDIR:?RED_SHIRT_PROBE_WORKDIR is required}"
PROFILE="${RED_SHIRT_ENV_PROFILE_ID:?RED_SHIRT_ENV_PROFILE_ID is required}"
mkdir -p "$WORK/$stage"
cd "$WORK/$stage"
printf 'environment_profile_id=%s\nstage=%s\n' "$PROFILE" "$stage"

require_file() { [ -f "$1" ] || { printf 'required file is absent: %s\n' "$1" >&2; exit 66; }; }
require_exec() { [ -x "$1" ] || { printf 'required executable is absent: %s\n' "$1" >&2; exit 66; }; }
reject_unresolved() {
  if grep -E 'not found|undefined symbol' "$1"; then
    printf 'unresolved dynamic dependency detected\n' >&2
    exit 1
  fi
}

case "$stage" in
  cxx20_concepts)
    CXX="${RED_SHIRT_CXX:?RED_SHIRT_CXX is required}"
    cat >concepts.cpp <<'EOF'
#include <concepts>
template<class T> concept Integral = std::integral<T>;
static_assert(Integral<int>);
int main() { return 0; }
EOF
    "$CXX" --version
    "$CXX" -std=c++20 concepts.cpp -o concepts
    ./concepts
    ;;

  mpi_native_two_rank)
    MPICXX="${RED_SHIRT_MPICXX:?RED_SHIRT_MPICXX is required}"
    MPIEXEC="${RED_SHIRT_MPIEXEC:?RED_SHIRT_MPIEXEC is required}"
    NODEFILE="${RED_SHIRT_PBS_NODEFILE:?RED_SHIRT_PBS_NODEFILE is required}"
    require_file "$NODEFILE"
    cat >mpi.cpp <<'EOF'
#include <mpi.h>
#include <cstdio>
int main(int argc, char **argv) {
  MPI_Init(&argc, &argv); int rank = -1, size = -1;
  MPI_Comm_rank(MPI_COMM_WORLD, &rank); MPI_Comm_size(MPI_COMM_WORLD, &size);
  std::printf("MPI_PROBE rank=%d size=%d\n", rank, size);
  MPI_Finalize(); return size == 2 ? 0 : 3;
}
EOF
    "$MPICXX" -std=c++20 mpi.cpp -o mpi_probe
    "$MPIEXEC" --hostfile "$NODEFILE" -n 2 ./mpi_probe | tee mpi-run.log
    python3 - <<'PY'
import re
text=open('mpi-run.log').read()
records=re.findall(r'MPI_PROBE rank=(\d+) size=(\d+)', text)
assert {int(r) for r,s in records} == {0,1}, records
assert {int(s) for r,s in records} == {2}, records
PY
    ;;

  mpi_gtl_link_resolution)
    MPICXX="${RED_SHIRT_MPICXX:?RED_SHIRT_MPICXX is required}"
    cat >mpi-link.cpp <<'EOF'
#include <mpi.h>
int main(int argc, char **argv) { MPI_Init(&argc, &argv); MPI_Finalize(); }
EOF
    "$MPICXX" -std=c++20 mpi-link.cpp -o mpi_link_probe
    ldd mpi_link_probe | tee mpi-link.ldd
    reject_unresolved mpi-link.ldd
    grep -Eiq 'lib(mpich|mpi)' mpi-link.ldd
    grep -Eiq 'lib(.*gtl|mpi_gtl)' mpi-link.ldd
    ;;

  cuda_runtime_compatibility)
    NVCC="${RED_SHIRT_NVCC:?RED_SHIRT_NVCC is required}"
    MPI_LINK_PROBE="${RED_SHIRT_MPI_LINK_PROBE:?RED_SHIRT_MPI_LINK_PROBE is required}"
    require_exec "$MPI_LINK_PROBE"
    "$NVCC" --version
    ldd "$MPI_LINK_PROBE" | tee mpi-cuda.ldd
    reject_unresolved mpi-cuda.ldd
    grep -Eiq 'libcudart|libcuda' mpi-cuda.ldd
    python3 - "$MPI_LINK_PROBE" <<'PY'
import pathlib, re, subprocess, sys
seen=set(); pending=[pathlib.Path(sys.argv[1])]
records=[]
while pending:
    item=pending.pop()
    key=str(item.resolve())
    if key in seen: continue
    seen.add(key)
    out=subprocess.run(['ldd', str(item)], check=True, text=True, capture_output=True).stdout
    records.append(f'## {item}\n{out}')
    if 'not found' in out: raise SystemExit(f'unresolved dependency beneath {item}')
    for line in out.splitlines():
        match=re.search(r'=>\s+(/\S+)', line)
        if match and ('mpi' in line.lower() or 'gtl' in line.lower()): pending.append(pathlib.Path(match.group(1)))
text='\n'.join(records)
pathlib.Path('recursive-mpi-cuda.ldd').write_text(text)
if not re.search(r'libcudart\.so|libcuda\.so', text): raise SystemExit('MPI/GTL closure has no resolved CUDA runtime')
PY
    ;;

  kokkos_required_features)
    KOKKOS_PREFIX="${RED_SHIRT_KOKKOS_PREFIX:?RED_SHIRT_KOKKOS_PREFIX is required}"
    CMAKE="${RED_SHIRT_CMAKE:-cmake}"
    cat >CMakeLists.txt <<'EOF'
cmake_minimum_required(VERSION 3.20)
project(kokkos_features LANGUAGES CXX CUDA)
find_package(Kokkos CONFIG REQUIRED)
string(TOUPPER "${Kokkos_DEVICES}" _devices)
string(TOUPPER "${Kokkos_ARCH}" _arch)
string(TOUPPER "${Kokkos_OPTIONS}" _options)
if(NOT _devices MATCHES "CUDA")
  message(FATAL_ERROR "Kokkos_ENABLE_CUDA missing: devices=${Kokkos_DEVICES}")
endif()
if(NOT _arch MATCHES "AMPERE80")
  message(FATAL_ERROR "Kokkos_ARCH_AMPERE80 missing: arch=${Kokkos_ARCH}")
endif()
if(NOT _options MATCHES "CUDA_LAMBDA")
  message(FATAL_ERROR "Kokkos_ENABLE_CUDA_LAMBDA missing: options=${Kokkos_OPTIONS}")
endif()
if(NOT _options MATCHES "CUDA_CONSTEXPR")
  message(FATAL_ERROR "Kokkos_ENABLE_CUDA_CONSTEXPR missing: options=${Kokkos_OPTIONS}")
endif()
get_target_property(_defs Kokkos::kokkos INTERFACE_COMPILE_DEFINITIONS)
file(WRITE "${CMAKE_BINARY_DIR}/kokkos-features.txt" "devices=${Kokkos_DEVICES}\narch=${Kokkos_ARCH}\noptions=${Kokkos_OPTIONS}\ndefinitions=${_defs}\n")
EOF
    "$CMAKE" -S . -B build -DCMAKE_PREFIX_PATH="$KOKKOS_PREFIX"
    cat build/kokkos-features.txt
    ;;

  kokkos_cuda_production_rank)
    KOKKOS_PREFIX="${RED_SHIRT_KOKKOS_PREFIX:?RED_SHIRT_KOKKOS_PREFIX is required}"
    MPICXX="${RED_SHIRT_MPICXX:?RED_SHIRT_MPICXX is required}"
    RUNNER="${RED_SHIRT_PRODUCTION_RUNNER:?RED_SHIRT_PRODUCTION_RUNNER is required}"
    CMAKE="${RED_SHIRT_CMAKE:-cmake}"
    require_exec "$RUNNER"
    cat >CMakeLists.txt <<'EOF'
cmake_minimum_required(VERSION 3.20)
project(kokkos_cuda_probe LANGUAGES CXX CUDA)
find_package(MPI REQUIRED COMPONENTS CXX)
find_package(Kokkos CONFIG REQUIRED)
add_executable(kokkos_cuda_probe probe.cpp)
target_link_libraries(kokkos_cuda_probe PRIVATE MPI::MPI_CXX Kokkos::kokkos)
target_compile_features(kokkos_cuda_probe PRIVATE cxx_std_20)
EOF
    cat >probe.cpp <<'EOF'
#include <Kokkos_Core.hpp>
#include <mpi.h>
#include <cstdio>
int main(int argc, char **argv) {
  MPI_Init(&argc, &argv); int rank=-1; MPI_Comm_rank(MPI_COMM_WORLD, &rank);
  Kokkos::initialize(argc, argv); long value=0;
  Kokkos::parallel_reduce("probe", 1024, KOKKOS_LAMBDA(int, long& x){x+=1;}, value);
  Kokkos::fence(); std::printf("KOKKOS_PROBE rank=%d value=%ld\n", rank, value);
  Kokkos::finalize(); MPI_Finalize(); return value == 1024 ? 0 : 4;
}
EOF
    "$CMAKE" -S . -B build -DCMAKE_CXX_COMPILER="$MPICXX" -DCMAKE_PREFIX_PATH="$KOKKOS_PREFIX"
    "$CMAKE" --build build --parallel "${RED_SHIRT_BUILD_JOBS:-8}"
    RED_SHIRT_EXECUTABLE="$PWD/build/kokkos_cuda_probe" RED_SHIRT_OUTPUT_DIR="$PWD" "$RUNNER" | tee production-run.log
    python3 - <<'PY'
import re
text=open('production-run.log').read()
records=re.findall(r'KOKKOS_PROBE rank=(\d+) value=(\d+)', text)
assert {int(r) for r,v in records} == set(range(8)), records
assert {int(v) for r,v in records} == {1024}, records
PY
    ;;

  pepper_configure_features)
    PEPPER_SOURCE="${RED_SHIRT_PEPPER_SOURCE:?RED_SHIRT_PEPPER_SOURCE is required}"
    CACHE_INIT="${RED_SHIRT_PEPPER_CACHE_INIT:?RED_SHIRT_PEPPER_CACHE_INIT is required}"
    CMAKE="${RED_SHIRT_CMAKE:-cmake}"
    [ -d "$PEPPER_SOURCE" ] || { printf 'Pepper source is absent: %s\n' "$PEPPER_SOURCE" >&2; exit 66; }
    require_file "$CACHE_INIT"
    "$CMAKE" -S "$PEPPER_SOURCE" -B build -C "$CACHE_INIT"
    require_file build/CMakeCache.txt
    cp build/CMakeCache.txt ./CMakeCache.txt
    grep -Eq '^PEPPER_MPI_DISABLED:BOOL=(OFF|FALSE)$' CMakeCache.txt
    grep -Eq '^Kokkos_ENABLE_CUDA:BOOL=ON$' CMakeCache.txt
    grep -Eq '^Kokkos_ENABLE_CUDA_LAMBDA:BOOL=ON$' CMakeCache.txt
    grep -Eq '^Kokkos_ENABLE_CUDA_CONSTEXPR:BOOL=ON$' CMakeCache.txt
    grep -Eq '^Kokkos_ARCH_AMPERE80:BOOL=ON$' CMakeCache.txt
    ;;
esac
