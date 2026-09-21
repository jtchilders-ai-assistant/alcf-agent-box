from pathlib import Path


ROOT = Path(__file__).parents[1]
PROMPT = ROOT / "config" / "red-shirt-polaris" / "tasks" / "pepper-gpu-8rank.md"


def test_pepper_prompt_is_task_only_and_contains_acceptance_contract():
    text = PROMPT.read_text(encoding="utf-8")
    required = (
        "AGENTS.md",
        "ENV.md",
        "295050cfa38e465fd8c299749d39e995d22dfba1",
        "8 MPI ranks",
        "2 allocated nodes",
        "4 A100 GPUs per node",
        "fixed random seed",
        "Drell-Yan",
        "ppee",
        "13.6 TeV",
        "20,000 accepted events",
        "raw event output",
        "dilepton invariant mass",
        "leading-lepton transverse momentum",
        "dilepton rapidity",
        "PNG",
        "CSV or JSON",
        "REPORT.md",
        "RESULT.json",
        "DONE",
        "FAILED",
        "ten-minute finalization reserve",
        "STATUS.json",
    )
    for phrase in required:
        assert phrase.lower() in text.lower(), phrase


def test_pepper_prompt_does_not_prescribe_or_claim_an_unverified_stack():
    text = PROMPT.read_text(encoding="utf-8").lower()
    forbidden = (
        "prefer this external kokkos",
        "kokkos 4.6.02",
        "tested polaris gnu environment",
        "cuda/12.9",
        "nvhpc/25.9",
        "module load",
        "cmake -d",
        "mpiexec --no-transfer",
        "red-shirt-host compile",
    )
    for phrase in forbidden:
        assert phrase not in text, phrase


def test_pepper_prompt_preserves_resident_ownership_and_evidence_boundaries():
    text = PROMPT.read_text(encoding="utf-8").lower()
    for phrase in (
        "you own dependency discovery",
        "choose a coherent compiler, mpi, cuda, and kokkos stack",
        "requested, detected, compiled/linked, and runtime evidence",
        "contradictory evidence",
        "do not fabricate",
        "source sha",
        "dirty-tree",
        "checksums",
    ):
        assert phrase in text, phrase
