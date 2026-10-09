#!/usr/bin/env python3
"""Behavioural regression suite for netquality.

Usage: python3 tests/test_netquality.py [path/to/indicator.py]
Exits non-zero on the first failure.
"""
import importlib.util
import os
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "netquality", "indicator.py")
# indicator.py does `from netquality import __version__`, so its parent package must be
# importable -- whether PATH points into this checkout or into /usr/lib/python3/dist-packages.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(PATH))))

spec = importlib.util.spec_from_file_location("nq", PATH)
nq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nq)
opts = nq.parse_args([])

FAILED = []


def check(condition, message):
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if not condition:
        FAILED.append(message)


def window(lines):
    w = nq.Window(9999, opts.deadline)
    for line in lines:
        w.feed(line)
    return w


def level_of(lines):
    return nq.grade(window(lines).snapshot(), opts).level.name


def replies(n, ms, host="x"):
    return [f"64 bytes from {host}: icmp_seq={i} ttl=60 time={ms}.0 ms" for i in range(1, n + 1)]


print(f"=== {PATH}: parsing and classification ===")
real = window(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.txt"))).snapshot()
check(real.late == 7 and real.lost == 5 and real.duplicates == 2,
      f"real capture decomposes to late=7 lost=5 dup=2 (got late={real.late} "
      f"lost={real.lost} dup={real.duplicates})")
check(real.longest_bad_run == 11, f"longest bad run 11 (got {real.longest_bad_run})")
check(nq.grade(real, opts).level.name == "bad", "real capture grades bad")

late_reply = window(replies(19, 45) + ["no answer yet for icmp_seq=20",
                                       "64 bytes from x: icmp_seq=20 ttl=60 time=13276 ms"])
snap = late_reply.snapshot()
check(snap.late == 1 and snap.lost == 0,
      "a 13s late reply counts LATE, not LOST and not OK")
check(nq.grade(snap, opts).level.name == "fair", "one 13s freeze in 20 probes grades fair, not good")

print("\n=== burst vs scatter at identical loss ===")
scattered = [f"no answer yet for icmp_seq={i}" if i % 10 == 0
             else f"64 bytes from x: icmp_seq={i} ttl=60 time=45.0 ms" for i in range(1, 41)]
consecutive = [f"no answer yet for icmp_seq={i}" if 11 <= i <= 14
               else f"64 bytes from x: icmp_seq={i} ttl=60 time=45.0 ms" for i in range(1, 41)]
check(window(scattered).snapshot().longest_bad_run == 1, "scattered loss has run 1")
check(window(consecutive).snapshot().longest_bad_run == 4, "consecutive loss has run 4")
check(level_of(scattered) == "poor" and level_of(consecutive) == "bad",
      "same 10% loss grades poor when scattered, bad when consecutive")

print("\n=== grading boundaries ===")
for label, lines, want in (
    ("clean 25ms", replies(40, 25), "good"),
    ("steady 350ms", replies(40, 350), "poor"),
    ("blackout", [f"no answer yet for icmp_seq={i}" for i in range(1, 11)], "down"),
    ("1 drop in 50", [("no answer yet for icmp_seq=7" if i == 7 else
                       f"64 bytes from x: icmp_seq={i} ttl=60 time=25.0 ms")
                      for i in range(1, 51)], "fair"),
):
    got = level_of(lines)
    check(got == want, f"{label} -> {want} (got {got})")

print("\n=== state machine invariants ===")
w = nq.Window(9999, opts.deadline)
for i in range(1, 6):
    w.feed(f"64 bytes from x: icmp_seq={i} ttl=60 time=20.0 ms")
w.new_epoch()
for i in range(1, 6):
    w.feed(f"64 bytes from x: icmp_seq={i} ttl=60 time=20.0 ms")
check(w.snapshot().total == 10,
      f"ping restart does not collide seq numbers (got {w.snapshot().total}/10)")

empty = nq.Window(9999, opts.deadline).snapshot()
check(nq.grade(empty, opts).level is nq.UNKNOWN, "empty window grades UNKNOWN, not GOOD")
check(nq.UNKNOWN.rank < nq.FAIR.rank, "UNKNOWN ranks below FAIR so startup cannot alert")
check(nq.DOWN.rank > nq.BAD.rank > nq.POOR.rank > nq.FAIR.rank > nq.GOOD.rank,
      "level ranks are strictly ordered")

print("\n=== gateway is judged on local-link terms ===")


class _Verdict:
    _hop_verdict = nq.Indicator._hop_verdict
    opts = opts


for label, lines, expect in (
    # Too few samples must never produce a confident verdict: the first hop is probed
    # every 20 s by default, and on wifi a probe that infrequent measures the radio
    # waking up rather than the link.
    ("3 probes at 110ms", replies(3, 110, "gw"), "not enough to judge"),
    ("110ms", replies(30, 110, "gw"), "SLOW"),
    ("4ms", replies(30, 4, "gw"), "fine"),
    ("4ms +1 loss", [("no answer yet for icmp_seq=5" if i == 5 else
                      f"64 bytes from gw: icmp_seq={i} ttl=64 time=4.0 ms")
                     for i in range(1, 31)], "LOSING"),
):
    verdict = _Verdict._hop_verdict(_Verdict, window(lines).snapshot())
    check(expect in verdict, f"gateway {label} -> {verdict!r}")

# A verdict is only drawn where the sampling can support one: in calm mode the first hop
# is probed every 20 s, and a wifi hop answers an isolated probe far slower than a burst.
class _Mode:
    ALERT, CALM = "alert", "calm"
    _local_verdict = nq.Indicator._local_verdict
    _hop_verdict = "sentinel"

_Mode.mode = _Mode.ALERT
check(_Mode._local_verdict(_Mode) == "sentinel", "alert mode draws a local-link verdict")
_Mode.mode = _Mode.CALM
check(_Mode._local_verdict(_Mode) is None, "calm mode shows the numbers and draws none")

# A tunnel endpoint that never answers must stop being probed: 3.8 MB/day of a metered
# allowance while alerting, spent on an address that is silent by design. A real first hop
# must NOT be given up on -- its silence means the local link is down.
class _Silence:
    ALERT = CALM = mode = "calm"
    _check_silence = nq.Indicator._check_silence
    _apply_rates = lambda self, mode: None

    def __init__(self, device, lines):
        self.silent = set()
        self.hops = {"t": ("10.0.0.1", device)}
        self.probers = {"10.0.0.1": type("P", (), {"window": window(lines)})()}

DEAD = ["no answer yet for icmp_seq=%d" % i for i in range(1, 7)]
# Seeded, so the test does not depend on which tunnel device happens to exist right now.
nq._TUNNEL_CACHE.update({"tun0": True, "wlp1s0": False})

silent = _Silence("tun0", DEAD)
silent._check_silence()
check("10.0.0.1" in silent.silent, "a silent tunnel hop stops being probed")

answering = _Silence("tun0", replies(6, 40))
answering._check_silence()
check(not answering.silent, "a tunnel hop that answers keeps being probed")

local = _Silence("wlp1s0", DEAD)
local._check_silence()
check(not local.silent, "a silent FIRST HOP is a finding, so probing continues")

# A device that has gone away must not be remembered as "not a tunnel": OpenVPN hands out
# tun0, tun1, tun0 again across reconnects, and a stale False puts LAN latency limits on a
# VPN endpoint -- the exact wrong verdict is_tunnel() exists to prevent.
check(nq.is_tunnel("nq-no-such-device") is False, "a missing device is not called a tunnel")
check("nq-no-such-device" not in nq._TUNNEL_CACHE,
      "...and that answer is not cached, because the name may come back")

print("\n=== the tray binding ===")
# If a future release drops both bindings, a bare gi traceback says only
# "Namespace ... not available", which does not tell the user what to install.
import gi as _gi
_real = _gi.require_version
_gi.require_version = lambda ns, v: (_ for _ in ()).throw(
    ValueError(f"Namespace {ns} not available")) if "AppIndicator" in ns else _real(ns, v)
try:
    nq._load_appindicator()
    check(False, "a missing binding must not fall through")
except SystemExit as exc:
    check("gir1.2-ayatanaappindicator3-0.1" in str(exc),
          "a missing binding names the package to install")
finally:
    _gi.require_version = _real
check(nq._load_appindicator().__name__.endswith("AyatanaAppIndicator3"),
      "Ayatana is preferred over Canonical's original binding")

print("\n=== what gets probed, and how often ===")
# The metered constraint lives or dies here. Keyed by ADDRESS, never by role.
class _Rates:
    CALM, ALERT = "calm", "alert"
    _wanted_intervals = nq.Indicator._wanted_intervals

    def __init__(self, targets, hops, silent=()):
        self.targets, self.hops, self.silent = targets, hops, set(silent)
        self.opts = opts

GW = ("192.168.1.1", "wlp1s0")
one = _Rates(["a"], {"a": GW})
check(one._wanted_intervals("calm") == {"a": 5.0, "192.168.1.1": 20.0},
      "one target in calm: the target at 5s, its hop 4x slower")

two = _Rates(["a", "b"], {"a": GW, "b": ("10.0.0.1", "eth0")})
check(two._wanted_intervals("calm") == {"a": 5.0, "192.168.1.1": 20.0},
      "extra targets cost nothing while the link is healthy")
check(two._wanted_intervals("alert") ==
      {"a": 1.0, "b": 1.0, "192.168.1.1": 2.0, "10.0.0.1": 2.0},
      "an alert probes every target and every hop")

# The whole point of keying by address: b IS a's first hop here.
shared = _Rates(["a", "192.168.1.1"], {"a": GW, "192.168.1.1": (None, None)})
check(shared._wanted_intervals("alert") == {"a": 1.0, "192.168.1.1": 1.0},
      "an address that is both a target and a hop is probed once, at the faster rate")

quiet = _Rates(["a"], {"a": GW}, silent=["192.168.1.1"])
check(quiet._wanted_intervals("calm") == {"a": 5.0},
      "a hop proved silent is dropped from the probe set entirely")

nohops = _Rates(["a"], {"a": GW})
nohops.opts = nq.parse_args(["--no-hops"])
check(nohops._wanted_intervals("calm") == {"a": 5.0}, "--no-hops probes targets only")

print("\n=== profiles change the numbers, not just the wording ===")
# The whole point of a profile: one identical measurement, three honest answers.
# A steady 200 ms link is fine for a page load, marginal for SSH, unusable for a call.
STEADY_200MS = replies(30, 200)
for profile, expect in (("web", "good"), ("ssh", "fair"), ("realtime", "poor")):
    tuned = nq.parse_args(["--profile", profile])
    w = nq.Window(9999, tuned.deadline)
    for line in STEADY_200MS:
        w.feed(line)
    got = nq.grade(w.snapshot(), tuned).level.name
    check(got == expect, f"200 ms steady under --profile {profile} -> {got} (want {expect})")

check(nq.parse_args([]).quality_label == "SSH quality", "ssh is the default profile")
check(nq.parse_args(["--profile", "realtime"]).deadline == 250.0,
      "a profile sets the interactivity deadline")
check(nq.parse_args(["--profile", "realtime", "--deadline", "777"]).deadline == 777.0,
      "an explicit flag overrides the profile")
check(all("dup_pct_fair" not in table for table in nq.PROFILES.values()),
      "duplicates are a link-layer symptom, so no profile redefines them")

print("\n=== fast probing is bought by variability, not by slowness ===")
# A steady-but-slow link must NOT latch alert mode on: under the realtime profile a
# constant 160 ms sits above p95_fair on every probe, which on a metered link would
# spend ~23 MB/day re-confirming a number already on screen.
rt = nq.parse_args(["--profile", "realtime"])
steady = nq.Window(9999, rt.deadline)
for line in replies(30, 160):
    steady.feed(line)
check(not nq.is_unstable(steady.snapshot(), rt),
      "steady 160 ms under realtime stays in calm mode")
check(nq.grade(steady.snapshot(), rt).level.name == "poor",
      "...while still being reported as poor")

slow_ssh = window(replies(30, 500))          # p95 500 ms, well over the ssh p95_fair 400
check(not nq.is_unstable(slow_ssh.snapshot(), opts),
      "steady 500 ms under ssh stays in calm mode too")

jittery = window(replies(25, 40) + [f"64 bytes from x: icmp_seq={i} ttl=60 time=500.0 ms"
                                    for i in range(26, 31)])
check(nq.is_unstable(jittery.snapshot(), opts),
      "a 460 ms tail on the same link does buy fast probing")

print("\n=== target resolution ===")
# `ip route get` rejects hostnames outright, so first_hop() must resolve first --
# otherwise a hostname target silently loses the whole fault-localisation section.
check(nq.resolve4("1.1.1.1") == "1.1.1.1", "a literal address passes through unchanged")
check(nq.resolve4("") is None, "an empty target resolves to nothing")
check(nq.resolve4("no.such.host.invalid") is None, "an unresolvable name is not fatal")
check("no.such.host.invalid" not in nq._ADDRESS_CACHE,
      "a failed lookup is not cached, so it can be retried")
check(nq.first_hop("no.such.host.invalid") == (None, None),
      "an unresolvable target yields no first hop")

print("\n=== probe rates and data cost ===")
check(nq.Prober("x", None, 16, 3, None).probes_per_second() == 0.0,
      "a disabled prober costs nothing")
check(nq.Prober("x", 5.0, 16, 3, None).probes_per_second() == 0.2,
      "a 5s interval is 0.2 probes/sec")
check(nq.Prober(None, 5.0, 16, 3, None).probes_per_second() == 0.0,
      "a prober with no target sends nothing, so it costs nothing")

print("\n=== every target has a menu row ===")
# A second --target is not probed while the link is healthy. Omitting its row makes the
# flag look ignored; the row must stay and say why it has no numbers.
class _Rows:
    _target_lines = nq.Indicator._target_lines
    _hop_lines = nq.Indicator._hop_lines

    def __init__(self, probers):
        self.targets, self.probers, self.hops, self.silent = ["a", "b"], probers, {}, set()

_P = lambda lines: type("P", (), {"window": window(lines)})()
check(_Rows({"a": _P(replies(3, 40))})._target_lines() ==
      ["  a: 3 probes in window", "  b: standby, probed only during an alert"],
      "calm mode lists an unprobed target as standby")
check(_Rows({"a": _P(replies(3, 40)), "b": _P([])})._target_lines() ==
      ["  a: 3 probes in window", "  b: no probes yet"],
      "an alert that has just started does not hide the target either")

print("\n=== the mtr menu item ===")
# opts.target is a list, because --target repeats. Formatted into the command line it gives
# mtr "['a', 'b']", which mtr cannot resolve: it exits at once, the terminal closes before
# it is drawn, and the click seems to do nothing. Only the primary target may reach mtr.
class _Mtr:
    _on_mtr = nq.Indicator._on_mtr

    def __init__(self, targets):
        self.targets = targets
        self.opts = nq.parse_args([arg for t in targets for arg in ("--target", t)])

launched = []
_which, _popen = nq.shutil.which, nq.subprocess.Popen
nq.shutil.which = lambda name: "/usr/bin/" + name
nq.subprocess.Popen = lambda argv: launched.append(argv)
try:
    _Mtr(["work.example.com", "10.0.0.1"])._on_mtr(None)
finally:
    nq.shutil.which, nq.subprocess.Popen = _which, _popen
check(launched == [["mate-terminal", "-e", "mtr work.example.com"]],
      f"mtr runs on the primary target alone (got {launched})")

print("\n=== the Quit menu item ===")
# self.probers maps address -> Prober, so iterating it yields address strings. A str has no
# .stop(), the AttributeError ends the handler before Gtk.main_quit(), and GTK only logs
# the traceback -- to stderr, which autostart discards. Quit then silently does nothing.
class _Quit:
    _on_quit = nq.Indicator._on_quit

    def __init__(self, addresses):
        self.probers = {a: type("P", (), {"stopped": False,
                                          "stop": lambda p: setattr(p, "stopped", True)})()
                        for a in addresses}

quitter, quit_calls = _Quit(["work.example.com", "192.168.1.1"]), []
_main_quit = nq.Gtk.main_quit
nq.Gtk.main_quit = lambda: quit_calls.append(True)
try:
    quitter._on_quit(None)
except AttributeError as exc:
    check(False, f"Quit stops every prober and leaves the main loop (raised {exc!r})")
else:
    check(all(p.stopped for p in quitter.probers.values()) and quit_calls == [True],
          "Quit stops every prober and leaves the main loop")
finally:
    nq.Gtk.main_quit = _main_quit

print()
if FAILED:
    print(f"FAILED {len(FAILED)}:")
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("ALL PASS")
