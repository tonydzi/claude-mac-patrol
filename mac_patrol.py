# -*- coding: utf-8 -*-
"""mac_patrol.py -- keep a Mac running Claude Code (or any agent) from grinding to a halt.

WHY: an agent CLI you leave open for days quietly accumulates half-dead sessions, orphaned
MCP child processes and stuck system daemons. Measured on one laptop: 172 orphaned MCP
Pythons + ~30 sessions 2-4 days old -> swap 14.5/15 GB full, machine crawling. Sweeping them:
free RAM 54% -> 77%, load average 233 -> 27.7.

WHAT IT HUNTS (in order of measured harm):
  1. ORPHANED MCP children  -- processes matching MCP_PATTERNS whose parent is dead
                               (PPID=1 on POSIX / parent PID missing on Windows). AUTO-KILL.
  2. STALE agent sessions   -- `claude` binaries idle >IDLE_SESSION_H, or older than
                               MAX_SESSION_H. Sessions are resumable from transcripts, so
                               killing is reversible. AUTO-KILL (never self, never our
                               own ancestor chain).
  3. STUCK hogs             -- burning >CPU_HOG_PCT of a core AND older than STUCK_AGE_H.
                               Restartable app renderers get restarted; anything else is
                               reported by name.
  4. STUCK system daemons   -- classified by PATH, not by name (see SYSTEM_PATH_MARKS).
                               Ours -> restarted; root-owned -> reported with a ready
                               `sudo kill <pid>` line. Never touches SYSTEM_NEVER.
  5. ZOMBIES (state Z)      -- cannot be killed from outside (the parent must reap them).
                               REPORTED with the parent's name, which is the actual cure.

THE ONE RULE THAT MATTERS: `ps %CPU` is an average over the process's whole LIFETIME, so it
lies about "burning right now". Every verdict here is based on the DELTA of two CPU-time
snapshots SAMPLE_SEC apart (cpu_delta_map). We shipped a false "the antivirus has been stuck
for 5 days" alarm off `ps %CPU` = 91.4% -- the delta showed 10:10.83 -> 10:10.83, i.e. asleep.

SAFETY RAILS:
  - ALLOWLIST mac_patrol_allow.txt (same dir): never killed
    (launchd:<label> / systemd:<unit> / cmdline substring). Long-lived daemons of your own
    look exactly like orphans (services have PPID=1) -- list them here;
  - never touches our own PID chain (walks ppid up to init);
  - only the classes above are killed; everything else is report-only;
  - --dry-run prints the kill list and kills nothing (this is what the test drives).

ALARM: a healthy run is SILENT. Findings print one compact line, and if MAC_PATROL_ALARM_CMD
is set it is run with that line as its last argument (pipe it to Slack/Telegram/notify-send).

EXIT CODES:
  0 = clean, nothing found        1 = found and handled
  3 = found but the alarm FAILED  4 = the patrol itself crashed (crash-guard)

Usage:
  python3 mac_patrol.py             # patrol + fix + alarm
  python3 mac_patrol.py --dry-run   # patrol, print what WOULD be killed, change nothing
  python3 mac_patrol.py --no-bus    # patrol + fix, skip the alarm
  python3 mac_patrol.py --install   # install the 30-min schedule (launchd/cron/schtasks)
  python3 mac_patrol.py --verify    # exit 0 iff the schedule exists AND ran in the last 2h

Repair passport (written for the weakest repairman): ONE file, stdlib only, no dependencies.
Misfiring? Run --dry-run to see every decision. Delete the state file to reset. All thresholds
are the UPPERCASE constants right below this docstring. Tests: test_mac_patrol.py.

MIT licensed. Distilled from the live fleet of the Palo Alto AI Research Lab.
"""

import os, re, sys, json, time, signal, socket, subprocess, datetime

IS_WIN = (os.name == "nt")
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
def _host():
    """Stable node name. Used only to shard the log/state filenames."""
    raw = (os.environ.get("MAC_PATROL_NODE") or os.environ.get("COMPUTERNAME")
           or socket.gethostname() or "unknown")
    return raw.split(".")[0]


HOST = _host()
try:
    import getpass
    ME = getpass.getuser()          # we may restart a process only if WE own it
