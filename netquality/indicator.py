#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# netquality -- network quality tray indicator for interactive use
# Copyright (C) 2026  Teodor Milkov <tm@del.bg>
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <https://www.gnu.org/licenses/>.
"""Network quality tray indicator for interactive use on a metered link.

Every probe is classified OK / LATE / LOST against an interactivity deadline: a reply
that arrives too late to be acted on is an impairment, not a success. What counts as
too late depends on the traffic, so --profile (ssh, realtime, web) sets the deadline
and the thresholds together. Quality is graded worst-impairment-wins, and the probe
rate adapts -- slow while healthy, fast while degraded -- so a metered link is not
charged for a connection that is behaving.
"""

import argparse
import collections
import importlib
import os
import re
import shlex
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
from typing import NamedTuple, Optional

import gi
gi.require_version("Gtk", "3.0")


def _load_appindicator():
    """Load a tray-indicator binding, and say what to install if there is none.

    Ayatana first, then Canonical's original. If a future release drops both, a bare
    `gi` traceback reports only "Namespace ... not available", which does not tell the
    user which package to install. This says it.

    A third binding exists, AyatanaAppIndicatorGlib-2.0, and is NOT tried here: it has
    dropped GTK, so its set_menu() cannot take the Gtk.Menu this program builds. Adding
    it is a port of the menu code, not another name in this list.
    """
    for namespace in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(namespace, "0.1")
            return importlib.import_module(f"gi.repository.{namespace}")
        except (ValueError, ImportError, AttributeError):
            continue
    sys.exit("netquality: no tray indicator binding is installed. On Debian and Ubuntu, "
             "install the package gir1.2-ayatanaappindicator3-0.1.")


AppIndicator = _load_appindicator()
from gi.repository import GLib, Gtk

import cairo

from netquality import __version__

APP_ID = "netquality-indicator"

# `stdbuf -oL` keeps ping's output line-buffered through a pipe. Resolved once: PATH
# does not change for the lifetime of the process, and ping restarts on every rate change.
PING_PREFIX = ["stdbuf", "-oL"] if shutil.which("stdbuf") else []

# A local-link verdict is pronounced against a tight threshold (--hop-bad-ms, 30 ms
# by default), but the first hop is probed slowly (--hop-divisor: every 20 s at calm
# defaults, so at most six samples fit the window). Two or three widely spaced samples
# cannot support a confident "LOCAL LINK IS SLOW" -- and on wifi they are actively
# misleading, because a probe every 20 s wakes a power-saving radio and measures the
# wake-up, not the link. Measured on one link: 82 ms from three slow probes, 20 ms from a
# burst. Below this many samples the numbers are shown and the verdict is withheld.
MIN_LOCAL_SAMPLES = 5

# A tunnel endpoint that has never answered is not going to start: plenty of them drop
# ICMP by design while carrying traffic perfectly well. Probing one forever spends a
# metered allowance on a guaranteed silence -- 0.38 MB/day at calm rates, 3.80 MB/day
# while alerting, which is exactly when it would be running. Give up after this many
# unanswered probes and say so, rather than showing a row that looks like a measurement.
SILENT_PEER_PROBES = 5

# ICMP echo carries an 8-byte header inside a 20-byte IPv4 header, and every probe
# pays for a request and a reply.
ICMP_OVERHEAD = 8 + 20


OK, LATE, LOST = "ok", "late", "lost"


def classify(rtt, deadline_ms):
    """The one definition of what a probe outcome means.

    LOST is no reply at all; LATE is a reply that arrived too late to be acted on --
    delivered by IP, useless to the user. Both are impairments, only OK is a success.
    Every count in Stats derives from this, so the deadline rule is written once.
    """
    if rtt is None:
        return LOST
    return LATE if rtt > deadline_ms else OK


def nearest_rank(sorted_values, quantile):
    """A quantile that is an actually observed value, never an interpolated one.

    statistics.quantiles() interpolates, inventing a latency that was never measured --
    wrong for an indicator reporting real observed delay -- and raises on an empty
    sample where this returns None.
    """
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, round(quantile * (len(sorted_values) - 1)))
    return sorted_values[index]


class Stats(NamedTuple):
    """One window reduced to what grading and the menu need.

    A NamedTuple rather than a dict: it is the most-read structure in the program, and
    the rest of the file already speaks in NamedTuples.
    """
    total: int
    replies: int
    late: int
    lost: int
    bad_pct: float
    late_pct: float
    loss_pct: float
    median: Optional[float]
    p95: Optional[float]
    tail: Optional[float]
    longest_bad_run: int
    duplicates: int
    dup_pct: float
    stale: float


class Verdict(NamedTuple):
    level: "Level"
    reason: str          # names the rule that fired, so the colour is never a mystery


