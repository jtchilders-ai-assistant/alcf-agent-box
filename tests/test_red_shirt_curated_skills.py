import re
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
DOCKERFILE = ROOT / "Dockerfile.red-shirt-polaris"
ENTRYPOINT = ROOT / "scripts" / "red_shirt_entrypoint.sh"
CURATED_ROOT = ROOT / "config" / "red-shirt-polaris" / "skills"
EXPECTED_SKILLS = {
    "polaris-resident-build",
    "scientific-evidence-contract",
    "long-command-process-discipline",
    "polaris-mpi-apptainer",
}


def test_compute_image_copies_only_curated_red_shirt_skills():
    body = DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "COPY config/red-shirt-polaris/skills/ /opt/red-shirt-polaris/skills/"
        in body
    )
    assert "COPY skills/ /opt/red-shirt-polaris/skills/" not in body


def _skill_metadata(name):
    path = CURATED_ROOT / name / "SKILL.md"
    body = path.read_text(encoding="utf-8")
    assert body.startswith("---\n")
    match = re.match(r"^---\n(.*?)\n---\n(.+)$", body, re.DOTALL)
    assert match, name
    metadata = yaml.safe_load(match.group(1))
    assert metadata["name"] == name
    description = metadata["description"]
    assert description.startswith("Use when ")
    assert len(description) <= 120
    assert match.group(2).strip()
    return body


def test_curated_skill_set_is_exact_and_frontmatter_is_valid():
    assert CURATED_ROOT.is_dir()
    discovered = {path.parent.name for path in CURATED_ROOT.glob("*/SKILL.md")}
    assert discovered == EXPECTED_SKILLS
    for name in sorted(EXPECTED_SKILLS):
        _skill_metadata(name)


def test_polaris_resident_build_skill_closes_baseline_failures():
    body = _skill_metadata("polaris-resident-build").lower()
    for phrase in (
        "environment entries as observations",
        "dependency choices",
        "compiler and language feature",
        "mpi compile/link",
        "cuda runtime resolution",
        "required kokkos options",
        "application configure",
        "requested",
        "detected",
        "compiled/linked",
        "executed",
        "finding `kokkosconfig.cmake`",
        "reserve the final ten minutes",
        "patch application sources only",
    ):
        assert phrase in body, phrase


def test_entrypoint_recursively_seeds_nested_skill_files():
    body = ENTRYPOINT.read_text(encoding="utf-8")
    skills_call = 'managed_seed_tree "$RS_DIR/skills" "$RS_HOME/skills" "skills"'
    assert "managed_seed_tree" in body
    assert skills_call in body
    assert 'for f in "$RS_DIR"/skills/*' not in body


def test_skill_seeding_contract_preserves_user_edits_and_refreshes_managed_files():
    body = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'elif [ -f "$stamp" ]' in body
    assert 'log "kept user-edited $label"' in body
    assert 'log "updated $label from image"' in body
    assert "find " not in body, "recursive seed should not depend on shell find semantics"


def test_recursive_seed_copies_nested_files_and_preserves_user_edits(tmp_path):
    source = tmp_path / "image" / "skills"
    destination = tmp_path / "home" / "skills"
    stamps = tmp_path / "home" / ".red_shirt_seed_stamps"
    nested_source = source / "example" / "references" / "guide.md"
    nested_source.parent.mkdir(parents=True)
    nested_source.write_text("version one\n", encoding="utf-8")

    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
    functions = entrypoint[entrypoint.index("managed_seed() {"):entrypoint.index('managed_seed "$RS_DIR/config/SOUL.md"')]
    harness = tmp_path / "seed-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "PYTHON_BIN=python3\n"
        f"STAMP_DIR={stamps!s}\n"
        "mkdir -p \"$STAMP_DIR\"\n"
        "log() { :; }\n"
        + functions
        + f'\nmanaged_seed_tree "{source}" "{destination}" "skills"\n',
        encoding="utf-8",
    )
    first = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    nested_destination = destination / "example" / "references" / "guide.md"
    assert nested_destination.read_text(encoding="utf-8") == "version one\n"

    nested_source.write_text("version two\n", encoding="utf-8")
    second = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert nested_destination.read_text(encoding="utf-8") == "version two\n"

    nested_destination.write_text("user edit\n", encoding="utf-8")
    nested_source.write_text("version three\n", encoding="utf-8")
    third = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert third.returncode == 0, third.stderr
    assert nested_destination.read_text(encoding="utf-8") == "user edit\n"