except Exception:
    ME = os.environ.get("USER") or os.environ.get("USERNAME") or ""
LOG = os.path.join(SCRIPTS, "_mac_patrol_%s.log" % HOST)
STATE = os.path.join(SCRIPTS, "_mac_patrol_state_%s.json" % HOST)

MAX_SESSION_H = 24          # hard ceiling: session older than this = kill even w/o CPU measure
IDLE_SESSION_H = 5          # older than this AND idle -> close it; sessions are resumable
IDLE_PCT = 10.0             # an idle session still ticks at ~4-5% (measured 01.08, 20 sessions);
                            # a genuinely working one burns 30-100%+. Below this = sleeping.
                            # Needs an honest delta measure -- on Windows (no cpu_delta_map)
                            # only the 24h hard ceiling applies, so night robots survive.
CPU_HOG_PCT = 40.0          # burning this share of a core across the sample = "hot"
SWAP_WARN_PCT = 85          # swap fuller than this -> include in alarm
SAMPLE_SEC = 15             # gap between the two CPU-time snapshots (see cpu_delta_map)
STUCK_AGE_H = 12            # hot AND older than this = STUCK, not merely busy
SESSION_FLOOD = 20          # more live claude sessions than this at once = report it
MCP_BLOAT_MB = 700          # an MCP server fatter than this = report it (measured 01.08:
                            # telegram-mcp grows to ~900 MB per session; report-only for now,
                            # kill only after a shadow period proves nothing live breaks)
# GUI renderers that are safe to kill: the app repaints its window, chats live server-side.
# ROOT FIX 2026-07-29: a Claude renderer burned a core for 4 days 16 h (91 min of CPU time)
# and the patrol never saw it -- it watched only orphans + old sessions, and read `ps` %CPU,
# which on macOS is the AVERAGE over the whole life of the process, so a long-running hog
# looks mild while a 20-second-old process looks catastrophic. Hence cpu_delta_map below.
RESTARTABLE_HOG = ("Claude Helper (R",)

# ---- SYSTEM TIER: clean up OS daemons too, carefully ----
# A stuck system daemon is its own class. You cannot kill it blindly, but you also cannot let
# it burn a core for days. Three buckets, and the border is WHO OWNS the process:
#   * SYSTEM_NEVER      -- killing it costs you the session / audio / input. Report only, ever.
#   * SYSTEM_SAFE_KILL  -- services macOS relaunches on first demand (indexers, spellchecker,
#                          iCloud sync). Killed ONLY if the owner is us: anything root-owned
#                          needs sudo, and this robot never types a password.
#   * everything else   -- reported WITH a ready-to-paste `sudo kill <pid>` line for a human.
# Its own threshold (SYSTEM_STUCK_H = 2h, not the 12h used for a GUI renderer): a system
# daemon that has burned a core for two hours is not "busy", it is in a loop.
SYSTEM_STUCK_H = 2
# "system" is decided by PATH, never by a list of names -- a name list is always incomplete.
# Measured: airportd burned a core for an hour and the first version of this tier did not
# recognise it, simply because its name was missing. A path under /System, /usr/libexec or
# /Library/Apple cannot be faked.
SYSTEM_PATH_MARKS = ("/system/library", "/usr/libexec/", "/library/apple/",
                     "/system/applications/", "/usr/sbin/", "/system/library/coreservices")
SYSTEM_NEVER = ("WindowServer", "kernel_task", "launchd", "loginwindow", "coreaudiod",
                "hidd", "opendirectoryd", "securityd", "configd", "diskarbitrationd",
                "SystemUIServer", "Finder", "Dock")
SYSTEM_SAFE_KILL = ("AppleSpell", "mdworker", "mds_stores", "photoanalysisd", "suggestd",
                    "searchpartyd", "sharingd", "bird", "cloudd", "TextInputMenuAgent",
                    "spotlightknowledged", "mediaanalysisd", "corespotlightd",
                    "XprotectService", "com.apple.quicklook", "QuickLookUIService")
MCP_PATTERNS = [            # cmdline substrings of session-child MCP servers (orphan class)
    "telegram-mcp/main.py",
    "whatsapp-mcp-server/main.py",
    re.compile(r"[\\/]mcp[\\/].+main\.py"),   # generic: anything under a /mcp/ dir
]
CLAUDE_BIN = ("claude", "claude.exe")
ALLOW_FILE = os.path.join(SCRIPTS, "mac_patrol_allow.txt")


