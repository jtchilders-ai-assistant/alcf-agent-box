# Disclaimer

**Please read this before using ALCF Agent in a Box.**

## Not an official ALCF / Argonne / DOE product

ALCF Agent in a Box is an **independent, community project**. It is **not** an
official product of Argonne National Laboratory (ANL), the Argonne Leadership
Computing Facility (ALCF), or the U.S. Department of Energy (DOE). It is **not
affiliated with, endorsed by, maintained by, or supported by** any of them.

References to ALCF systems and services (Aurora, Polaris, Sophia, Crux, the ALCF
Inference Service, the IRI Facility API, etc.) exist only because this tool is
designed to *interoperate* with those services using your own credentials. That
interoperability does not imply any endorsement or affiliation.

**Do not open ALCF support tickets about this tool.** For problems with the
tool itself, use this project's issue tracker. For problems with the underlying
ALCF services, follow normal ALCF support channels — but note that ALCF support
is under no obligation to help with an unofficial third-party tool.

## This is an autonomous AI agent — it can be wrong, and it can act

This software runs an **autonomous AI agent** that:

- **Generates text that may be incorrect, incomplete, or misleading.** Answers
  about queue policy, allocations, filesystems, job configuration, and commands
  can be wrong. **Verify anything important against the official ALCF
  documentation and your own judgment before relying on it.**
- **Takes real actions with your credentials.** When you authenticate, the agent
  can act *as you* against the ALCF Inference Service and the IRI Facility API —
  for example submitting, cancelling, or inspecting jobs, and reading, writing,
  or deleting files on Home/Eagle. These actions **consume your allocation
  (node-hours), can modify or delete your data, and are attributed to your
  account.**
- **May act in unintended ways.** Like any LLM-driven agent, it can
  misinterpret a request and do something you did not intend.

## You are responsible for what the agent does

By using this tool you acknowledge and agree that:

- **You are solely responsible** for all actions taken through the agent using
  your credentials, including consumed compute allocation, submitted or
  cancelled jobs, and any creation, modification, or deletion of your files.
- **You must comply** with all applicable ALCF / Argonne / DOE acceptable-use
  policies, allocation terms, data-handling rules, and export-control and
  security requirements. This tool does not grant you any access you do not
  already have, and does not relax any policy that applies to you.
- **You should not** use this tool with sensitive, export-controlled, or
  otherwise restricted data unless you have independently confirmed that doing
  so is permitted for both the tool and the model you select.
- **Review before you trust.** Treat the agent's output as a draft and its
  proposed actions as suggestions to be checked, especially anything
  destructive (deleting files, cancelling jobs) or costly (large jobs).

## No warranty; limitation of liability

This software is provided **"AS IS", without warranty of any kind**, express or
implied, as set out in the Apache License, Version 2.0 (see `LICENSE`, in
particular the "Disclaimer of Warranty" and "Limitation of Liability"
sections). To the maximum extent permitted by law, the authors and
contributors, and Argonne National Laboratory / ALCF / the DOE, shall **not be
liable** for any claim, damages, lost compute allocation, lost or corrupted
data, or other liability arising from the use of, or inability to use, this
tool or the actions taken by the agent.

Your use of the ALCF Inference Service and the IRI Facility API is additionally
governed by ALCF's own terms and policies.
