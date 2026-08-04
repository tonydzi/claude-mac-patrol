---
name: mac-patrol
description: >
  Clean up a Mac that has slowed to a crawl under an always-on agent CLI: find and remove
  orphaned MCP children, idle agent sessions, stuck CPU hogs, hung headless LLM calls and
  stuck OS daemons. Triggers: "/mac-patrol", "clean up my mac", "my mac is slow", "kill
  zombies", "high load average", "why is my machine crawling".
  Wraps mac_patrol.py. Order matters: MEASURE first (is it growing? is it burning RIGHT NOW,
  by CPU-time delta?), only then kill. A stable pair of zombies is normal, not a leak; a
  renderer stuck for 12h is the opposite, and the robot handles that one itself.
  macOS-first; Linux works; Windows is partial (no delta measure).
---

# mac-patrol — the procedure

Engine: `mac_patrol.py` (single source of the logic — do not reimplement it here).
This file is the ORDER OF OPERATIONS around the engine. Every step below was paid for on a
live machine; the traps are annotated with what they actually cost.

## Step 0 — "slow" is not one disease, and load average is not the evidence

Three different complaints, three different culprits. Jumping straight to zombies means
fixing at random.

| What the human feels | Culprit | Probe |
|---|---|---|
| Cursor stutters, windows repaint in jerks | swap / WindowServer under someone else's load | `sysctl vm.swapusage`, `memory_pressure \| tail -3` |
| Beachball, disk churns, Spotlight search empty | indexers (`mds_stores`, `corespotlightd`, `installd`) | CPU delta on those (Step 1) |
| Everything at once, apps take seconds to open | memory eaten by MCP copies | Step 1b |

⚠️ **Load average on a Mac with dozens of sessions means almost nothing.** Measured: load 233
while only 4% of the CPU was busy — the processes were queued on MEMORY, not on the CPU. The
honest pair of numbers is `free CPU %` from `top` plus `swap used`. Load is a before/after
prop, not a diagnosis.

## Step 1 — MEASURE (never kill blind)

```bash
top -l 1 -n 0 | head -12                  # load avg + memory
ps aux | awk '$8 ~ /^Z/ {print $2, $8}'   # zombies (state Z)
python3 mac_patrol.py --dry-run           # every decision, no action
memory_pressure | tail -3                 # the TRUTH about memory
```

⚠️ Traps, all paid for:

- **"257 MB unused" in `top` does not mean you are out of memory.** macOS deliberately keeps
  RAM busy as cache. The truth is `memory_pressure` → "System-wide memory free percentage"
  (70% = you are fine).
- **`ps %CPU` is the average over the process's whole LIFETIME.** A patrol can shout
  "STUCK AT 76%" about a process that went quiet hours ago. Before any kill:
  ```bash
  t1=$(ps -o time= -p <PID>); sleep 5; t2=$(ps -o time= -p <PID>); echo "$t1 -> $t2"
  ```
  Delta ≈ 0 over 5s = asleep, do NOT kill. Delta ≈ 5s over 5s = burning a whole core, now.
- ⚠️ **One probe is not enough for a bursty daemon.** Measured on `searchpartyd` inside a
  single session: probe #1 = 0.01s/5s (asleep — the accusation was false) → twenty minutes
  later probe #2 = **5.04s/5s** (100% of a core — the accusation was true). Same process,
  opposite verdict. A verdict of "asleep" is good for minutes, not hours: measure immediately
  before the kill, not from memory. Borderline case = two or three probes spread out.

## Step 1b — memory is eaten by COPIES of an MCP server, not by one fat process

Your watchdog complains about "a bloated MCP over 700 MB" — but the real eater is usually
modest and numerous. **The weight of a class is footprint × number of sessions:**

```bash
for M in computer-use telegram-mcp claude-in-chrome; do
  printf "%-18s %2d procs  %5d MB\n" "$M" \
    $(pgrep -fc "$M") \
    $(( $(ps -Ao rss=,command= | grep "$M" | grep -v grep | awk '{s+=$1} END {print s+0}') / 1024 ))
done
```

Measured: one MCP server at **163 MB × 30 sessions = 4.9 GB**, while every individual copy
sat at a third of the "bloated" threshold, so nothing ever alarmed. Closing 13 sleeping
sessions took it to 0.81 GB. That — not orphans, not zombies — was the actual cause of the
slowdown.