def load_allow(path=None, _run=None):
    """Allowlist -> (substrings, protected_pids). NEVER killed by the patrol.

    ROOT FIX 2026-08-01: the telegram SSE daemon (launchd service) has PPID=1 -- the exact
    signature of an orphan MCP -- so the patrol SIGTERM'd our own shield every 30 min and
    KeepAlive silently resurrected it (connection churn, Telegram throttling risk). A daemon
    manager's children are not orphans. Long-lived sessions
    doing real work (e.g. the 03 bus sync) need a standing exception list.

    Line formats in mac_patrol_allow.txt (unknown services resolve to
    nothing on nodes that lack them, so one file serves every node):
      # comment
      launchd:<label>    macOS: service PID looked up via launchctl -- survives restarts
      systemd:<unit>     Linux: service PID looked up via systemctl
      <substring>        any process whose cmdline contains it (case-insensitive, / and \\ equal)
    """
    run = _run or subprocess.run
    subs, pids = [], set()
    try:
        with open(path or ALLOW_FILE, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh]
    except Exception:
        return subs, pids
    for ln in lines:
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith("launchd:"):
            if sys.platform == "darwin" or _run:
                try:
                    out = run(["launchctl", "list", ln[8:]], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10).stdout or ""
                    m = re.search(r'"PID"\s*=\s*(\d+)', out)
                    if m:
                        pids.add(int(m.group(1)))
                except Exception:
                    pass
        elif ln.startswith("systemd:"):
            if (not IS_WIN and sys.platform != "darwin") or _run:
                try:
                    out = (run(["systemctl", "show", "-p", "MainPID", "--value", ln[8:]],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=10).stdout or "").strip()
                    if out.isdigit() and int(out) > 0:
                        pids.add(int(out))
                except Exception:
                    pass
        else:
            subs.append(ln.lower())
    return subs, pids


def _match_mcp(cmd):
    for p in MCP_PATTERNS:
        if isinstance(p, str):
            if p in cmd.replace("\\", "/"):
                return True
        elif p.search(cmd):
            return True
    return False


def _etime_to_sec(etime):
    """ps etime: [[dd-]hh:]mm:ss -> seconds."""
    try:
        days = 0
        t = etime.strip()
        if "-" in t:
            d, t = t.split("-", 1)
            days = int(d)
        parts = [int(x) for x in t.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, s = parts
        return days * 86400 + h * 3600 + m * 60 + s
    except Exception:
        return 0


def snapshot_posix(text=None):
    """Return list of dicts for every process. `text` injectable for tests."""
    if text is None:
        text = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,state=,pcpu=,rss=,etime=,user=,ucomm=,command="],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30).stdout
    procs = []
    for line in text.splitlines():
        f = line.split(None, 8)
        if len(f) < 9:
            continue
        try:
            procs.append(dict(pid=int(f[0]), ppid=int(f[1]), state=f[2],
                              cpu=float(f[3]), rss=int(f[4]),
                              age=_etime_to_sec(f[5]), user=f[6], name=f[7], cmd=f[8]))
        except ValueError:
            continue
    return procs


def snapshot_windows(text=None):
    """Windows via PowerShell CIM. `text` injectable for tests (JSON array)."""
    if text is None:
        # force UTF-8 both sides: the console default (cp866/cp1251) breaks decoding on
        # any process whose cmdline holds non-ASCII -- measured on the hub 2026-07-29.
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,"
              "Name,CommandLine,WorkingSetSize,CreationDate | ConvertTo-Json -Compress")
        text = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60).stdout
    now = time.time()
    procs = []
    try:
        rows = json.loads(text or "[]")
    except Exception:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    for r in rows:
        try:
            cd = str(r.get("CreationDate") or "")
            born = now
            m = re.search(r"\((\d+)\)", cd)               # /Date(1690000000000)/
            if m:
                born = int(m.group(1)) / 1000.0
            else:                                          # CIM DMTF: 20260729074500.000000+060
                m = re.match(r"(\d{14})", cd)
                if m:
                    born = time.mktime(time.strptime(m.group(1), "%Y%m%d%H%M%S"))
                else:                                      # ISO-ish datetime string
                    m = re.match(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})", cd)
                    if m:
                        born = time.mktime(time.strptime(m.group(1).replace("T", " "),
                                                         "%Y-%m-%d %H:%M:%S"))
            procs.append(dict(pid=int(r["ProcessId"]), ppid=int(r.get("ParentProcessId") or 0),
                              state="?", cpu=0.0,
                              rss=int(r.get("WorkingSetSize") or 0) // 1024,
                              age=max(0, int(now - born)), user="",
                              name=str(r.get("Name") or ""), cmd=str(r.get("CommandLine") or "")))
        except Exception:
            continue
    return procs


