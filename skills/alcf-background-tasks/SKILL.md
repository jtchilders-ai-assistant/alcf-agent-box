---
name: alcf-background-tasks
description: Watch ALCF jobs/builds in the background and act or notify — cron patterns that work in this container, plus ntfy push notifications to the user's phone/desktop.
category: research
---

# ALCF background tasks (cron watching + notifications)

Load this skill when the user asks to **monitor something and follow up** —
"watch my job and continue when it starts", "tell me when the build finishes",
"check the queue every few minutes", "notify me when Polaris comes back".

## HARD FACT — cron output CANNOT reach this chat

The TUI and the web dashboard are **pull** surfaces. A cron job's result can be
delivered ONLY to gateway chat platforms (Telegram/Slack/Discord/email/…, none
configured by default in this container) or to files
(`deliver: local` → `~/.hermes/cron/output/` as timestamped markdown). It can
NEVER appear in this conversation or "wake" this session. Do not promise the
user a message here; do not create a job with `deliver: origin` from a TUI/
dashboard session and imply they'll see it.

What DOES work in this container:
1. **The cron job acts on its own** (best — see next section).
2. **The cron job pushes a phone/desktop notification via ntfy** (if configured).
3. The user asks you later and you read `~/.hermes/cron/output/` or re-check
   live state on demand.

## Pattern: watch-then-CONTINUE (prefer this over watch-then-notify)

A cron run is a **full agent session with skills** — it can do the work itself
instead of asking the user to come back and say "go". For "when my job starts /
node frees up / system comes back, do X":

1. **Write the state down first.** Before creating the job, persist everything
   a fresh session needs into a resume file (per SOUL.md): what's done, the
   exact `module` setup, the next command(s). For cluster work put it ON the
   cluster (e.g. `$HOME/agent-in-a-box/workspaces/<proj>/AGENT_STATE.md`).
2. **Create ONE cron job whose prompt is self-contained**: check the condition;
   if not met, reply briefly and stop; if met, load the needed skills, read the
   resume file, EXECUTE the next step, write results back to the resume file,
   send an ntfy notification if configured, and **disable/delete the job**
   (one-shot duration schedules like `30m` auto-delete; `every Nm` jobs must
   clean themselves up or they run forever).
3. Tell the user where the results will land (the resume file / build log /
   `~/.hermes/cron/output/`) and that you can pick it up from there next time
   they open a chat.

## Polling etiquette (each agent-mode run costs inference tokens)

- **Never poll faster than every 5 minutes in agent mode.** A 60-second
  agent-mode poll burns tokens ~1440×/day for nothing. (A previous session left
  exactly that running — an `every 1m`, `repeat: forever` job.)
- Faster checks belong in a **no-agent script job** (`--no-agent --script
  <name>` with the script under `~/.hermes/scripts/`): deterministic, zero
  tokens; empty stdout = silent, non-empty stdout = delivered, errors alert. A
  script can also gate a following agent run by printing `{"wakeAgent": false}`
  as its last line.
- Always set an end condition: a one-shot schedule, a self-disabling prompt, or
  a promise to the user to clean it up. Check leftovers with
  `cronjob(action='list')` and delete stale watchers.
- Schedule syntax accepted: `'30m'` (one-shot), `'every 30m'` (recurring), cron
  expressions, or a timestamp — NOT bare `'60s'`.

## ntfy push notifications (`/opt/alcf/alcf_notify.py`)

If the user configured `ALCF_NTFY_TOPIC` (docker run env), you can push real
notifications to their phone/desktop — from this session OR from a cron run:

    PY=/opt/hermes/.venv/bin/python

    # Is it configured? (exit 0 = yes, 3 = no topic set)
    $PY /opt/alcf/alcf_notify.py check

    # Send one (title/priority/tags optional):
    $PY /opt/alcf/alcf_notify.py send --title "Polaris" --priority high \
        --tags rocket "Job 7386110 is RUNNING — resuming the Pepper build"

- If `check` says NOT CONFIGURED, tell the user how to enable it: pick a
  hard-to-guess topic (it acts as a password), subscribe in the ntfy app or at
  `https://ntfy.sh/<topic>`, and restart the container with
  `-e ALCF_NTFY_TOPIC=<topic>` (self-hosters: `-e ALCF_NTFY_SERVER=<url>`).
  Don't silently skip notifying — say it's unavailable and offer the setup.
- **Status-level facts only** in messages (job id, state, exit code, one-line
  next step). The topic rides a public relay unless self-hosted: NEVER include
  tokens, secrets, or file contents.
- Sends must never break the main task: `... send "..." || true` in scripts.

## Suggested flow for "watch my job and continue"

1. Confirm the exact condition + the next action with the user.
2. Persist the resume file (cluster-side for builds).
3. `alcf_notify.py check` — mention notifications are/aren't available.
4. Create the self-continuing cron job (sane interval, end condition, ntfy send
   in the prompt if available).
5. Tell the user: what the job checks, how often, what it will DO when the
   condition fires, where results land, and that this chat will NOT get a
   message (only ntfy / their next visit).