The class-level fix is **one shared daemon per machine instead of one child per session**.
We did it for one MCP server (SSE daemon on a fixed port): 48 copies × 378 MB = 11.7 GB
became 4 copies / 97 MB. Where no such diet exists yet, closing idle sessions is the only
lever you have.

## Step 2 — zombies: first check whether they are GROWING (usually they are not)

⚠️ **Measure before you kill.** There are two entirely different kinds of zombie, and the
cure is opposite.

```bash
Z1=$(ps -Ao state|grep -c Z); sleep 45; Z2=$(ps -Ao state|grep -c Z); echo "$Z1 -> $Z2"
ps -Ao pid,ppid,etime,state,ucomm | awk '$4 ~ /^Z/'   # is their age == the parent's age?
```

- **Count is flat, zombie age == parent age** → not a leak, but a permanent state of some
  program. Do nothing, report it in one line.
- **Count grows between probes** → that is a leak. Go to Step 2b.

Measured: 2 → 2 over 45 seconds, four mentions in two days of logs. Verdict: not a leak.
Killing a useful service's parent over two harmless zombies is a bad trade.

## Step 2b — if they really grow: kill the PARENT, never the zombie

A zombie (STAT=Z, `<defunct>`) is already dead — `kill` on it does nothing. It lingers
because its parent never collected the exit code. Find the parent:

```bash
ps -o pid,ppid,stat,command -p <ZPID>
ps -o pid,command -p <PPID>
```

⛔ **First rule: the kill target is the zombie's PPID, and it must never be 1.**
`PID 1` is `launchd`; killing it is a kernel panic. If the zombie's PPID is already 1, it has
been adopted by launchd and **there is nothing to do at all** — launchd will reap it. Gate
this before any kill:

```bash
PPID=$(ps -o ppid= -p <ZPID> | tr -d ' ')
[ "$PPID" = "1" ] && { echo "adopted by launchd — leave it"; exit 0; }
[ -z "$PPID" ]    && { echo "parent already gone"; exit 0; }
```