class Level(NamedTuple):
    rank: int          # higher is worse; UNKNOWN sorts below GOOD so it never raises an alert
    name: str
    bars: int          # how many bars are lit; the rest are drawn dimmed
    rgb: tuple
    crossed: bool = False   # DOWN alone is struck through


UNKNOWN = Level(-1, "unknown", 0, (0.55, 0.55, 0.58))
GOOD    = Level(0, "good", 4, (0.30, 0.75, 0.30))
FAIR    = Level(1, "fair", 3, (0.85, 0.75, 0.15))
POOR    = Level(2, "poor", 2, (0.90, 0.55, 0.10))
BAD     = Level(3, "bad",  1, (0.85, 0.20, 0.20))
DOWN    = Level(4, "down", 1, (0.55, 0.55, 0.58), crossed=True)
ALL_LEVELS = (UNKNOWN, GOOD, FAIR, POOR, BAD, DOWN)

# Both patterns expose the icmp_seq as group(1); see Window.feed.
RE_REPLY = re.compile(r"icmp_seq=(\d+).*?time=([\d.]+)\s*ms")
RE_NOANSWER = re.compile(r"no answer yet for icmp_seq=(\d+)")
RE_ERRSEQ = re.compile(r"icmp_seq=(\d+).*(Unreachable|Time to live exceeded|Frag needed)")
RE_DUP = re.compile(r"\(DUP!\)")


class Window:
    """Rolling window of probe outcomes for one target.

    Records are keyed by (epoch, seq). ping restarts whenever the probe rate changes and
    its icmp_seq counter restarts with it, so seq alone would collide across restarts and
    silently discard probes.
    """

    def __init__(self, seconds, deadline_ms):
        self.seconds = seconds
        self.deadline_ms = deadline_ms
        self.lock = threading.Lock()
        self.records = collections.OrderedDict()
        self.duplicates = collections.deque()
        self.epoch = 0
        self.last_line_at = time.monotonic()

    def new_epoch(self):
        with self.lock:
            self.epoch += 1

    def reset(self):
        """Discard everything: the readings belong to a target we no longer probe."""
        with self.lock:
            self.records.clear()
            self.duplicates.clear()
            self.epoch += 1
            self.last_line_at = time.monotonic()

    def _trim(self, now):
        cutoff = now - self.seconds
        while self.records and next(iter(self.records.values()))["t"] < cutoff:
            self.records.popitem(last=False)
        while self.duplicates and self.duplicates[0] < cutoff:
            self.duplicates.popleft()

    def feed(self, line):
        """Record one line of ping output.

        Only lines we actually understand refresh last_line_at. Chatter such as
        "ping: connect: Network is unreachable" -- which ping emits, then exits, once
        the route is gone -- must not count as liveness, or a link that has vanished
        looks merely quiet instead of down.
        """
        now = time.monotonic()
        with self.lock:
            if RE_DUP.search(line):
                self.last_line_at = now
                self.duplicates.append(now)
                return

            reply = RE_REPLY.search(line)
            if reply:
                self.last_line_at = now
                key = (self.epoch, int(reply.group(1)))
                rtt = float(reply.group(2))
                record = self.records.get(key)
                if record is None:
                    self.records[key] = {"t": now, "rtt": rtt}
                elif record["rtt"] is None:
                    # A late reply to a probe we already timed out. Record the RTT so it
                    # can be judged against the deadline -- do NOT treat it as healthy.
                    record["rtt"] = rtt
                self._trim(now)
                return

            timed_out = RE_NOANSWER.search(line)
            if timed_out is None:
                timed_out = RE_ERRSEQ.search(line)
            if timed_out:
                self.last_line_at = now
                key = (self.epoch, int(timed_out.group(1)))
                self.records.setdefault(key, {"t": now, "rtt": None})
                self._trim(now)

    def snapshot(self) -> "Stats":
        now = time.monotonic()
        with self.lock:
            self._trim(now)
            rtts = [record["rtt"] for record in self.records.values()]
            duplicates = len(self.duplicates)
            stale = now - self.last_line_at

        outcomes = [classify(rtt, self.deadline_ms) for rtt in rtts]
        replies = sorted(rtt for rtt in rtts if rtt is not None)
        total = len(outcomes)

        longest_bad_run = current_run = 0
        for outcome in outcomes:
            current_run = 0 if outcome == OK else current_run + 1
            longest_bad_run = max(longest_bad_run, current_run)

        def pct(count):
            return (count / total * 100.0) if total else 0.0

        late, lost = outcomes.count(LATE), outcomes.count(LOST)
        median = statistics.median(replies) if replies else None
        p95 = nearest_rank(replies, 0.95)
        return Stats(
            total=total, replies=len(replies), late=late, lost=lost,
            bad_pct=pct(late + lost), late_pct=pct(late), loss_pct=pct(lost),
            median=median, p95=p95,
            tail=(p95 - median) if (p95 is not None and median is not None) else None,
            longest_bad_run=longest_bad_run, duplicates=duplicates,
            dup_pct=pct(duplicates), stale=stale,
        )


