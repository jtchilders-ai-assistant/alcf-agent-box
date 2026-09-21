import re
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


def test_soul_states_compute_identity_and_docs_contract():
    soul = (ROOT / "config/red-shirt-polaris/SOUL.md").read_text()
    for phrase in (
        "Red Shirt Polaris", "Apptainer", "PBS job", "Polaris compute node",
        "ALCF Inference Service", "Wesley", "standard A2A",
        "proxy.alcf.anl.gov:3128", "/opt/red-shirt-polaris/docs/README.md",
    ):
        assert phrase in soul, f"SOUL.md missing required phrase: {phrase}"
    assert "cite the local source path" in soul.lower()
    # Identity must be exact, not merely a substring that would still be
    # present in a renamed/suffixed identity (e.g. "Red Shirt Polaris Mini").
    identity_line = next(
        line for line in soul.splitlines() if line.startswith("You are Red Shirt Polaris")
    )
    assert identity_line.startswith(
        "You are Red Shirt Polaris, a Hermes agent running inside an Apptainer"
    ), f"unexpected identity framing: {identity_line!r}"
    lowered = soul.lower()
    # Authenticated, per-direction A2A relationship with Wesley — not just
    # the bare word "A2A" somewhere in the file.
    assert "authenticated" in lowered and "a2a" in lowered
    assert "per-direction" in lowered or "per direction" in lowered
    assert "bearer" in lowered
    assert "does not accept" in lowered or "do not accept" in lowered
    assert "unauthenticated a2a" in lowered
    # Explicit read-before-act docs contract, not just a bare path mention.
    assert "read " in lowered and "/opt/red-shirt-polaris/docs/readme.md" in lowered
    assert (
        "before" in lowered
        and "polaris-specific" in lowered
        and ("advice" in lowered or "action" in lowered)
    )
    # Explicit official-vs-local classification and non-policy framing.
    assert "official snapshot" in lowered
    assert "local deployment note" in lowered or "locally measured" in lowered
    assert "not alcf policy" in lowered or "not official alcf" in lowered


def test_soul_is_non_roleplay_and_covers_full_operating_contract():
    soul = (ROOT / "config/red-shirt-polaris/SOUL.md").read_text()
    lowered = soul.lower()
    # No Star Trek role-play / catchphrase framing.
    for banned in ("captain", "away team", "beam me", "red alert", "phaser"):
        assert banned not in lowered, f"SOUL.md must not role-play: found {banned!r}"
    for phrase in (
        "no docker",
        "privileged networking",
        "ssh",
        "explicitly mounted",
        "distinguish",
        "verify",
        "readback",
        "uncertainty",
        "security",
        "allocation",
        "job lifetime",
    ):
        assert phrase in lowered, f"SOUL.md missing required operating phrase: {phrase}"


def test_soul_covers_resident_execution_and_evidence_contract():
    soul = (ROOT / "config/red-shirt-polaris/SOUL.md").read_text()
    lowered = soul.lower()
    for phrase in (
        "packaging boundary",
        "dependency discovery and installation",
        "build, tests,",
        "execution, and scientific analysis",
        "launch acknowledgement",
        "terminal result",
        "contradictory evidence",
        "requested configuration",
        "detected configuration",
        "compiled and linked",
        "runtime evidence",
        "terminal checkpoint",
        "time exhaustion",
    ):
        assert phrase in lowered, f"SOUL.md missing resident evidence rule: {phrase}"


def test_doc_index_has_provenance_for_every_snapshot():
    index = (ROOT / "docs/polaris-snapshot/README.md").read_text()
    docs = list((ROOT / "docs/polaris-snapshot").glob("*/*.md"))
    assert docs
    for doc in docs:
        assert str(doc.relative_to(ROOT / "docs/polaris-snapshot")) in index
    assert "Canonical URL" in index and "Retrieved" in index and "Classification" in index


def test_doc_index_records_retrieval_date_and_classification_values():
    index = (ROOT / "docs/polaris-snapshot/README.md").read_text()
    assert "2026-09-16" in index
    assert "official" in index.lower()
    assert "local" in index.lower()


def _parse_markdown_tables(text):
    """Parse pipe-delimited Markdown tables into (header, rows) pairs.

    Independent of the specific column set so it works for both the
    official-docs table (Canonical URL) and the local-notes table (Source).
    """
    lines = text.splitlines()
    tables = []
    i = 0
    separator_re = re.compile(r"^\|[-\s|]+\|$")
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("|") and i + 1 < len(lines) and separator_re.match(lines[i + 1].strip()):
            header = [c.strip() for c in line.strip("|").split("|")]
            rows = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            tables.append((header, rows))
            i = j
            continue
        i += 1
    return tables


def _row_field(header, row, *names):
    for name in names:
        if name in header:
            return row[header.index(name)]
    raise AssertionError(f"none of {names} present in table header {header}")


