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
#include <unistd.h>
int main(int argc, char **argv) {
  MPI_Init(&argc, &argv); int rank = -1, size = -1; char host[256] = {0};
  gethostname(host, sizeof(host) - 1);
  MPI_Comm_rank(MPI_COMM_WORLD, &rank); MPI_Comm_size(MPI_COMM_WORLD, &size);
  std::printf("MPI_PROBE rank=%d size=%d host=%s\n", rank, size, host);
  MPI_Finalize(); return size == 2 ? 0 : 3;
}
EOF
    "$MPICXX" -std=c++20 mpi.cpp -o mpi_probe
    "$MPIEXEC" --hostfile "$NODEFILE" -n 2 ./mpi_probe | tee mpi-run.log
    python3 - <<'PY'
import re
text=open('mpi-run.log').read()
records=re.findall(r'MPI_PROBE rank=(\d+) size=(\d+) host=(\S+)', text)
assert {int(r) for r,s,h in records} == {0,1}, records
assert {int(s) for r,s,h in records} == {2}, records
hosts={h.split('.',1)[0] for r,s,h in records}
assert len(hosts) == 2, records
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
    MPI_LINK_PROBE="$WORK/mpi_gtl_link_resolution/mpi_link_probe"
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
    cat >feature_compile.cpp <<'EOF'
#include <Kokkos_Core.hpp>
#ifndef KOKKOS_ENABLE_CUDA
#error KOKKOS_ENABLE_CUDA is required
#endif
#ifndef KOKKOS_ENABLE_CUDA_LAMBDA
#error KOKKOS_ENABLE_CUDA_LAMBDA is required
#endif
#ifndef KOKKOS_ENABLE_CUDA_CONSTEXPR
#error KOKKOS_ENABLE_CUDA_CONSTEXPR is required
#endif
#ifndef KOKKOS_ARCH_AMPERE80
#error KOKKOS_ARCH_AMPERE80 is required
#endif
int main() { return 0; }
EOF
    cat >CMakeLists.txt <<'EOF'
cmake_minimum_required(VERSION 3.20)
project(kokkos_features LANGUAGES CXX CUDA)
find_package(Kokkos CONFIG REQUIRED)
add_executable(kokkos_feature_compile feature_compile.cpp)
target_link_libraries(kokkos_feature_compile PRIVATE Kokkos::kokkos)
target_compile_features(kokkos_feature_compile PRIVATE cxx_std_20)
get_target_property(_defs Kokkos::kokkos INTERFACE_COMPILE_DEFINITIONS)
file(WRITE "${CMAKE_BINARY_DIR}/kokkos-features.txt" "definitions=${_defs}\n")
EOF
    "$CMAKE" -S . -B build -DCMAKE_PREFIX_PATH="$KOKKOS_PREFIX"
    "$CMAKE" --build build --parallel "${RED_SHIRT_BUILD_JOBS:-8}"
    ./build/kokkos_feature_compile
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
#include <fstream>
#include <string>
#include <unistd.h>
int main(int argc, char **argv) {
  MPI_Init(&argc, &argv); int rank=-1; char host[256] = {0};
  gethostname(host, sizeof(host) - 1); MPI_Comm_rank(MPI_COMM_WORLD, &rank);
  Kokkos::initialize(argc, argv); long value=0;
  Kokkos::parallel_reduce("probe", 1024, KOKKOS_LAMBDA(int, long& x){x+=1;}, value);
  Kokkos::fence();
  if (rank == 0) {
    std::ifstream maps("/proc/self/maps"); std::string line;
    while (std::getline(maps, line)) {
      if (line.find("mpi") != std::string::npos || line.find("gtl") != std::string::npos ||
          line.find("cuda") != std::string::npos) std::printf("LOADED_LIB %s\n", line.c_str());
    }
  }
  std::printf("KOKKOS_PROBE rank=%d host=%s value=%ld\n", rank, host, value);
  Kokkos::finalize(); MPI_Finalize(); return value == 1024 ? 0 : 4;
}
EOF
    "$CMAKE" -S . -B build -DCMAKE_CXX_COMPILER="$MPICXX" -DCMAKE_PREFIX_PATH="$KOKKOS_PREFIX"
    "$CMAKE" --build build --parallel "${RED_SHIRT_BUILD_JOBS:-8}"
    RED_SHIRT_EXECUTABLE="$PWD/build/kokkos_cuda_probe" RED_SHIRT_OUTPUT_DIR="$PWD" "$RUNNER" | tee production-run.log
    python3 - <<'PY'
import re
text=open('production-run.log').read()
records=re.findall(r'KOKKOS_PROBE rank=(\d+) host=(\S+) value=(\d+)', text)
expected=int(__import__('os').environ.get('RED_SHIRT_EXPECTED_RANKS', '8'))
expected_hosts=int(__import__('os').environ.get('RED_SHIRT_EXPECTED_HOSTS', '2'))
assert {int(r) for r,h,v in records} == set(range(expected)), records
assert {int(v) for r,h,v in records} == {1024}, records
hosts={h.split('.',1)[0] for r,h,v in records}
assert len(hosts) == expected_hosts, (hosts, records)
loaded=[line for line in text.splitlines() if line.startswith('LOADED_LIB ')]
assert any('mpi' in line.lower() for line in loaded), loaded
assert any('gtl' in line.lower() for line in loaded), loaded
assert any('cuda' in line.lower() for line in loaded), loaded
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