Then branch on what the parent is (below, `<PPID>` is always the zombie's parent, and ≠ 1):

- **The parent is itself an orphan** (the PARENT's own ppid is 1) — e.g. an abandoned
  `geckodriver` from a dead Selenium run → `kill <PPID>` freely; the zombie moves to launchd
  and gets reaped. Check the ppid of the PARENT, not of the zombie: `ps -o ppid= -p $PPID`.
- **The parent is a well-behaved app that simply never reaps** → ⛔ **leave it alone; this is
  a false target.** Measured on a desktop time-tracker: killed the watcher → restarted it →
  the **new** watcher spawned exactly the same pair of zombies within two minutes. It does
  not reap its two children, ever; that is its permanent state, not a leak. kill→restart is
  whack-a-mole: the counter resets, the root stays. Cost of the zombies: two rows in the
  process table, zero CPU, zero memory. The real fix is upstream (update or disable the app),
  and that is the human's call.
  ⚠️ Also: some apps do NOT respawn their own helper after you kill it. If you killed it,
  restart it by hand.
- **The parent is a live working session** (your shell, your editor, your agent) → do not
  kill. The zombie is harmless. Report and move on.

## Step 3 — CPU hogs and hung LLM calls

- **A hung headless LLM call** (`gemini -p`, `claude -p`, `codex` running > 15 minutes):
  walk `ps -o ppid=` upward to find the owner. If the owner is a live session waiting on that
  call, killing the CALL is the RIGHT move — the session unblocks and regains control.
  ⚠️ **Name the tree explicitly, and guard against PID reuse.** The target is the call plus
  its wrapper (for `gemini` that is a PAIR of node processes: `node .../gemini` and its child
  `node --max-old-space-size=…`), but ⛔ never walk up as far as the owning session — that one
  must survive. A PID can be freed and reused between your `ps` and your `kill`, so re-check
  the target immediately before killing it:
  ```bash
  for P in <PID_wrapper> <PID_call>; do
    ps -o command= -p "$P" | grep -q 'gemini' && kill "$P" || echo "PID $P is a different process now — skip"
  done
  ```
  Take the age from the CALL (`ps -o etime= -p <PID>`), not from the owning session.
- **Root daemons** (`searchpartyd`, `mds`, …): cannot be killed without `sudo`, and this
  procedure never types a password. Delta-measure first (Step 1) — most of the time it has
  already gone quiet. Genuinely stuck → hand the human one line: `sudo kill <PID>`
  (launchd will restart it).
- **A GUI renderer — branch on AGE, not on percentage.** A fresh renderer at 100% is a live
  window doing work: leave it. A renderer that is burning **and** older than 12 hours is
  stuck: kill it, the app repaints and your data lives server-side. Measured: one renderer
  burned a core for **4 days 16 hours** (91 minutes of pure CPU time) with load average at 37;
  killing it took the load to 18. The robot does this itself (`RESTARTABLE_HOG` +
  `STUCK_AGE_H=12`), so you should not need to.
- ⛔⛔ **System processes a naive patrol will call "stuck" but which must NOT be killed.**
  A 12-hour threshold is meaningless for daemons that are SUPPOSED to live the whole session:

  | Process | A naive patrol says | What a kill actually does |
  |---|---|---|
  | `WindowServer` | "STUCK 56h, 82% cpu" | **logs you out**; everything unsaved is gone |
  | `launchd` (pid 1) | — | kernel panic |
  | `fileproviderd` | "108% cpu" | tears a cloud-drive sync in half |
  | your sync daemon | "hog" | stops file delivery you depend on |

  Their burning is a SYMPTOM of load you created (many windows, many sessions, many files in
  flight); the cure is removing that load, not the kill. Measured: `WindowServer` burning 77%
  of a core — the root cause was **36 simultaneous agent sessions**, not WindowServer.
- ⛔⛔ **An unfamiliar name at the top of `top` — ATTRIBUTE FIRST, judge second.**
  Identification takes a minute and answers the only question that matters: **whose work is
  this?**
  ```bash
  ps -o pid,ppid,etime,command= -p <PID>            # the full command line
  ls -l  $(which <name> || echo /path/to/bin)       # WHEN was it installed = who could have
  file   /path/to/bin ; strings /path/to/bin | head -50
  P=<PID>; while [ "$P" != 1 ]; do ps -o ppid=,command= -p $P; P=$(ps -o ppid= -p $P|tr -d ' '); done
  ```
  Measured: an unknown binary burning **728% CPU and 1.05 GB** looked like a runaway. One
  minute of identification: installed that same morning, an MCP server and repository indexer
  inside, and the ppid chain led to **a live agent session in the next window** that had run
  it deliberately. Nine minutes later it exited on its own. Killing it would have severed
  someone's work blindly.
  Rule: **live work is not killed on the strength of its position in `top`.** Owner first,
  verdict second. Could not attribute it? Report, do not kill.
- ⛔ **Never kill**: live sessions younger than 24h; anything whose burning is not confirmed
  by a DELTA.

## Step 3b — after killing a Spotlight daemon, expect a loud reindex. That is correct.

The indexer comes back, and for the first few minutes it works harder than the stuck one did:

```
kill corespotlightd + spotlightknowledged   (stuck for 128 hours)
   → a minute later: mds_stores 47%, installd 34%, fresh corespotlight daemons
   → 10-20 minutes later: quiet
```

Two consequences, both mandatory:

- **Do not take the before/after verdict immediately**, take it after things settle.
  Otherwise honest work looks like a failure: "I killed the hogs and it got worse" — those
  are the same daemons, now doing their job.
- ⛔ **Do not kill the reindex.** What saves you from an infinite kill-loop is the
  `SYSTEM_STUCK_H = 2` threshold: fresh daemons are immune by design. Do not reach past it
  by hand.

## Step 4 — VERIFY (a kill is silent; count the survivors)

`zsh` does not word-split `$VAR` in a `for` loop, so a kill loop can quietly become a no-op:

```bash
ps -p <PID1>,<PID2>,... | tail -n +2   # empty = all dead
ps -Ao state | grep -c '^Z'            # compare against the BASELINE, not against zero
uptime                                  # did load average fall?
```

⚠️ **The zombie criterion is the BASELINE, not zero.** Zero is unreachable and, as a
criterion, false: a permanent pair of zombies from some app (Step 2) will fail every honest
cleanup you ever run.

```bash
# before:     BASE=$(ps -Ao state | grep -c '^Z')
# after:      AFTER=$(ps -Ao state | grep -c '^Z')    # expect AFTER <= BASE
# not growing: sleep 45; ps -Ao state | grep -c '^Z'  # third number == AFTER
```

Growing between probes → that is a leak, go to Step 2. Flat → you are done.

Report as before → after: load average, zombie count, what was killed and why, and — equally
important — what was NOT touched and why. The reason for every kill must be *proved* by a
delta or by a parentage chain, never by "it looked wrong".

## What the robot already does, so you do not do it by hand

- **Measures burning by DELTA** of two CPU-time snapshots on every tick, never by `ps %CPU`.
- **Class "STUCK"**: burning > 40% of a core AND older than 12h. Restartable app renderers get
  restarted; anything else is reported with a "STUCK Nh" marker.
- **Counts concurrent sessions**: more than 20 → an alarm line, because each one drags its own
  MCP servers. This, not orphans, is the main memory cost on a busy machine.
- **Closes sessions idle for more than 5 hours.** Key measurement: a sleeping session still
  ticks at ~4-5% CPU (its own event loop), so the "asleep" threshold is <10% by honest delta;
  a genuinely working one burns 30-100%+ and lives as long as it needs. Without a delta
  measure (Windows) only the hard 24h ceiling applies, so a night robot never gets shot.
  Sessions are resumable from transcripts — closing one loses nothing.
- **Reports bloated MCP servers** (>700 MB) rather than killing them: many hang directly off a
  live app, and a blind kill severs a live chat.
- **Takes its node name from a stable source, not from environment variables.** Root cause we
  paid for: under `launchd` the environment is EMPTY, so the node named itself differently in
  scheduled runs than in interactive ones and wrote a SECOND state file — which then went
  stale silently under a freshness watchdog that was watching the other one. ⚠️ When you patch
  anything a scheduler runs, test it in a bare environment (`env -i HOME=$HOME …`), or your
  fix rests on a variable the robot does not have.

## The system tier — cleaning up OS processes too

**"System" is decided by PATH, not by a list of names.** A name list is always incomplete: in
the first version `airportd` went unrecognised purely because its name was missing. Path marks
(`/System/Library`, `/usr/libexec/`, `/Library/Apple/`, `/usr/sbin/`) cannot be faked.

Three buckets, and the border is the process OWNER:

| Bucket | What it is | What the robot does |
|---|---|---|
| `SYSTEM_NEVER` | WindowServer, kernel_task, launchd, loginwindow, coreaudiod, Dock, Finder… | Never touched. Death = losing your session, audio, or input |
| `SYSTEM_SAFE_KILL` **and owner == you** | AppleSpell, mdworker, bird, cloudd, searchpartyd, photoanalysisd… | Killed — macOS relaunches these on first demand, the loss is zero |
| everything else (usually `root`) | airportd, third-party daemons | **Report only, plus a ready `sudo kill <pid>` line.** This tool never types a password |

**Its own threshold: `SYSTEM_STUCK_H = 2` hours**, not the 12 used for a GUI renderer. A system
daemon that has burned a core for two hours is not busy — it is in a loop.

⚠️ **The trap that cost us the most.** We announced that the system antivirus had been stuck
for five days grinding the CPU, taking the number from `ps %CPU` = **91.4%**. The delta probe:
`10:10.83 → 10:10.83` over five seconds — the process was **asleep**, and the diagnosis was
100% wrong. That is precisely the trap Step 1 of this very document warns about, and we walked
into it anyway. **Before saying anything about a system process, take the delta — and do not
even look at `ps %CPU`.** The system tier therefore judges only by `cpu_delta_map`; on Windows,
where the delta is not implemented, the tier does not fire at all (honestly empty, rather than
a fake zero).

Tier tests: 11 checks inside `test_mac_patrol.py` (54/54 green) — our own daemon gets fixed,
root is not touched and comes back as a line, WindowServer survives even when we own it, a
young daemon is left alone, and the allowlist outranks the tier.

## Related

- Engine: `mac_patrol.py` — `--dry-run` reports only, `--verify` exits 0 iff the schedule is
  alive and ran within 2 hours.
- Allowlist: `mac_patrol_allow.txt` — your own long-lived daemons look exactly like orphans
  (`PPID=1`). List them there before the first scheduled run.