def test_doc_index_row_metadata_is_independently_complete_per_snapshot():
    """Requirement 7: a single incomplete index row must fail the suite even
    when every other row and every global keyword check still passes.

    This walks every parsed row rather than checking the document text as a
    whole, so deleting a single date/URL/classification cell — while leaving
    the words "Canonical URL", "2026-09-16", "official", and "local"
    present elsewhere in the file — still fails.
    """
    root = ROOT / "docs/polaris-snapshot"
    index_path = root / "README.md"
    index = index_path.read_text()
    tables = _parse_markdown_tables(index)
    assert tables, "no Markdown tables found in docs/polaris-snapshot/README.md"

    on_disk = {str(p.relative_to(root)) for p in root.glob("*/*.md")}
    indexed_paths = set()

    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    for header, rows in tables:
        assert rows, f"table with header {header} has no data rows"
        for row in rows:
            assert len(row) == len(header), (
                f"row {row!r} does not match header {header!r} (missing/extra cell)"
            )
            title = _row_field(header, row, "Title")
            provenance = _row_field(header, row, "Canonical URL", "Source")
            retrieved = _row_field(header, row, "Retrieved")
            local_path_cell = _row_field(header, row, "Local path")
            classification = _row_field(header, row, "Classification")

            local_path = local_path_cell.strip("`")

            assert title, f"row for {local_path!r} is missing a Title"
            assert provenance, f"row for {local_path!r} is missing canonical URL / provenance"
            assert date_re.match(retrieved), (
                f"row for {local_path!r} has missing/malformed Retrieved date: {retrieved!r}"
            )
            assert retrieved == "2026-09-16", (
                f"row for {local_path!r} has unexpected retrieval date: {retrieved!r}"
            )
            assert local_path, f"row is missing a Local path: {row!r}"
            assert local_path in on_disk, (
                f"row references {local_path!r}, which is not a real file under docs/polaris-snapshot"
            )
            assert classification.lower() in ("official", "local"), (
                f"row for {local_path!r} has missing/invalid Classification: {classification!r}"
            )
            # Classification must match the file's actual directory, not just
            # be present as a legal value.
            expected_classification = local_path.split("/", 1)[0]
            assert classification.lower() == expected_classification, (
                f"row for {local_path!r} claims classification {classification!r} "
                f"but lives under {expected_classification!r}"
            )
            if expected_classification == "official":
                assert provenance.startswith("https://docs.alcf.anl.gov/"), (
                    f"official row for {local_path!r} must cite a canonical "
                    f"https://docs.alcf.anl.gov/... URL, got {provenance!r}"
                )
            else:
                # Local (measured, non-official) rows must cite concrete,
                # existing repository-local evidence — at least one
                # backticked repo-relative path that actually resolves on
                # disk — not unverifiable prose. A description with no
                # backticked path, or a backticked path to a nonexistent
                # file, must fail.
                repo_paths = re.findall(r"`([^`]+)`", provenance)
                assert repo_paths, (
                    f"local row for {local_path!r} must cite at least one "
                    f"backticked repository-local path as provenance, got {provenance!r}"
                )
                existing = [p for p in repo_paths if (ROOT / p).is_file()]
                assert existing, (
                    f"local row for {local_path!r} cites backticked path(s) "
                    f"{repo_paths!r} but none resolve to a real file under {ROOT}"
                )

            indexed_paths.add(local_path)

    assert indexed_paths == on_disk, (
        "index rows and on-disk snapshot files must match exactly: "
        f"missing from index={on_disk - indexed_paths}, "
        f"stale in index={indexed_paths - on_disk}"
    )


def test_official_snapshots_cover_minimum_required_topics():
    official_dir = ROOT / "docs/polaris-snapshot/official"
    assert official_dir.is_dir()
    files = list(official_dir.glob("*.md"))
    assert files, "no official snapshot files present"
    combined = "\n".join(f.read_text().lower() for f in files)
    combined_names = " ".join(f.name.lower() for f in files)
    # Minimum topics from the design/plan: overview/getting-started, PBS/job
    # queues, filesystems/storage, modules/programming environment,
    # containers/Apptainer, node-local storage.
    for topic_markers in (
        ("getting started", "overview"),
        ("pbs", "queue"),
        ("filesystem", "storage"),
        ("module", "programming environment"),
        ("apptainer", "container"),
        ("local scratch", "node-local", "/local/scratch"),
    ):
        assert any(marker in combined or marker in combined_names for marker in topic_markers), (
            f"no official snapshot covers required topic markers: {topic_markers}"
        )


def test_official_snapshots_are_real_fetched_content_not_fabricated_stubs():
    official_dir = ROOT / "docs/polaris-snapshot/official"
    for f in official_dir.glob("*.md"):
        text = f.read_text()
        assert len(text) > 500, f"{f} looks too small to be real fetched doc content"


def test_local_deployment_notes_are_explicitly_non_official():
    notes = (ROOT / "docs/polaris-snapshot/local/deployment-notes.md").read_text()
    lowered = notes.lower()
    assert "not official alcf policy" in lowered or "not an official alcf" in lowered
    assert "tailscale" in lowered
    assert "derp" in lowered
    assert "proxy.alcf.anl.gov:3128" in notes
    # Real port check, not ICMP ping, per measured findings.
    assert "ping" not in lowered or "not" in lowered
    assert "http" in lowered

    index = (ROOT / "docs/polaris-snapshot/README.md").read_text()
    assert "local/deployment-notes.md" in index