def grade(stats, limits) -> Verdict:
    """Worst impairment wins, and the winning rule is reported alongside the level.

    One table drives both the colour and the explanation, so they cannot disagree.
    """
    if stats.total == 0:
        if stats.stale > limits.stale_down:
            return Verdict(DOWN, "no output from ping")
        return Verdict(UNKNOWN, "waiting for the first probe")
    if stats.replies == 0 and stats.total >= 3:
        return Verdict(DOWN, "no replies at all")

    median = stats.median or 0
    p95 = stats.p95 or 0
    run = stats.longest_bad_run
    dup_pct = stats.dup_pct

    tiers = (
        (BAD, (
            (run >= limits.run_bad, f"{run} bad in a row"),
            (stats.bad_pct >= limits.bad_pct_bad, f"{stats.bad_pct:.0f}% unusable"),
            (median >= limits.median_bad, f"median {median:.0f} ms"),
            (p95 >= limits.p95_bad, f"p95 {p95:.0f} ms"),
        )),
        (POOR, (
            (run >= limits.run_poor, f"{run} bad in a row"),
            (stats.bad_pct >= limits.bad_pct_poor, f"{stats.bad_pct:.0f}% unusable"),
            (median >= limits.median_poor, f"median {median:.0f} ms"),
            (p95 >= limits.p95_poor, f"p95 {p95:.0f} ms"),
        )),
        (FAIR, (
            (run >= 1, f"{run} bad probe(s)"),
            (stats.bad_pct > 0, f"{stats.bad_pct:.1f}% unusable"),
            (dup_pct >= limits.dup_pct_fair, f"{dup_pct:.0f}% duplicates"),
            (median >= limits.median_fair, f"median {median:.0f} ms"),
            (p95 >= limits.p95_fair, f"p95 {p95:.0f} ms"),
        )),
    )
    def annotate(level, reason):
        """Duplicates are a diagnostic annotation, not a severity tier.

        A path can be POOR on latency while duplicate replies are the more telling
        symptom -- they point at link-layer retransmission. Because the tiers return
        on the first match, a duplicate check that lives in one tier is invisible from
        every worse one, so report it alongside whatever verdict was reached.
        """
        if dup_pct >= limits.dup_pct_fair and "duplicate" not in reason:
            reason += f", {dup_pct:.0f}% duplicates"
        return Verdict(level, reason)

    for level, checks in tiers:
        fired = [why for triggered, why in checks if triggered]
        if fired:
            return annotate(level, ", ".join(fired))
    return annotate(GOOD, "no impairment")


def is_unstable(stats, limits):
    """Whether fast probing is worth the data.

    A link that is merely slow but steady tells us nothing new by being probed harder --
    only variability (loss, late replies, spikes, duplicates) justifies the extra traffic.

    The spike test is therefore on the tail, p95 minus median, and NOT on p95 itself.
    An absolute p95 threshold is a level, not a variability measure: a steady 160 ms
    link sits above the realtime profile's p95_fair on every single probe, which would
    latch fast probing on for as long as the link stayed that way -- roughly 23 MB/day
    of a metered allowance spent re-confirming something already on screen.
    """
    return bool(stats.lost or stats.late or stats.longest_bad_run
                or stats.dup_pct >= limits.dup_pct_fair
                or (stats.tail or 0) >= limits.spike_ms)


class Prober(threading.Thread):
    """Runs one ping subprocess per target, restarting it when the probe rate changes.

    An interval of None means "do not probe this target at all".
    """

    def __init__(self, target, interval: Optional[float], payload, wait, window):
        super().__init__(daemon=True)
        self.target, self.payload, self.wait = target, payload, wait
        self.window = window
        self._interval = interval
        self._process = None
        self._stopping = False
        self._restart = threading.Event()

    def configure(self, interval: Optional[float]):
        if interval != self._interval:
            self._interval = interval
            self.window.new_epoch()
            self._interrupt()

    def retarget(self, target: Optional[str]):
        """Point at a different host, discarding readings taken from the old one."""
        if target != self.target:
            self.target = target
            self.window.reset()
            self._interrupt()

    def restart(self):
        """Same target, fresh start: discard readings and respawn ping.

        Needed when the route to the target changes underneath us. ping connects its
        socket, which binds a source address; once the path moves to another interface
        that source is wrong, so the process must be replaced, and the samples it already
        collected describe a path that no longer exists.
        """
        self.window.reset()
        self._interrupt()

    def stop(self):
        self._stopping = True
        self._interrupt()

    def probes_per_second(self):
        """Zero unless this prober is actually sending: run() skips a target-less prober,
        so counting its interval would overstate the data bill of an untunnelled link."""
        if self._interval is None or not self.target:
            return 0.0
        return 1.0 / self._interval

    def _interrupt(self):
        if self._process and self._process.poll() is None:
            self._process.terminate()
        self._restart.set()

    def _pause(self, seconds):
        self._restart.wait(seconds)
        self._restart.clear()

    def run(self):
        while not self._stopping:
            if self._interval is None or not self.target:
                self._pause(1.0)
                continue
            command = PING_PREFIX + ["ping", "-O", "-n", "-s", str(self.payload),
                                     "-i", str(self._interval), "-W", str(self.wait),
                                     self.target]
            try:
                self._process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1)
            except OSError as exc:
                print(f"{APP_ID}: {self.target}: {exc}", file=sys.stderr)
                self._pause(5.0)
                continue
            for line in self._process.stdout:
                if self._stopping:
                    break
                self.window.feed(line)
            if not self._stopping:
                self._pause(1.0)      # ping exited: link down, DNS failure, or a rate change