def _cputime_map():
    """{pid: cumulative CPU seconds} -- the only honest basis for "is it burning NOW"."""
    if IS_WIN:
        return {}
    out = subprocess.run(["ps", "-Ao", "pid=,time="], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=30).stdout
    d = {}
    for ln in out.splitlines():
        f = ln.split()
        if len(f) < 2:
            continue
        try:
            parts = [float(x) for x in f[1].replace("-", ":").split(":")]
            sec = 0.0
            for x in parts:
                sec = sec * 60 + x
            d[int(f[0])] = sec
        except ValueError:
            continue
    return d


def cpu_delta_map(sample_sec=None, _before=None, _after=None):
    """Percent of ONE core each pid burned across the sample window.

    `ps` %CPU averages over the process lifetime, so a process stuck since Tuesday reads as
    mild while a fresh one reads as 150%. Two snapshots of cumulative CPU-time do not lie.
    _before/_after are injectable for tests.
    """
    if _before is None:
        if IS_WIN:
            return {}          # Windows: no delta measure -- honestly EMPTY, not a fake zero
        _before = _cputime_map()
        time.sleep(sample_sec or SAMPLE_SEC)
        _after = _cputime_map()
    gap = float(sample_sec or SAMPLE_SEC)
    out = {}
    for pid, aft in _after.items():
        bef = _before.get(pid)
        if bef is None:
            continue
        out[pid] = max(0.0, (aft - bef)) / gap * 100.0
    return out


def swap_pct_posix():
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "vm.swapusage"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10).stdout
            tot = re.search(r"total = ([\d.]+)M", out)
            used = re.search(r"used = ([\d.]+)M", out)
            if tot and used and float(tot.group(1)) > 0:
                return round(100.0 * float(used.group(1)) / float(tot.group(1)))
        else:                                              # Linux: /proc/meminfo
            mi = {}
            with open("/proc/meminfo") as fh:
                for ln in fh:
                    k, _, v = ln.partition(":")
                    mi[k.strip()] = v
            tot = int(mi.get("SwapTotal", "0 kB").split()[0])
            free = int(mi.get("SwapFree", "0 kB").split()[0])
            if tot > 0:
                return round(100.0 * (tot - free) / tot)
    except Exception:
        pass
    return None


def self_chain(procs):
    """PIDs of our own ancestry -- NEVER kill these."""
    by_pid = {p["pid"]: p for p in procs}
    chain, pid, hops = set(), os.getpid(), 0
    while pid and pid in by_pid and hops < 20:
        chain.add(pid)
        pid = by_pid[pid]["ppid"]
        hops += 1
    chain.add(os.getpid())
    try:
        chain.add(os.getppid())   # direct parent even if ps snapshot missed it
    except Exception:
        pass
    return chain


