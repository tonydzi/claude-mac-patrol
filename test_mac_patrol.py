# -*- coding: utf-8 -*-
"""test_mac_patrol.py -- offline test of mac_patrol decision logic (fake snapshots, no kills).

Covers: orphan-MCP detection, stale-claude detection, self-chain protection, zombie report,
CPU-hog report, etime parsing, and the crash-guard (injected crash must exit 4).
Run: python3 test_mac_patrol.py   -> exit 0 = all green, prints PASS/FAIL per case.
"""
import os, sys, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mac_patrol as pp

FAILS = []
TOTAL = [0]


def check(name, cond):
    TOTAL[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


ME = getattr(__import__("mac_patrol"), "ME", "dev") or "dev"
# columns: pid ppid state pcpu rss etime USER ucomm command
# (USER matters: the system tier decides by OWNER -- ours gets restarted, root gets a line)
FAKE_PS = ("""\
  100     1 S   2.0  1000 03-01:00:00 %(me)s Python /usr/bin/python3 /Users/dev/mcp/telegram-mcp/main.py /Users/dev/TG-Media
  101   200 S   1.0  1000    01:00:00 %(me)s Python /usr/bin/python3 /Users/dev/mcp/telegram-mcp/main.py /Users/dev/TG-Media
  102     1 S   0.0   500 01-02:00:00 %(me)s Python /usr/bin/python3 /Users/dev/mcp/whatsapp-mcp/whatsapp-mcp-server/main.py
  200   468 S   3.0  2000 02-05:00:00 %(me)s claude /path/claude --output-format stream-json
  201   468 S   3.0  2000    02:00:00 %(me)s claude /path/claude --output-format stream-json
  300   468 Z   0.0     0    05:00:00 %(me)s aw-watcher-windo aw-watcher-window
  400     1 R  95.0  9000    01:00:00 %(me)s Python /usr/bin/python3 /Users/dev/work/embed_update.py
  500     1 S   1.0   100 05-00:00:00 %(me)s syncthing /usr/local/bin/syncthing
  103   200 S   1.0 819200    04:00:00 %(me)s Python /usr/bin/python3 /Users/dev/mcp/telegram-mcp/main.py /Users/dev/TG-Media
""" % {"me": ME})


def run():
    procs = pp.snapshot_posix(FAKE_PS)
    check("parse: 9 rows", len(procs) == 9)
    check("etime dd-hh parse", next(p for p in procs if p["pid"] == 100)["age"] == 3 * 86400 + 3600)

    kill, f = pp.patrol(procs, protect=set())
    kp = {pid for pid, _, _ in kill}
    check("orphan telegram-mcp (100) killed", 100 in kp)
    check("orphan whatsapp-mcp (102) killed", 102 in kp)
    check("parented mcp (101) NOT killed", 101 not in kp)
    check("stale claude 2d (200) killed", 200 in kp)
    check("fresh claude 2h (201) NOT killed", 201 not in kp)
    check("zombie reported with parent name",
          300 not in kp and len(f["zombies"]) == 1 and "aw-watcher-windo" in f["zombies"][0]
          and "parent" in f["zombies"][0])
    check("cpu hog reported not killed", 400 not in kp and any("400" in h for h in f["hogs"]))
    check("old syncthing (500) untouched", 500 not in kp)
    check("bloated MCP with a LIVE parent (103) is reported, never killed",
          103 not in kp and any("103" in b and "800MB" in b for b in f["mcp_bloat"]))
    check("a normal-size MCP (101) stays out of the bloat report",
          not any("pid=101 " in b for b in f["mcp_bloat"]))

    # the idle rule: older than IDLE_SESSION_H AND asleep (measured <10%) -> close it;
    # older but genuinely working -> leave alone; no delta measure (Windows) -> only the 24h cap
    idle6 = dict(pid=600, ppid=468, state="S", cpu=4.0, rss=200000, age=6*3600,
                 name="claude", cmd="/path/claude --output-format stream-json")
    busy6 = dict(idle6, pid=601)
    busy30 = dict(idle6, pid=602, age=30*3600)
    k5, _ = pp.patrol([idle6, busy6, busy30], protect=set(),
                      cpu_now={600: 4.0, 601: 85.0, 602: 85.0})
    kp5 = {pid for pid, _, _ in k5}
    check("idle 6h session (600) closed on the measurement", 600 in kp5)
    check("working 6h session (601) survives", 601 not in kp5)
    check("working 30h session (602) survives while it burns", 602 not in kp5)
    k5w, _ = pp.patrol([idle6, busy30], protect=set(), cpu_now={})
    kp5w = {pid for pid, _, _ in k5w}
    check("no delta measure: 6h survives, 30h hits the hard cap", 600 not in kp5w and 602 in kp5w)

    # --- allowlist (root fix: the patrol kept killing our own launchd daemon -- services have ppid=1) ---
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write("# comment\nlaunchd:com.example.my-daemon\nsystemd:tg-sse\nbus_watch.py\n")
        allow_path = tf.name

    class FakeRun:
        def __call__(self, cmd, **kw):
            class R: pass
            r = R()
            r.stdout = '{ "PID" = 100; }' if cmd[0] == "launchctl" else "102\n"
            return r
    subs, keep = pp.load_allow(allow_path, _run=FakeRun())
    os.unlink(allow_path)
    check("allow: cmdline substring parsed", subs == ["bus_watch.py"])
    check("allow: launchd PID=100 and systemd PID=102 resolved", keep == {100, 102})

    ak, af = pp.patrol(procs, protect=set(), allow=(subs, keep))
    akp = {pid for pid, _, _ in ak}
    check("allow: our daemon (100, ppid=1) NOT killed", 100 not in akp)
    check("allow: systemd unit (102) NOT killed", 102 not in akp)
    check("allow: spared processes are counted", af["spared"] >= 2)
    check("allow: an unrelated stale session (200) is still killed", 200 in akp)
    guard = dict(pid=700, ppid=468, state="S", cpu=4.0, rss=100000, age=9 * 3600,
                 name="claude", cmd="/path/claude -p --resume bus_watch.py sync")
    gk, _ = pp.patrol([guard], protect=set(), cpu_now={700: 3.0}, allow=(subs, keep))
    check("allow: an idle session matching the allowlist survives", not gk)
    check("allow: missing file -> empty allowlist", pp.load_allow("/no/such") == ([], set()))

    kill2, _ = pp.patrol(procs, protect={100, 200})
    kp2 = {pid for pid, _, _ in kill2}
    check("self-chain protected", 100 not in kp2 and 200 not in kp2)

    # dry-run must not kill anything real
    n = pp.do_kill([(99999999, "test", "ghost")], dry=True)
    check("dry-run kills nothing", n == 0)

    # Windows CreationDate: DMTF + ISO + /Date()/ all parse to a real age
    import json as _json, time as _time
    old_dmtf = _time.strftime("%Y%m%d%H%M%S", _time.localtime(_time.time() - 3 * 86400))
    win = _json.dumps([
        {"ProcessId": 10, "ParentProcessId": 4, "Name": "claude.exe",
         "CommandLine": "claude.exe --output-format stream-json",
         "WorkingSetSize": 1000, "CreationDate": old_dmtf + ".000000+060"},
        {"ProcessId": 11, "ParentProcessId": 4, "Name": "claude.exe",
         "CommandLine": "claude.exe", "WorkingSetSize": 1000,
         "CreationDate": "/Date(%d)/" % int((_time.time() - 3600) * 1000)},
    ])
    wp = pp.snapshot_windows(win)
    check("win DMTF date -> ~3d age", abs(wp[0]["age"] - 3 * 86400) < 7200)
    check("win /Date()/ -> ~1h age", abs(wp[1]["age"] - 3600) < 600)

    # hogs are sorted by CPU descending, capped at 3
    many = pp.snapshot_posix("\n".join(
        "  9%02d     1 R  %d.0  100    01:00:00 %s hog%d /bin/hog%d" % (i, 80 + i, ME, i, i)
        for i in range(5)))
    _, fh = pp.patrol(many, protect=set())
    check("hogs sorted desc, top-3", len(fh["hogs"]) == 3 and "84%cpu" in fh["hogs"][0])

    # host naming: the state filename must be STABLE between an interactive run and a
    # scheduled one. Under launchd/cron the environment is EMPTY, so a node that names
    # itself from env vars ends up with two state files -- and a freshness watchdog then
    # watches the one you refresh by hand while the robot's copy silently goes stale.
    check("HOST resolved, not 'unknown'", pp.HOST and pp.HOST.lower() != "unknown")
    check("HOST used in state path", pp.HOST in pp.STATE and pp.HOST in pp.LOG)
    check("HOST has no domain suffix", "." not in pp.HOST)

    # --- burn is measured by DELTA, never by ps %CPU (which lies about long-lived processes) ---
    d = pp.cpu_delta_map(sample_sec=10, _before={1: 100.0, 2: 50.0, 3: 0.0},
                         _after={1: 110.0, 2: 50.0, 3: 5.0})
    check("cpu_delta: 10s of CPU in a 10s window = 100%", abs(d[1] - 100.0) < 0.1)
    check("cpu_delta: consumed nothing = 0%", d[2] == 0.0)
    check("cpu_delta: half a core = 50%", abs(d[3] - 50.0) < 0.1)

    # stuck renderer: hot AND old -> killed (the app just repaints its window)
    stuck = pp.snapshot_posix(
        "  744   468 R  12.0  800000 04-16:35:46 " + ME + " Claude Helper (R /Applications/Claude.app/"
        "Contents/Frameworks/Claude Helper (Renderer).app/Contents/MacOS/Claude Helper (Renderer)\n"
        "  900   468 R  99.0  100000    00:30:00 " + ME + " Claude Helper (R fresh renderer\n"
        "  901     1 R  99.0  100000 03-00:00:00 root kernel_task /kernel\n")
    k, fs = pp.patrol(stuck, protect=set(), cpu_now={744: 95.0, 900: 95.0, 901: 95.0})
    kp = {pid for pid, _, _ in k}
    check("stuck renderer (4 days, burning) killed", 744 in kp)
    check("fresh renderer (30 min) NOT killed", 900 not in kp)
    check("someone else's system process is reported, never killed",
          901 not in kp and any("kernel_task" in h for h in fs["hogs"]))
    check("ps %CPU=12 did not fool it: the 95% delta decided", any("744" in x for x in fs["stuck_killed"]))

    # session flood: counted and signalled
    flood = pp.snapshot_posix("\n".join(
        "  %d   468 S  1.0  100000    01:00:00 %s claude /path/claude --x" % (2000 + i, ME)
        for i in range(pp.SESSION_FLOOD + 3)))
    _, ff = pp.patrol(flood, protect=set(), cpu_now={})
    check("session flood counted", ff["sessions"] == pp.SESSION_FLOOD + 3)

    # --- SYSTEM TIER: a stuck OS daemon; the verdict depends on the OWNER ---
    # 3h of burning (> SYSTEM_STUCK_H=2h) at 90% of a core. Ours -> restarted silently;
    # root-owned -> handed back as a ready `sudo kill` line for a human.
    sys_ps = (
        "  900     1 R  90.0  50000    03:00:00 %s AppleSpell"
        " /System/Library/Services/AppleSpell.service/Contents/MacOS/AppleSpell\n"
        "  901     1 R  90.0  50000    03:00:00 root airportd /usr/libexec/airportd\n"
        "  902     1 R  95.0  50000 05-00:00:00 _windowserver WindowServer"
        " /System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer\n"
        "  903     1 R  90.0  50000       30:00 %s mdworker"
        " /System/Library/Frameworks/CoreServices.framework/mdworker\n"
        % (ME, ME))
    sp = pp.snapshot_posix(sys_ps)
    check("rows with an owner column parse: 4 processes", len(sp) == 4)
    check("owner column read", next(p for p in sp if p["pid"] == 901)["user"] == "root")
    burn = {900: 90.0, 901: 90.0, 902: 95.0, 903: 90.0}
    ks, sf = pp.patrol(sp, protect=set(), cpu_now=burn)
    kpids = [k[0] for k in ks]
    check("our own stuck system daemon is restarted", 900 in kpids)
    check("root daemon NOT killed (we never type a password)", 901 not in kpids)
    check("root daemon handed back as a line", any("901" in s for s in sf["system_needs_hands"]))
    check("that line carries a ready-to-paste command",
          any("sudo kill" in s for s in sf["system_needs_hands"]))
    check("WindowServer untouched even when we own it (SYSTEM_NEVER)", 902 not in kpids)
    check("a young daemon (30 min < 2h) is untouched", 903 not in kpids)
    check("the restarted daemon shows up in the report", any("900" in s for s in sf["system_stuck"]))
    # tier border: a system daemon must not leak into the plain CPU-hog list, or vice versa
    check("our system daemon is not double-counted in hogs",
          not any("pid=900" in h for h in sf["hogs"]))
    # the allowlist outranks the system tier: listed -> not touched at all
    ks2, sf2 = pp.patrol(sp, protect=set(), cpu_now=burn, allow=(["applespell"], set()))
    check("allowlist spares a system daemon", 900 not in [k[0] for k in ks2])
    check("the spared one is counted", sf2["spared"] >= 1)

    # crash-guard: inject a REAL crash into a REAL run of the script -- its death must come
    # back as exit 4, never as 1 ("found and reported"). Runs on
    # every OS because it drives the actual file, not a monkeypatched copy.
    me = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mac_patrol.py")
    env = dict(os.environ, MAC_PATROL_SELFTEST_CRASH="1")
    r = subprocess.run([sys.executable, me, "--dry-run"], capture_output=True, text=True,
                       errors="replace", env=env, timeout=120)
    check("crash-guard: injected crash -> exit 4 (not 1)", r.returncode == 4)

    # and a clean run must never exit 4
    r2 = subprocess.run([sys.executable, me, "--dry-run"], capture_output=True, text=True,
                        errors="replace", timeout=180)
    check("clean dry-run exits 0 or 1, never 4", r2.returncode in (0, 1))

    print("\n%d/%d passed" % (TOTAL[0] - len(FAILS), TOTAL[0]))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(run())