def icon_name(level: Level):
    return f"netq-{level.name}"


def render_icons(cache_dir):
    """Draw one signal-bar PNG per level: lit bars in the level colour, dead bars dimmed."""
    os.makedirs(cache_dir, exist_ok=True)
    size, bar_count, gap = 24, 4, 2
    bar_width = (size - gap * (bar_count - 1)) / bar_count
    for level in ALL_LEVELS:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        context = cairo.Context(surface)
        for i in range(bar_count):
            height = size * (0.28 + 0.24 * i)
            if i < level.bars:
                context.set_source_rgba(*level.rgb, 1.0)
            else:
                context.set_source_rgba(0.5, 0.5, 0.5, 0.30)
            context.rectangle(i * (bar_width + gap), size - height, bar_width, height)
            context.fill()
        if level.crossed:
            context.set_source_rgba(0.85, 0.20, 0.20, 0.95)
            context.set_line_width(2.5)
            context.move_to(2, size - 2)
            context.line_to(size - 2, 2)
            context.stroke()
        surface.write_to_png(os.path.join(cache_dir, f"{icon_name(level)}.png"))
    return cache_dir


_ADDRESS_CACHE = {}


def resolve4(target):
    """`target` as an IPv4 literal, for tools that will not resolve names themselves.

    `ip route get` demands an address and fails outright on a hostname, while ping is
    happy with either. Resolved once and remembered -- exactly as ping itself resolves
    once at startup -- so the periodic route recheck never blocks the UI waiting on a
    slow or unreachable DNS server. A literal address costs no lookup at all.
    """
    if not target:
        return None
    if target not in _ADDRESS_CACHE:
        try:
            info = socket.getaddrinfo(target, None, socket.AF_INET)
        except OSError:
            return None      # transient: retry next recheck rather than cache a failure
        _ADDRESS_CACHE[target] = info[0][4][0]
    return _ADDRESS_CACHE[target]