def patrol(procs, protect, cpu_now=None, allow=None):
    """Pure decision logic (unit-testable): returns (kill_list, findings dict)."""
    pids = {p["pid"] for p in procs}
    by_pid = {p["pid"]: p for p in procs}
    kill, findings = [], {"orphan_mcp": 0, "stale_claude": 0, "zombies": [], "hogs": [],
                          "stuck_killed": [], "sessions": 0, "mcp_bloat": [], "spared": 0,
                          "system_stuck": [], "system_needs_hands": []}
    hogs = []
    cpu_now = cpu_now or {}
    allow_subs, allow_pids = allow if allow else ([], set())
    findings["sessions"] = sum(1 for p in procs if p["name"] in CLAUDE_BIN)
    for p in procs:
        if p["pid"] in protect:
            continue
        # allowlist spares from KILLING only -- zombies/hogs/bloat still get reported,
        # so a protected daemon that misbehaves stays visible instead of invisible.
        spared = (p["pid"] in allow_pids
                  or any(s in p["cmd"].lower().replace("\\", "/") for s in allow_subs))
        if spared:
            findings["spared"] += 1
        orphaned = (p["ppid"] == 1) if not IS_WIN else (p["ppid"] not in pids)
        if _match_mcp(p["cmd"]) and "ython" in p["name"] + p["cmd"]:
            if orphaned and not spared:
                kill.append((p["pid"], "orphan-mcp", p["name"]))
                findings["orphan_mcp"] += 1
                continue
            # NOT orphaned but bloated: report only. Measured 01.08: 34 MCP servers hang
            # directly off the live Claude.app -- killing those blindly severs a live chat.
            if p["rss"] > MCP_BLOAT_MB * 1024:
                parent = by_pid.get(p["ppid"], {}).get("name", "?")
                findings["mcp_bloat"].append(
                    "%s pid=%d %dMB (parent %s)"
                    % (os.path.basename(p["cmd"].split()[-1])[:40] if p["cmd"] else p["name"],
                       p["pid"], p["rss"] // 1024, parent))
            # a live MCP can still be a CPU hog -- deliberately falls into the checks below
        if p["name"] in CLAUDE_BIN and p["age"] > IDLE_SESSION_H * 3600 and not spared:
            burn = cpu_now.get(p["pid"]) if cpu_now else None
            if burn is not None and burn < IDLE_PCT:
                kill.append((p["pid"], "idle-claude %dh %.0f%%" % (p["age"] // 3600, burn),
                             p["name"]))
                findings["stale_claude"] += 1
            elif p["age"] > MAX_SESSION_H * 3600 and (burn is None or burn < IDLE_PCT):
                kill.append((p["pid"], "stale-claude %dh" % (p["age"] // 3600), p["name"]))
                findings["stale_claude"] += 1
        elif p["state"].startswith("Z"):
            parent = by_pid.get(p["ppid"], {}).get("name", "?")
            findings["zombies"].append("%s (parent %s pid=%d -- restart THAT to reap it)" % (p["name"], parent, p["ppid"]))
        else:
            # measured burn, NOT ps %CPU (which is a lifetime average -- see cpu_delta_map)
            burn = cpu_now.get(p["pid"])
            if burn is None or not cpu_now:
                burn = p["cpu"]
            if burn < CPU_HOG_PCT or p["age"] <= 600:
                continue
            hot_for_hours = p["age"] > STUCK_AGE_H * 3600
            restartable = any(tag in p["name"] or tag in p["cmd"] for tag in RESTARTABLE_HOG)
            # --- system tier: a stuck OS daemon (not our process, not a session) ---
            sysname = p["name"] + " " + p["cmd"]
            low = sysname.lower()
            is_system = any(m in low for m in SYSTEM_PATH_MARKS)
            if (not spared and not restartable and is_system
                    and p["age"] > SYSTEM_STUCK_H * 3600
                    and not any(t in sysname for t in SYSTEM_NEVER)):
                # We restart ONLY what we own AND only what is on the safe list: another
                # uid needs sudo (this robot never types a password), and a system service
                # outside the list may be the only thing holding the session -> report it.
                if p.get("user") == ME and any(t in sysname for t in SYSTEM_SAFE_KILL):
                    kill.append((p["pid"], "stuck-system %dh %.0f%%" % (p["age"] // 3600, burn),
                                 p["name"]))
                    findings["system_stuck"].append(
                        "%s pid=%d (%dh, %.0f%%) -- restarted" % (p["name"], p["pid"],
                                                                   p["age"] // 3600, burn))
                else:
                    own = p.get("user") or "?"
                    fix = ("kill %d" % p["pid"]) if own == ME else ("sudo kill %d" % p["pid"])
                    findings["system_needs_hands"].append(
                        "%s pid=%d (%dh, %.0f%%, owner %s) -> %s"
                        % (p["name"], p["pid"], p["age"] // 3600, burn, own, fix))
                continue
            if hot_for_hours and restartable and not spared:
                # stuck GUI renderer: killing it makes the app repaint, chats are server-side
                kill.append((p["pid"], "stuck-renderer %dh %.0f%%" % (p["age"] // 3600, burn),
                             p["name"]))
                findings["stuck_killed"].append("%s pid=%d (%dh, %.0f%%)"
                                                % (p["name"], p["pid"], p["age"] // 3600, burn))
            else:
                mark = " STUCK %dh" % (p["age"] // 3600) if hot_for_hours else ""
                hogs.append((burn, "%s pid=%d %.0f%%cpu%s"
                             % (p["name"], p["pid"], burn, mark)))
    findings["hogs"] = [h for _, h in sorted(hogs, reverse=True)[:3]]
    return kill, findings


def _same_process(pid, name):
    """PID-reuse guard: is `pid` still the process we snapshotted (same binary name)?"""
    try:
        out = subprocess.run(["ps", "-o", "ucomm=", "-p", str(pid)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10).stdout.strip()
        return out == name
    except Exception:
        return False


def do_kill(kill_list, dry):
    done = 0
    for pid, why, name in kill_list:
        if dry:
            print("DRY would kill %d (%s, %s)" % (pid, name, why))
            continue
        try:
            if IS_WIN:
                r = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   capture_output=True, timeout=15)
                done += 1 if r.returncode == 0 else 0
            else:
                os.kill(pid, signal.SIGTERM)
                done += 1
        except Exception:
            pass
    if not dry and done and not IS_WIN:
        time.sleep(3)   # let SIGTERM land; survivors get SIGKILL -- only if STILL the same binary
        for pid, why, name in kill_list:
            try:
                os.kill(pid, 0)
                if _same_process(pid, name):
                    os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    return done


PLIST = os.path.expanduser("~/Library/LaunchAgents/com.paloaltoresearchlab.mac-patrol.plist")
PLIST_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.paloaltoresearchlab.mac-patrol</string>
  <key>ProgramArguments</key><array>
    <string>%s</string>
    <string>%s</string>
  </array>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardErrorPath</key><string>%s</string>
</dict></plist>
"""


def install():
    """Install the per-OS 30-min schedule for THIS node. Idempotent."""
    me = os.path.abspath(__file__)
    if IS_WIN:
        cmd = ["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "30",
               "/TN", "Mac Patrol", "/TR",
               '"%s" "%s"' % (sys.executable or "python", me)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        print(r.stdout.strip() or r.stderr.strip())
        return 0 if r.returncode == 0 else 3
    if sys.platform == "darwin":
        py = sys.executable or "/usr/bin/python3"
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        with open(PLIST, "w", encoding="utf-8") as fh:
            fh.write(PLIST_BODY % (py, me, os.path.join(SCRIPTS, "_mac_patrol_launchd.err")))
        subprocess.run(["launchctl", "unload", PLIST], capture_output=True, timeout=30)
        r = subprocess.run(["launchctl", "load", PLIST], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        print("launchd loaded" if r.returncode == 0 else "launchctl load failed: " + r.stderr)
        return 0 if r.returncode == 0 else 3
    # Linux: cron
    tag = "# mac-patrol"
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, errors="replace").stdout
    if tag not in cur:
        py = sys.executable or "/usr/bin/python3"
        line = "*/30 * * * * '%s' '%s' >/dev/null 2>&1 %s\n" % (py, me, tag)
        p = subprocess.run(["crontab", "-"], input=cur + line, text=True)
        print("cron installed" if p.returncode == 0 else "crontab failed")
        return 0 if p.returncode == 0 else 3
    print("cron already installed")
    return 0


def verify():
    """Machine verify (deploy gate): schedule exists AND state file written in last 2h -> 0."""
    ok_sched = False
    if IS_WIN:
        r = subprocess.run(["schtasks", "/Query", "/TN", "Mac Patrol"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        ok_sched = r.returncode == 0
    elif sys.platform == "darwin":
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        ok_sched = "com.paloaltoresearchlab.mac-patrol" in r.stdout
    else:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, errors="replace").stdout
        ok_sched = "mac-patrol" in cur
    ok_state = os.path.exists(STATE) and (time.time() - os.path.getmtime(STATE)) < 7200
    print("schedule=%s state_fresh=%s" % (ok_sched, ok_state))
    return 0 if (ok_sched and ok_state) else 2


def main(argv):
    if os.environ.get("MAC_PATROL_SELFTEST_CRASH"):
        raise RuntimeError("injected crash (crash-guard selftest)")   # gate: must exit 4
    if "--install" in argv:
        code = install()
        if code == 0:
            code = main([a for a in argv if a != "--install"] + ["--no-bus"]) or 0
            return 0 if code in (0, 1) else code
        return code
    if "--verify" in argv:
        return verify()
    dry = "--dry-run" in argv
    no_bus = "--no-bus" in argv or dry
    procs = snapshot_windows() if IS_WIN else snapshot_posix()
    if not procs:
        raise RuntimeError("empty process snapshot")
    protect = self_chain(procs)
    cpu_now = cpu_delta_map()          # two snapshots, SAMPLE_SEC apart -- honest "burning now"
    kill_list, f = patrol(procs, protect, cpu_now, allow=load_allow())
    killed = do_kill(kill_list, dry)
    swap = None if IS_WIN else swap_pct_posix()

    dirty = bool(kill_list or f["zombies"] or f["hogs"] or f["stuck_killed"]
                 or f.get("mcp_bloat") or f.get("system_stuck") or f.get("system_needs_hands")
                 or f["sessions"] > SESSION_FLOOD or (swap and swap >= SWAP_WARN_PCT))
    msg = ""
    if dirty:
        bits = []
        if f["orphan_mcp"]:
            bits.append("orphan-MCP killed: %d" % f["orphan_mcp"])
        if f["stale_claude"]:
            bits.append("sessions closed (idle>%dh or >%dh): %d -- resumable from transcripts"
                        % (IDLE_SESSION_H, MAX_SESSION_H, f["stale_claude"]))
        if f["zombies"]:
            bits.append("zombies (restart the PARENT to reap): %s" % ",".join(sorted(set(f["zombies"]))[:3]))
        if f["stuck_killed"]:
            bits.append("stuck-and-restarted: " + "; ".join(f["stuck_killed"]))
        if f["sessions"] > SESSION_FLOOD:
            bits.append("concurrent sessions: %d (>%d -- each drags its own MCP servers; close the idle ones)"
                        % (f["sessions"], SESSION_FLOOD))
        if f.get("mcp_bloat"):
            bits.append("bloated MCP >%dMB (%d of them): %s"
                        % (MCP_BLOAT_MB, len(f["mcp_bloat"]), "; ".join(f["mcp_bloat"][:3])))
        if f.get("system_stuck"):
            bits.append("system daemons restarted: " + "; ".join(f["system_stuck"]))
        if f.get("system_needs_hands"):
            bits.append("stuck root daemons (need YOUR hands, see sudo line): "
                        + "; ".join(f["system_needs_hands"][:3]))
        if f["hogs"]:
            bits.append("CPU hogs: " + "; ".join(f["hogs"]))
        if swap is not None and swap >= SWAP_WARN_PCT:
            bits.append("swap %d%% -- if it feels slow, restart the agent app" % swap)
        msg = "PATROL %s: %s" % (HOST, " | ".join(bits))

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state = dict(ts=now, host=HOST, dirty=dirty, killed=killed, swap_pct=swap,
                 dry_run=dry, **{k: v for k, v in f.items()})
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("%s %s\n" % (now, msg if dirty else "clean (procs=%d)" % len(procs)))
    print(msg if dirty else "clean: %d procs, swap=%s%%" % (len(procs), swap))

    if not dirty:
        return 0
    if no_bus:
        return 1
    hook = os.environ.get("MAC_PATROL_ALARM_CMD")
    if not hook:
        return 1
    try:
        r = subprocess.run(hook.split() + [msg], capture_output=True, text=True, timeout=120)
        return 1 if r.returncode == 0 else 3
    except Exception:
        return 3


if __name__ == "__main__":
    # CRASH-GUARD: our own death must exit 4, never masquerade as 1 (found-and-handled).
    try:
        sys.exit(main(sys.argv[1:]))
    except BaseException as e:
        if isinstance(e, SystemExit):
            raise
        try:
            with open(LOG, "a", encoding="utf-8") as fh:
                fh.write("%s CRASH: %r\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), e))
        except Exception:
            pass
        sys.exit(4)
