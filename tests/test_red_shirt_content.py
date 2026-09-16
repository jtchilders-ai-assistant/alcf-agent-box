from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]

HERMES_INDEX_DIGEST = (
    "sha256:99641e57ec762c59e54cb44aa6746b7fc68c18b3c5ddb088af54234c613d9294"
)
TAILSCALE_DIGEST = (
    "sha256:b2a19f6b6402adc26a2aa8cb90da66afe3061e718ac67ed3f21ec3d4b366439f"
)

REQUIRED_COPY_DESTINATIONS = (
    "/usr/local/bin/tailscale",
    "/usr/local/bin/tailscaled",
    "/opt/red-shirt-polaris/connect_proxy.py",
    "/opt/red-shirt-polaris/red_shirt_config.py",
    "/opt/red-shirt-polaris/red_shirt_probe.py",
    "/opt/red-shirt-polaris/entrypoint.sh",
    "/opt/red-shirt-polaris/config/",
    "/opt/red-shirt-polaris/docs/",
    "/opt/red-shirt-polaris/skills/",
)

# Complete, exact COPY directives (source -> destination, including the
# tailscale-src stage COPYs). Order-independent set comparison: a build that
# swaps a source path for the wrong file (while keeping the destination
# unchanged) must fail this test.
REQUIRED_COPY_DIRECTIVES = (
    "COPY --from=tailscale-src /usr/local/bin/tailscale /usr/local/bin/tailscale",
    "COPY --from=tailscale-src /usr/local/bin/tailscaled /usr/local/bin/tailscaled",
    "COPY scripts/connect_proxy.py /opt/red-shirt-polaris/connect_proxy.py",
    "COPY scripts/red_shirt_config.py /opt/red-shirt-polaris/red_shirt_config.py",
    "COPY scripts/red_shirt_probe.py /opt/red-shirt-polaris/red_shirt_probe.py",
    "COPY scripts/red_shirt_entrypoint.sh /opt/red-shirt-polaris/entrypoint.sh",
    "COPY config/red-shirt-polaris/ /opt/red-shirt-polaris/config/",
    "COPY docs/polaris-snapshot/ /opt/red-shirt-polaris/docs/",
    "COPY skills/ /opt/red-shirt-polaris/skills/",
)


def _dockerfile_lines():
    return (ROOT / "Dockerfile.red-shirt-polaris").read_text().splitlines()


def test_compute_image_is_pinned_and_non_root():
    text = (ROOT / "Dockerfile.red-shirt-polaris").read_text()
    assert "nousresearch/hermes-agent:v2026.9.14@sha256:" in text
    assert "tailscale/tailscale:v1.88.3@sha256:" in text
    assert "USER hermes" in text
    assert 'ENTRYPOINT ["/opt/red-shirt-polaris/entrypoint.sh"]' in text


def test_hermes_and_tailscale_bases_pin_exact_verified_digests():
    text = _dockerfile_lines()
    hermes_line = next(
        line for line in text if line.startswith("ARG HERMES_BASE=")
    )
    assert (
        hermes_line
        == f"ARG HERMES_BASE=nousresearch/hermes-agent:v2026.9.14@{HERMES_INDEX_DIGEST}"
    )

    tailscale_line = next(
        line for line in text if line.startswith("FROM tailscale/tailscale:")
    )
    assert (
        tailscale_line
        == f"FROM tailscale/tailscale:v1.88.3@{TAILSCALE_DIGEST} AS tailscale-src"
    )


def test_dockerfile_copies_every_required_content_path():
    text = (ROOT / "Dockerfile.red-shirt-polaris").read_text()
    for destination in REQUIRED_COPY_DESTINATIONS:
        assert destination in text, f"missing required COPY destination: {destination}"


def test_dockerfile_copy_directives_match_exact_source_and_destination():
    lines = [line.strip() for line in _dockerfile_lines()]
    copy_lines = {line for line in lines if line.startswith("COPY ")}
    for directive in REQUIRED_COPY_DIRECTIVES:
        assert directive in copy_lines, f"missing exact COPY directive: {directive}"
    # No unexpected extra COPY into the required destinations set (guards
    # against a source-path swap that still lands on the right destination
    # but drags in wrong/duplicate content).
    required_destinations = {d.split(" ")[-1] for d in REQUIRED_COPY_DIRECTIVES}
    for line in copy_lines:
        dest = line.split(" ")[-1]
        if dest in required_destinations:
            assert line in REQUIRED_COPY_DIRECTIVES, (
                f"unexpected COPY directive targeting a required destination: {line}"
            )


