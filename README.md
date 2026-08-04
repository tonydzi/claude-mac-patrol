# claude-mac-patrol

**Your Mac is not old. It is holding thirty half-dead agent sessions.**

If you leave Claude Code (or any agent CLI) running for days, your machine slowly turns to
treacle. This repo is the janitor we wrote for our own fleet after that cost us a week, plus
the field notes for diagnosing it by hand.

One file. Python standard library only. No dependencies, no telemetry, no network.

```bash
git clone https://github.com/Palo-Alto-AI-Research-Lab/claude-mac-patrol
cd claude-mac-patrol
python3 mac_patrol.py --dry-run     # shows every decision, changes nothing
python3 mac_patrol.py --install     # 30-min schedule (launchd on macOS, cron on Linux)
```

## Measured on one laptop, 4 August 2026

| | before | after |
|---|---|---|
| load average | 233 | 27.7 |
| free CPU | 4% | 68% |
| open agent sessions | 32 | 6 |
| RAM held by one MCP server class | 4.9 GB | 0.81 GB |

Nothing was reinstalled and nothing was rebooted. The machine was simply carrying work
nobody was waiting for any more.

## The three things that cost us the most

**1. `ps %CPU` is an average over the process's whole LIFETIME.** It is not "CPU right now",
and on a long-lived daemon it is off by any amount you like. We once announced that the
system antivirus had been stuck for five days, off a reading of `%CPU = 91.4`. The honest
probe took five seconds:

```bash
t1=$(ps -o time= -p <PID>); sleep 5; t2=$(ps -o time= -p <PID>); echo "$t1 -> $t2"
```

`10:10.83 -> 10:10.83` — the process was asleep. The diagnosis was 100% wrong. Every verdict
in this tool is based on the delta of two CPU-time snapshots, never on `ps %CPU`.

**2. Memory is eaten by COPIES, not by fat singletons.** A watchdog that alarms on "any MCP
server over 700 MB" never fires while thirty copies of a 163 MB server quietly eat 4.9 GB.
Weigh the class, not the process:

```bash
for M in computer-use telegram-mcp claude-in-chrome; do
  printf "%-18s %2d procs  %5d MB\n" "$M" \
    $(pgrep -fc "$M") \
    $(( $(ps -Ao rss=,command= | grep "$M" | grep -v grep | awk '{s+=$1} END {print s+0}') / 1024 ))
done
```

The real fix for a class you use constantly is one shared daemon per machine instead of one
child per session. We did that for one MCP server and went from 48 copies / 11.7 GB to
4 copies / 97 MB. Until such a diet exists, closing idle sessions is the only lever — which
is what this robot does for you.

**3. A zombie cannot be killed. Kill its PARENT — and check the parent is not PID 1.**
A zombie is already dead; it lingers because its parent never collected the exit code.
`kill <zombie>` is a guaranteed no-op. And if the zombie's PPID is 1 it has already been
adopted by `launchd`, which will reap it — there is nothing to do at all. Also: a *stable*
pair of zombies is usually not a leak but a permanent quirk of some app. Measure whether the
count GROWS before you touch anything.

## What the robot does on a schedule

| class | action |
|---|---|
| orphaned MCP children (parent dead) | killed |
| agent sessions idle > 5h, or any session > 24h | closed — sessions are resumable from transcripts |
| GUI renderer burning > 40% of a core for > 12h | restarted; the app repaints its window |
| stuck OS daemon we own (indexer, spellchecker, iCloud sync) | restarted; macOS relaunches on demand |
| stuck OS daemon owned by root | **reported** with a ready `sudo kill <pid>` line |
| `WindowServer`, `kernel_task`, `launchd`, `loginwindow`, … | never touched, under any circumstances |
| zombies | reported by PARENT name, which is the actual cure |

A healthy run is silent. Set `MAC_PATROL_ALARM_CMD` and findings get piped to it as one line
(`export MAC_PATROL_ALARM_CMD="/usr/bin/osascript -e 'display notification'"`, a curl to
Slack, whatever you like).

### Safety rails you should know about

- **It never types a password.** Anything needing `sudo` is reported, not executed.
- **`mac_patrol_allow.txt`** (same directory) protects anything you list — by launchd label,
  systemd unit, or cmdline substring. You will need this: your own long-lived daemons have
  `PPID=1`, which is exactly what an orphan looks like. See `mac_patrol_allow.example.txt`.
- **It never kills its own process chain** — it walks its ppid up to init first.
- **`--dry-run`** prints the full kill list and touches nothing.
- **A system daemon younger than 2 hours is immune by design.** This matters right after you
  kill a Spotlight indexer: the replacements come back loud (`mds_stores` 47%, `installd` 34%)
  for ten to twenty minutes. That is reindexing, it is correct behaviour, and a naive
  watchdog would kill it in a loop forever.

## Field guide for doing it by hand

`SKILL.md` is the human/agent-readable procedure — the same one our agents follow, step by
step, with the traps annotated. Drop it into `~/.claude/skills/mac-patrol/SKILL.md` and your
Claude Code can run the whole cleanup for you, or read it yourself as a checklist.

## Tests

```bash
python3 test_mac_patrol.py     # 54 checks, fake process snapshots, kills nothing
```

The tests drive the real file (including a real injected crash, to prove the crash-guard
exits 4 and never masquerades as "found and handled").

## Platform

macOS is the target. Linux works (cron + `/proc`). Windows parses and schedules, but has no
CPU-delta measurement, so it falls back to the hard 24h session ceiling only — deliberately
returning *nothing* rather than a fake zero.

---

Built by [Palo Alto AI Research Lab](https://github.com/Palo-Alto-AI-Research-Lab) — one
founder, an AI cofounder, and a fleet of machines that kept slowing down until we wrote this.
Everything we ship is free.

**If this saved your afternoon, star it — and if it misdiagnoses something on your machine,
open an issue with the `--dry-run` output.** That output is exactly what we need, and a
disagreement with our thresholds is the most useful bug report we can get.

Anton Dziatkovskii · [@T0x0AA](https://t.me/T0x0AA) on Telegram ·
[ORCID 0000-0001-7408-3054](https://orcid.org/0000-0001-7408-3054) ·
[book a call](https://calendly.com/paloaltoailab/1-on-1-meeting-antony)

MIT.