def first_hop(target):
    """The gateway the kernel would actually use to reach `target`, plus the device.

    Deliberately not "the default route". A machine can have several -- wifi, wired,
    a phone tether, a VPN -- and more-specific or policy routes can send different
    destinations out of different interfaces. Probing the wrong one produces the worst
    possible outcome: a confident "local link is fine" about a link the monitored
    traffic never touches.

    Returns (gateway, device); gateway is None when the target is on-link or unknown.
    """
    address = resolve4(target)
    if not address:
        return None, None
    try:
        output = subprocess.run(["ip", "-4", "route", "get", address],
                                capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    via = re.search(r"\bvia (\S+)", output)
    dev = re.search(r"\bdev (\S+)", output)
    return (via.group(1) if via else None), (dev.group(1) if dev else None)


_TUNNEL_CACHE = {}


def is_tunnel(device):
    """Whether `device` is a tunnel rather than a link to a physical neighbour.

    Asked of the kernel, not guessed from the name: a WireGuard interface can be called
    anything, and a name like "tundra-vpn" matches a naive "tun*" prefix test by pure
    coincidence. Two authoritative signals, either of which is decisive:

      link/none  the device has no link-layer address at all
      NOARP      no address resolution, i.e. no shared medium with neighbours on it

    NOARP is the semantically load-bearing one: it is exactly the property that makes
    "a few milliseconds to the router" a meaningless expectation.
    """
    if device is None:
        return False
    if device not in _TUNNEL_CACHE:
        try:
            found = subprocess.run(["ip", "-details", "link", "show", "dev", device],
                                   capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return False        # unknown: do not cache, and do not claim it is a tunnel
        if found.returncode:
            # The device is gone. `ip` exits non-zero and prints nothing, so caching the
            # answer here would record "not a tunnel" for a name that is very likely to
            # come back: OpenVPN hands out tun0, tun1, tun0 again across reconnects. The
            # stale False would then put LAN latency limits on a VPN endpoint, which is
            # the exact wrong verdict this function exists to prevent.
            return False
        _TUNNEL_CACHE[device] = "link/none" in found.stdout or "NOARP" in found.stdout
    return _TUNNEL_CACHE[device]


class Indicator:
    """Owns the probers, the grading loop, and the tray icon."""

    CALM, ALERT = "calm", "alert"

    def __init__(self, opts):
        self.opts = opts
        self.targets = list(opts.target)
        self.hops = {}                # target -> (hop address, device)
        self.probers = {}             # address -> Prober; one per ADDRESS, never per role
        self.silent = set()           # tunnel hops that have proved they never answer
        self.hops_checked_at = 0.0
        self.mode = self.CALM
        self.level = UNKNOWN
        self.alert_until = 0.0

        self._resolve_hops()
        self.indicator = self._build_indicator()
        self.rows = []
        self._shown_label = None
        self._shown_lines = None
        self._apply_rates(self.CALM)
        GLib.timeout_add_seconds(1, self._tick)

    @property
    def primary(self):
        """The prober for the first target. It alone drives the icon and the grade."""
        return self.probers[self.targets[0]]

    # -- construction ----------------------------------------------------------------

    def _build_indicator(self):
        cache_dir = render_icons(os.path.join(GLib.get_user_cache_dir(), APP_ID))
        indicator = AppIndicator.Indicator.new(
            APP_ID, icon_name(UNKNOWN), AppIndicator.IndicatorCategory.COMMUNICATIONS)
        indicator.set_icon_theme_path(cache_dir)
        indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        indicator.set_title("Network quality")

        self.menu = Gtk.Menu()
        self.menu.append(Gtk.SeparatorMenuItem())
        for label, callback in (("Force fast probing for 5 min", self._on_force),
                                ("Run mtr in a terminal", self._on_mtr),
                                ("Quit", self._on_quit)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", callback)
            self.menu.append(item)
        self.menu.show_all()
        indicator.set_menu(self.menu)
        return indicator

    # -- probe rates -----------------------------------------------------------------

    def _wanted_intervals(self, mode):
        """address -> probe interval, for every address that must be probed in `mode`.

        Keyed by ADDRESS, not by role. Several targets usually share a first hop, and a
        hop can itself be a target; without this an address would be probed once per role
        and the metered allowance would leak.
        """
        opts = self.opts
        alert = mode == self.ALERT
        wanted = {}

        def want(address, interval):
            if not address or address in self.silent:
                return
            already = wanted.get(address)
            wanted[address] = interval if already is None else min(already, interval)

        for index, target in enumerate(self.targets):
            # Only the first target is watched continuously: it is the one being judged.
            # The rest answer "is it this destination, or everything?", which is worth
            # nothing while the link is healthy and costs data to ask.
            if index and not alert:
                continue
            want(target, opts.busy_interval if alert else opts.calm_interval)
            if opts.hops:
                hop, _ = self.hops.get(target, (None, None))
                want(hop, max(2.0, opts.busy_interval * 2) if alert
                     else opts.calm_interval * opts.hop_divisor)
        return wanted

    def _apply_rates(self, mode):
        """Bring the running probers into line with what `mode` wants probed."""
        wanted = self._wanted_intervals(mode)
        for address, interval in wanted.items():
            prober = self.probers.get(address)
            if prober is None:
                prober = Prober(address, None, self.opts.payload, self.opts.wait,
                                Window(self.opts.window, self.opts.deadline))
                self.probers[address] = prober
                prober.start()
            prober.configure(interval)
        for address in [a for a in self.probers if a not in wanted]:
            self.probers.pop(address).stop()
        self.mode = mode

    def megabytes_per_day(self):
        bytes_per_probe = (self.opts.payload + ICMP_OVERHEAD) * 2
        probes_per_second = sum(p.probes_per_second() for p in self.probers.values())
        return probes_per_second * bytes_per_probe * 86400 / 1e6

    # -- main loop -------------------------------------------------------------------

    def _tick(self):
        self._recheck_hops(time.monotonic())
        self._check_silence()
        stats = self.primary.window.snapshot()
        verdict = grade(stats, self.opts)
        self._update_mode(stats)
        self._update_icon(verdict.level, stats)
        self._set_menu_lines(self._compose_lines(verdict, stats))
        return True

    def _resolve_hops(self):
        """Ask the kernel which hop it would use for each target. Returns True on change.

        Not "the default route". A machine can have several, and more-specific or policy
        routes send different destinations out of different interfaces, so each target is
        asked about separately.
        """
        changed = False
        for target in self.targets:
            found = first_hop(target) if self.opts.hops else (None, None)
            if found == self.hops.get(target):
                continue
            was = self.hops.get(target, (None, None))[0]
            self.hops[target] = found
            # A hop we have not watched before deserves a fresh chance to answer.
            self.silent.discard(was)
            self.silent.discard(found[0])
            changed = True
        return changed

    def _recheck_hops(self, now):
        """Routes move under us: wifi to ethernet, a hotspot, a VPN, a failover script.

        A prober still aimed at the old hop reports either a false local outage or, worse,
        whatever different device now answers on that address.
        """
        if now < self.hops_checked_at + self.opts.hop_recheck:
            return
        self.hops_checked_at = now
        if not self._resolve_hops():
            return
        # The path moved. Everything measured before describes a link that is no longer
        # carrying this traffic, so start clean rather than averaging two uplinks together.
        for target in self.targets:
            prober = self.probers.get(target)
            if prober:
                prober.restart()
        self._apply_rates(self.mode)

    def _check_silence(self):
        """Stop probing a tunnel hop that has proved it never answers.

        Tunnel hops only. Silence from a real first hop means the local link is down,
        which is a finding worth going on measuring. Silence from a tunnel endpoint means
        nothing at all -- many drop ICMP while carrying traffic perfectly well -- and the
        target's own row already reports whether the tunnel works.
        """
        for address, device in self.hops.values():
            if not address or address in self.silent or not is_tunnel(device):
                continue
            prober = self.probers.get(address)
            if prober is None:
                continue
            stats = prober.window.snapshot()
            if stats.total >= SILENT_PEER_PROBES and stats.replies == 0:
                self.silent.add(address)
                self._apply_rates(self.mode)      # re-apply, so the prober really stops

    def _update_mode(self, stats):
        now = time.monotonic()
        if is_unstable(stats, self.opts):
            self.alert_until = now + self.opts.alert_hold
        wanted = self.ALERT if now < self.alert_until else self.CALM
        if wanted != self.mode:
            self._apply_rates(wanted)

    def _update_icon(self, level, stats):
        if level is not self.level:
            self.level = level
            self.indicator.set_icon_full(icon_name(level), f"network quality: {level.name}")
        if stats.replies:
            label = f"{stats.median:.0f}ms {stats.bad_pct:.0f}%"
        elif level is UNKNOWN:
            label = "..."
        else:
            label = "no reply"
        # The label crosses DBus to the panel; skip the round trip when nothing changed.
        if label != self._shown_label:
            self._shown_label = label
            self.indicator.set_label(label, "8888ms 100%")

    def _compose_lines(self, verdict, stats):
        opts = self.opts
        level = verdict.level
        lines = [f"{opts.quality_label}: {level.name.upper()}   ({self.mode} mode)",
                 f"  Why: {verdict.reason}"]
        if stats.replies:
            lines += [
                f"  RTT: {stats.median:.0f} ms median, {stats.p95:.0f} ms p95"
                f" (tail +{stats.tail:.0f} ms)",
                f"  Late: {stats.late_pct:.0f}% over {opts.deadline:.0f} ms",
                f"  Lost: {stats.loss_pct:.0f}%",
                f"  Worst run: {stats.longest_bad_run} probe(s)"
                + (f",  {stats.duplicates} dup" if stats.duplicates else ""),
            ]
        elif level is UNKNOWN:
            lines.append("  waiting for the first probe...")
        else:
            lines.append(f"  no replies in the last {opts.window:g}s")
        lines += self._target_lines()
        lines.append(f"  ~{self.megabytes_per_day():.1f} MB/day at current rate")
        return lines

    @staticmethod
    def _reading(stats):
        """The one definition of how a probe window is written out."""
        return f"{stats.median:.0f} ms, {stats.bad_pct:.0f}% bad"

    @staticmethod
    def _hop_name(address, device):
        """Name a hop and the interface it leaves by -- with several routes present, the
        interface is the part that says which link is being measured."""
        return f"{address} ({device})" if device else str(address)

    def _local_verdict(self):
        """The local-link verdict, or None while the sampling cannot support one.

        In calm mode a hop is probed every 20 s by default, and a hop reached over wifi
        answers an isolated probe far more slowly than it answers a burst -- the radio has
        to wake first. Measured on one link: 26.8 / 6.5 / 11.1 ms inside a single burst.
        A verdict off readings like that would be the confidently-wrong answer this whole
        section exists to avoid, so calm mode shows the numbers and draws no conclusion.
        Nothing is lost: a local link bad enough to matter drags the first target down
        too, and that is what raises alert mode, where the hop is probed every 2 s.
        """
        return self._hop_verdict if self.mode == self.ALERT else None

    def _hop_verdict(self, hop):
        """A first hop is judged on its own terms: a LAN hop answering in tens of
        milliseconds is already a bad link, long before that would worry us on a WAN."""
        if hop.total < MIN_LOCAL_SAMPLES:
            return f"only {hop.total} probe(s) so far, not enough to judge"
        if hop.bad_pct > 0 or hop.longest_bad_run > 0:
            return "LOCAL LINK IS LOSING PACKETS"
        if hop.median >= self.opts.hop_bad_ms:
            return f"LOCAL LINK IS SLOW (>{self.opts.hop_bad_ms:.0f} ms to the router)"
        return "local link is fine"

    def _target_lines(self):
        """One row per target, each followed by its first hop.

        Uniform on purpose. A tunnel needs no case of its own here: it is simply a first
        hop that leaves by a tunnel device, and `is_tunnel` decides what may be concluded.

        Every target gets a row, even one that is not probed. A target the user asked for
        and the menu silently omits looks like a flag that was ignored, not a cost policy.
        """
        lines = []
        for index, target in enumerate(self.targets):
            prober = self.probers.get(target)
            if prober is None:                # a context target, not probed while healthy
                lines.append(f"  {target}: standby, probed only during an alert")
                continue
            stats = prober.window.snapshot()
            if index == 0:
                lines.append(f"  {target}: {stats.total} probes in window")
            elif stats.replies:
                lines.append(f"  {target}: {self._reading(stats)}")
            elif stats.total:
                lines.append(f"  {target}: no replies")
            else:                             # an alert has just started
                lines.append(f"  {target}: no probes yet")
            lines += self._hop_lines(target)
        return lines

    def _hop_lines(self, target):
        address, device = self.hops.get(target, (None, None))
        if not address:
            return []
        name = self._hop_name(address, device)
        tunnel = is_tunnel(device)
        if address in self.silent:
            return [f"    via {name}: reached through this tunnel, silent to ICMP"]
        prober = self.probers.get(address)
        if prober is None:
            return []
        stats = prober.window.snapshot()
        if stats.replies:
            line = f"    via {name}: {self._reading(stats)}"
            # A tunnel endpoint sits at the far side of the whole internet path, so
            # local-link thresholds would condemn a healthy VPN. Report, judge nothing.
            verdict = None if tunnel else self._local_verdict()
            return [line + (f"  ->  {verdict(stats)}" if verdict else "")]
        if not stats.total:
            return []
        # Never call a tunnel down from this row. Many endpoints drop ICMP while carrying
        # traffic, and if the tunnel really were down the target's own row would say so.
        return [f"    via {name}: " + ("no ICMP replies (endpoint may not answer pings)"
                                       if tunnel else "no replies -> LOCAL LINK IS DOWN")]

    def _set_menu_lines(self, lines):
        """Grow the row pool as needed, so a new line can never be silently dropped."""
        if lines == self._shown_lines:      # also stops the menu twitching while it is open
            return
        self._shown_lines = list(lines)
        while len(self.rows) < len(lines):
            item = Gtk.MenuItem(label="")
            item.set_sensitive(False)       # a reading, not something to click
            self.menu.insert(item, len(self.rows))     # stats rows sit above the separator
            item.show()
            self.rows.append(item)
        for i, item in enumerate(self.rows):
            if i < len(lines):
                # Plain text only: AppIndicator exports this menu over com.canonical.dbusmenu
                # and the panel rebuilds it with its own widgets and font. Pango markup does
                # not survive the trip, and leaves raw tags in the item's label property.
                item.set_label(lines[i])
            item.set_visible(i < len(lines))

    # -- menu actions ----------------------------------------------------------------

    def _on_force(self, _item):
        self.alert_until = time.monotonic() + 300
        self._apply_rates(self.ALERT)

    def _on_mtr(self, _item):
        # The primary target only: opts.target is a list (--target repeats), and `-e`
        # takes shell text. A bad argument fails invisibly -- mtr exits at once and the
        # terminal closes before it is drawn -- so the test pins the exact command.
        command = f"mtr {shlex.quote(self.targets[0])}"
        for terminal in ("mate-terminal", "x-terminal-emulator", "xterm"):
            if shutil.which(terminal):
                subprocess.Popen([terminal, "-e", command])
                return

    def _on_quit(self, _item):
        # .values(): self.probers maps address -> Prober. Iterating the dict itself yields
        # strings, and the error would end this handler before main_quit() -- silently,
        # because GTK only logs exceptions raised in a signal handler.
        for prober in self.probers.values():
            prober.stop()
        Gtk.main_quit()


# What "quality" means depends on what the link is for, so a profile sets the wording
# and the numbers together -- a generic label over SSH-tuned thresholds would judge a
# video call by typing-latency rules and call 8% loss merely POOR, which for audio is
# wrecked. Values here are defaults: an explicit flag always wins over the profile.
#
# Deliberately NOT profiled: --dup-pct-fair (duplicate replies mean link-layer
# retransmission whatever the traffic is) and --wait (a late reply still distinguishes
# LATE from LOST, so there is no reason to stop waiting for one sooner).
#
# There is no throughput or streaming profile. ICMP round-trip time cannot measure
# bandwidth, and a profile implying otherwise would be a fabricated reading.
PROFILES = {
    "ssh": dict(
        quality_label="SSH quality",
        deadline=1000.0,
        median_fair=120.0, median_poor=300.0, median_bad=600.0,
        p95_fair=400.0, p95_poor=1000.0, p95_bad=2000.0,
        bad_pct_poor=8.0, bad_pct_bad=20.0, run_poor=2, run_bad=4,
    ),
    # Conversational audio has a one-way budget of about 150 ms (ITU-T G.114), so a
    # reply past 250 ms round trip arrives too late to be played out, and loss hurts
    # far sooner than it does for a keystroke that can simply be retransmitted.
    "realtime": dict(
        quality_label="Call quality",
        deadline=250.0,
        median_fair=80.0, median_poor=150.0, median_bad=300.0,
        p95_fair=150.0, p95_poor=250.0, p95_bad=400.0,
        bad_pct_poor=2.0, bad_pct_bad=5.0, run_poor=2, run_bad=3,
    ),
    # Page loads and downloads survive a retransmission the user never sees, so only
    # sustained impairment is worth a colour change.
    "web": dict(
        quality_label="Net quality",
        deadline=3000.0,
        median_fair=250.0, median_poor=600.0, median_bad=1200.0,
        p95_fair=1000.0, p95_poor=2500.0, p95_bad=5000.0,
        bad_pct_poor=15.0, bad_pct_bad=30.0, run_poor=4, run_bad=8,
    ),
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="netquality", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version",
                        version=f"netquality {__version__}")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="ssh",
                        help="the use of the link: ssh for typed commands, realtime "
                             "for voice and video, web for a browser. This sets the "
                             "limits below and the menu words. The defaults that follow "
                             "are the ssh ones")
    parser.add_argument("--target", action="append", metavar="HOST",
                        help="a host to watch. You can give this option more than one "
                             "time. The first target sets the icon and the grade. The "
                             "other targets are probed only during an alert, to show if "
                             "a fault is one destination or all traffic. Default: 8.8.8.8")
    parser.add_argument("--no-hops", dest="hops", action="store_false",
                        help="do not probe the first hop to each target")
    parser.add_argument("--hop-recheck", type=float, default=10.0,
                        help="the time between two checks of the first hop to each target, in seconds")
    parser.add_argument("--hop-divisor", type=float, default=4.0,
                        help="probe each first hop at this multiple of its target interval")
    parser.add_argument("--hop-bad-ms", type=float, default=30.0,
                        help="the median RTT to a first hop that makes the local link suspect, in ms")
    parser.add_argument("--payload", type=int, default=16, help="the size of the ICMP payload, in bytes")
    parser.add_argument("--calm-interval", type=float, default=5.0)
    parser.add_argument("--busy-interval", type=float, default=1.0)
    parser.add_argument("--alert-hold", type=float, default=60.0,
                        help="the time to stay in fast mode after the last bad probe, in seconds")
    parser.add_argument("--wait", type=float, default=3.0, help="the time to wait for one reply (ping -W), in seconds")
    parser.add_argument("--window", type=float, default=120.0, help="the length of the measurement window, in seconds")
    parser.add_argument("--deadline", type=float, default=1000.0,
                        help="an RTT more than this is LATE: too slow to use, in ms")
    parser.add_argument("--run-poor", type=int, default=2, dest="run_poor")
    parser.add_argument("--run-bad", type=int, default=4, dest="run_bad")
    parser.add_argument("--bad-pct-poor", type=float, default=8.0)
    parser.add_argument("--bad-pct-bad", type=float, default=20.0)
    parser.add_argument("--median-fair", type=float, default=120.0)
    parser.add_argument("--median-poor", type=float, default=300.0)
    parser.add_argument("--median-bad", type=float, default=600.0)
    parser.add_argument("--p95-fair", type=float, default=400.0)
    parser.add_argument("--p95-poor", type=float, default=1000.0)
    parser.add_argument("--p95-bad", type=float, default=2000.0)
    parser.add_argument("--dup-pct-fair", type=float, default=5.0,
                        help="the percentage of duplicate replies that makes the link suspect")

    # Read --profile first, fold it into the defaults, then parse for real: anything
    # given explicitly on the command line is set in the second pass and so wins.
    chosen, _ = parser.parse_known_args(argv)
    parser.set_defaults(**PROFILES[chosen.profile])

    opts = parser.parse_args(argv)
    # action="append" cannot carry a default without appending to it, so the default
    # is applied here instead.
    opts.target = opts.target or ["8.8.8.8"]
    # grade() reads its limits straight off opts; these two are derived.
    opts.stale_down = max(15.0, opts.calm_interval * 4)
    # How big a latency excursion counts as a spike: the profile's own gap between a
    # fine median and a fine p95. Floored, because a tail of a few milliseconds is
    # measurement noise and would keep the link permanently "unstable".
    opts.spike_ms = max(50.0, opts.p95_fair - opts.median_fair)
    return opts


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    Indicator(parse_args())
    Gtk.main()


if __name__ == "__main__":
    main()