def test_final_user_and_entrypoint_ordering_is_non_root_last():
    lines = _dockerfile_lines()
    user_indices = [i for i, line in enumerate(lines) if line.startswith("USER ")]
    entrypoint_indices = [
        i for i, line in enumerate(lines) if line.startswith("ENTRYPOINT ")
    ]
    assert user_indices, "no USER directive found"
    assert entrypoint_indices, "no ENTRYPOINT directive found"

    # The image must start privileged (root, to install/copy/chown) and end
    # unprivileged (hermes) — the final USER directive in the file must be
    # exactly "USER hermes", and it must precede the single ENTRYPOINT line,
    # which must be the last line of the file.
    last_user_line = lines[user_indices[-1]]
    assert last_user_line == "USER hermes"
    assert user_indices[-1] < entrypoint_indices[-1]
    assert entrypoint_indices[-1] == len(lines) - 1

    # chown/chmod hardening on the compute content must happen while still
    # root, i.e. before the final USER hermes switch.
    chown_indices = [
        i for i, line in enumerate(lines) if "chown" in line and "hermes:hermes" in line
    ]
    assert chown_indices, "no chown hermes:hermes hardening step found"
    assert all(i < user_indices[-1] for i in chown_indices)


def _load_workflow():
    return yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())


def _red_shirt_job(workflow):
    jobs = workflow["jobs"]
    assert "build-red-shirt-polaris" in jobs, "dedicated build-red-shirt-polaris job missing"
    return jobs["build-red-shirt-polaris"]


def _build_push_step(job):
    for step in job["steps"]:
        if step.get("uses", "").startswith("docker/build-push-action@"):
            return step
    raise AssertionError("no docker/build-push-action step in build-red-shirt-polaris job")


def test_ci_publishes_compute_image_for_both_architectures():
    text = (ROOT / ".github/workflows/build.yml").read_text()
    assert "alcf-red-shirt-polaris" in text
    assert "file: Dockerfile.red-shirt-polaris" in text
    assert "platforms: linux/amd64,linux/arm64" in text


def test_red_shirt_job_is_dedicated_and_isolated_from_other_jobs():
    workflow = _load_workflow()
    jobs = workflow["jobs"]
    job = _red_shirt_job(workflow)
    step = _build_push_step(job)
    with_block = step["with"]

    assert with_block["file"] == "Dockerfile.red-shirt-polaris"
    assert with_block["platforms"] == "linux/amd64,linux/arm64"
    assert with_block["provenance"] is False
    assert with_block["push"] is True

    build_args = with_block.get("build-args", "")
    assert "ALCF_GIT_SHA=${{ github.sha }}" in build_args

    # SHA-scoped cache so a stale cross-commit cache can never serve this job.
    assert with_block["cache-from"] == "type=gha,scope=red-shirt-polaris-${{ github.sha }}"
    assert with_block["cache-to"] == "type=gha,scope=red-shirt-polaris-${{ github.sha }},mode=max"

    # The published image must be commit-addressed (design requires a
    # SHA-tagged, immutable publish target, not only a floating :latest).
    meta_step = next(
        s for s in job["steps"] if s.get("uses", "").startswith("docker/metadata-action@")
    )
    meta_tags = meta_step["with"]["tags"]
    assert "type=sha,format=short" in meta_tags

    # The dedicated job must not be the existing laptop/probe jobs, and those
    # jobs must not have been repointed at the new Dockerfile/image.
    assert set(jobs.keys()) >= {"build", "build-headscale-probe", "build-red-shirt-polaris"}
    other_build_push_steps = []
    for job_name in ("build", "build-headscale-probe"):
        for other_step in jobs[job_name]["steps"]:
            if other_step.get("uses", "").startswith("docker/build-push-action@"):
                other_build_push_steps.append((job_name, other_step))
    for job_name, other_step in other_build_push_steps:
        other_file = other_step["with"].get("file")
        assert other_file != "Dockerfile.red-shirt-polaris", (
            f"{job_name} must not build the Red Shirt Dockerfile"
        )

    # metadata-action image target must resolve to the dedicated image name,
    # not the laptop image or the headscale probe image.
    assert meta_step["with"]["images"] == "${{ env.RED_SHIRT_POLARIS_IMAGE }}"
    assert (
        workflow["env"]["RED_SHIRT_POLARIS_IMAGE"]
        == "ghcr.io/${{ github.repository_owner }}/alcf-red-shirt-polaris"
    )
