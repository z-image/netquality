# Design notes

Decisions that are not obvious from the code, and the reasoning behind them. Written
so a future change does not quietly undo something that was deliberate.

## Measurement

**Probes are OK / LATE / LOST, not just lost.** A reply that arrives after 13 seconds
was delivered by IP but is useless for an interactive session. Counting it as a success
made the indicator show green through a total SSH freeze. `--deadline` is the cutoff
(1000 ms under the default `ssh` profile; see Grading); late replies are recorded and judged, never forgiven. Compare RFC 3611's
distinction between network loss and discard/late-arrival impairment.

**Longest run of consecutive bad probes is a first-class metric.** Six scattered losses
in a minute is a working session; six consecutive is a frozen one. Loss percentage alone
cannot tell them apart.

**p95 is nearest-rank, not `statistics.quantiles`.** The library interpolates, which
produces a latency that was never measured. For an indicator reporting real observed
delay that is wrong, and `quantiles` additionally raises on an empty window where the
one-liner returns `None`.

**Jitter is not in the grading.** `p95 - median` ("tail") is reported instead because it
is legible. An earlier median-absolute-deviation metric was removed: robust dispersion
estimators are designed to ignore single outliers, which is precisely the event that
matters here.

## Grading

**Worst impairment wins.** No composite score. The colour is always attributable to one
named rule, and `grade()` returns that rule as `Verdict.reason` from the same table that
picks the level, so the two cannot drift apart.

**No ITU-T G.107 / MOS.** It models speech transmission quality with codec-specific
impairment factors. Repurposing it as a generic "internet quality" number would be
pseudo-science. Y.1541 is a useful sanity reference for thresholds, not a scoring engine.

**Duplicates annotate, they do not escalate.** TCP absorbs duplicate packets, so they do
not damage a session the way latency does -- but they are diagnostic (see `ping(8)`:
inappropriate link-level retransmission). Because the tiers return on first match, a
duplicate check living in one tier is invisible from every worse one, so duplicates are
appended to whatever verdict was reached.

**A profile sets the wording and the thresholds together.** "Quality" is meaningless
without saying quality *for what*. The thresholds were tuned for interactive SSH and the
menu said so; relabelling that generically while keeping the numbers would have judged a
video call by typing-latency rules, and called 8% loss merely POOR, which for audio is
wrecked. So `--profile` carries `quality_label` in the same table as the limits it sets --
label and numbers cannot drift apart, the same way `grade()` derives the colour and its
explanation from one table. Defaults are applied by parsing `--profile` first, folding it
into the parser defaults, then parsing again, so an explicit flag is set in the second
pass and naturally wins, with no precedence logic to maintain.

**`--dup-pct-fair` and `--wait` are deliberately not profiled.** Duplicate replies mean
link-layer retransmission whatever the traffic is, and a late reply still distinguishes
LATE from LOST, so there is no reason to stop waiting for one sooner.

**There is no throughput or streaming profile.** ICMP round-trip time cannot measure
bandwidth, and a profile implying otherwise would be a fabricated reading. Nor is the
sampling rate profiled: 5-second probes catch loss, freezes and spikes, but are far too
coarse to characterise packet-level jitter inside a call. `realtime` tightens the
thresholds; it does not turn this into a VoIP analyser.

## Probing

**The rate adapts, and instability drives it -- not the colour.** A link that is merely
slow but steady tells us nothing new by being probed harder. Only loss, late replies, bad
runs, duplicates or latency spikes justify the extra traffic. This matters on metered
mobile data: an earlier version pinned itself in fast mode for any FAIR verdict, so a
steadily-130 ms link would have burned ~19 MB/day forever.

**The spike test is on the tail, never on absolute p95.** An absolute p95 threshold is a
level, not a variability measure, so it reintroduces exactly the bug above by another
route: a steady 160 ms link sits above the `realtime` profile's `p95_fair` on every single
probe, latching fast probing on for as long as the link stays that way -- about 23 MB/day
spent re-confirming a number already on the screen. It was latent under `ssh` too, for any
link steadily above 400 ms; the profiles merely made it easy to hit. `spike_ms` is derived
as `p95_fair - median_fair`, the profile's own gap between an acceptable median and an
acceptable p95, floored at 50 ms so measurement noise cannot count as a spike. Deriving it
keeps a fourth set of numbers out of the profile table. Loss, late replies, bad runs and
duplicates still trigger fast probing on their very first occurrence.

**A prober with no target costs nothing.** `probes_per_second()` returns zero unless the
prober is actually sending, mirroring the condition `run()` uses to skip it. Counting a
configured-but-target-less prober -- a hop watcher with nothing to watch -- inflated
the reported data bill by ~20%.

**16-byte payload.** Payload size does not affect RTT measurement meaningfully and nearly
halves bytes per probe.

**The second public target is alert-only.** Its job is to disambiguate "this destination"
from "the internet", which is worth nothing while everything is healthy.

## Routing

**The target is resolved before the kernel is asked.** `ip route get` rejects a hostname
outright ("inet prefix is expected"), while `ping` accepts one happily. So a named target
left `first_hop()` returning nothing and silently deleted the entire fault-localisation
section of the menu -- no error, and only for the targets people are most likely to name.
The address is resolved once and cached, exactly as `ping` itself resolves once at startup,
so the ten-second route recheck never blocks the GTK loop on a slow DNS server. A failed
lookup is not cached, so it retries.

**`first_hop(target)`, not "the default route".** A machine can have several default
routes -- wifi, wired, a phone tether -- and more-specific or policy routes can send
different destinations out of different interfaces. Probing the wrong one produces the
worst possible outcome: a confident "local link is fine" about a link the monitored
traffic never touches. Ask the kernel with `ip route get <target>`.

**Tunnels are detected by asking the kernel, not by device name.** `link/none` (no
link-layer address) or `NOARP` (no shared medium with neighbours). Name prefixes fail:
a WireGuard interface can be called anything, and a name like `tundra-vpn` matches a
naive `tun*` test by pure coincidence. `NOARP` is the semantically load-bearing signal --
it is exactly the property that makes "a few ms to the router" a meaningless expectation.

**A device that has gone away is never cached as "not a tunnel".** `ip link show dev X`
does not raise when the device is missing: it exits non-zero and prints nothing. The first
version read only `.stdout`, so a missing device was recorded as a normal link, for good.
That matters because tunnel device names come back: OpenVPN hands out `tun0`, then `tun1`,
then `tun0` again across reconnects. A stale `False` would put LAN latency limits on a VPN
endpoint and report LOCAL LINK IS SLOW about a healthy tunnel -- the exact verdict this
function exists to prevent. The return code is now checked, and a failure is not cached.

Found by a test failing for a real reason: it named `tun0`, and by then the live tunnel had
reconnected as `tun1`. The test now seeds `_TUNNEL_CACHE` so that it does not depend on
which device exists at the moment it runs.

**A tunnel endpoint that has proven silent stops being probed.** Not calling it down was
the right call but only half the answer: the row still looked like a measurement, and the
probes still cost money. On the machine this was developed against, tun0's peer
`10.8.0.1` answers no ICMP at all, ever -- 0 replies to 5 pings at 0.3 s spacing --
while the tunnel carries traffic perfectly well. Probing that forever costs 0.38 MB/day at
calm rates and 3.80 MB/day while alerting, which is precisely when it would be running.
After `SILENT_PEER_PROBES` unanswered probes the prober is switched off and the row says
"does not answer ICMP, no longer probed", so the menu never implies a reading it does not
have. A changed endpoint clears the flag and gets a fresh chance.

This applies to tunnel endpoints only. Silence from a real first hop *is* the finding --
the local link is down -- so that one keeps being measured.

**A tunnel endpoint gets no local-link verdict, and is never called down.** It sits at
the far side of the whole internet path, so LAN thresholds would condemn a healthy VPN
permanently. Many endpoints also drop ICMP while carrying traffic fine -- and if a tunnel
really is down, the primary target's own line already says so.

**Many targets, each with its own first hop -- no tunnel case at all.** An earlier
version grew a `--tunnel-endpoint` flag, an "uplink" concept, `default_route_hop()` and
`tunnel_uplink()`, all to answer one question for VPN users: how much of the delay is the
path to the VPN, and how much is beyond it. Every one of those was a special case bolted
onto the general mechanism, and all of them were designed against a single machine.

`--target` given more than once answers the same question with nothing VPN-shaped in it:

    --target work.example.com   ->  54 ms   via 10.8.0.1 (tun0)
    --target 203.0.113.9        ->  51 ms   via 192.168.1.1 (wlp1s0)

The first row is the tunnelled path, the second is the underlay to the VPN server, and the
difference between them is the answer. "The uplink" stops being a concept: it is simply the
first hop of the second target. Four helpers and two flags were deleted, not added.

The only tunnel-specific logic left is `is_tunnel(device)`, which decides two things that
are true of any tunnel anywhere: a hop over a tunnel gets no LAN-latency verdict, and its
silence means nothing (see below).

**Only the first target is watched continuously.** It is the one that sets the icon and the
grade. The rest answer "is this one destination broken, or everything?", which is worth
nothing while the link is healthy and costs a metered allowance to ask. So extra targets and
their hops are probed only during an alert. Idle cost is unchanged by adding targets. The menu
still gives each unprobed target a "standby" row: a target the user asked for that silently
disappears reads as a flag that was ignored.

**The probe set is keyed by ADDRESS, not by role.** Several targets usually share a first
hop, and a hop can itself be a target. Keyed by role, such an address is probed once per
role and the allowance leaks. `_wanted_intervals()` returns one entry per unique address at
the fastest rate any role asks for, and `_apply_rates()` starts, retunes and stops probers
to match. The version this replaced had the bug: its gateway and uplink probers could sit
on the same address and probe it twice.

**A path change resets the window and respawns ping.** ping connects its socket, binding
a source address; after failover to another interface that source is wrong. The samples
already collected also describe a path that no longer exists -- without the reset, a
120-second window keeps averaging the old uplink into readings from the new one.

**A local-link verdict is only drawn where the sampling supports one.** The first hop is
probed slowly on purpose (`--hop-divisor`: every 20 s at calm defaults, at most six
samples in the window), yet it is judged against a tight 30 ms threshold. Two problems,
both measured rather than assumed. A handful of samples cannot support a confident verdict
at all, so `MIN_LOCAL_SAMPLES` withholds it -- the same policy as UNKNOWN at startup. And
a hop reached over wifi answers an isolated probe far more slowly than it answers a burst,
because the radio has to wake first: five bursts of three probes, 20 s apart, gave

    26.8  6.50  11.1        <- first probe pays a penalty the rest do not
    40.6  102   35.1
    228   228   528  383    <- and sometimes the link really is collapsing (4 replies
    14.7  11.3  13.6           for `-c 3`: a duplicate)
    107   9.33  5.70

Both effects are real, and they are not separable from six sparse samples. So in calm mode
the numbers are shown and no verdict is drawn; the verdict returns in alert mode, where the
hop is probed every 2 s. Nothing is lost, because a local link bad enough to matter makes
the primary target bad too, which is what raises alert mode.

An obvious improvement, deliberately not taken yet: probe the local hop in short
bursts (three back-to-back every 60 s) instead of isolated probes. Same bytes, far better
data -- it separates the wake-up penalty from the link, and surfaces duplicates. It needs
`Prober` to stop assuming one long-running `ping -i`, which is more surgery than this
release warrants.

**Only understood lines count as liveness.** ping emits `connect: Network is unreachable`
and exits when a route vanishes. Letting that refresh the liveness timestamp meant `stale`
never grew, so a dead link showed dim grey UNKNOWN forever instead of red DOWN.

**Probe outcomes are classified once, by `classify()`.** The design has always been
OK / LATE / LOST, but the code used to re-derive the deadline rule three separate times
inside `snapshot()` -- once in `_is_bad`, once for `lost`, once for `late`. The central
concept of the program was nowhere named. Every count now derives from one classifier.

**`snapshot()` returns a `Stats` NamedTuple, not a dict.** It is the most-read structure
in the program and the rest of the file already speaks in NamedTuples; a bare dict read by
string subscript in thirty-odd places was the odd one out.

**`_peer_lines()` has one renderer, not one per peer.** It was three near-copies of the
same `{median} ms, {bad_pct}% bad` row. That is not a hypothetical cost: the tunnel work
added the third copy and shipped the wrong no-reply wording in it, announcing TUNNEL IS
DOWN for a tunnel that was carrying traffic. A `Peer` descriptor now carries what differs
-- the role word, whether a verdict may be drawn, and what to say when it stays silent.

## Presentation

**Plain text only in the menu -- no Pango markup.** AppIndicator does not render the menu;
it exports it over `com.canonical.dbusmenu` and the panel rebuilds it with its own widgets
and font. Markup is stripped in transit, and leaves raw tags in the item's label property
where another panel implementation would display them literally. This also means column
alignment via space padding is impossible: the font is not ours to choose, so labels use
`Label: value` rather than pretending to be columns.

**Icons are regenerated at every start.** They are cheap (~1 ms for all six) and a
skip-if-exists cache would keep a stale icon forever after a palette change.

**The grade starts at UNKNOWN, not GOOD.** An empty window means nothing has been measured
yet; showing a confident green light for a link that has not been probed is a lie.
`UNKNOWN.rank` sorts below `GOOD` so startup can never trigger an alert.

## Packaging

**Native source format (`3.0 (native)`), version with no Debian revision.** Needs no
separate orig tarball, so `dpkg-buildpackage` works straight from a git checkout. Switch
to `3.0 (quilt)` and `X.Y.Z-1` only if pursuing Debian inclusion.

**Build-Depends are hand-written and must stay that way.** `dh_python3` sees neither
`gi.require_version()` nor `subprocess`, so it finds none of the real dependencies.
A missing entry produces a package that installs cleanly and crashes on launch -- which
is why CI installs the `.deb` on a clean runner and starts it under Xvfb.

**A missing tray binding exits with the package name, not a traceback.** `gi` reports only
"Namespace AyatanaAppIndicator3 not available", which does not tell anyone what to install.
`_load_appindicator()` tries Ayatana, then Canonical's original, then exits with the Debian
package name. This is what makes the deprecation below survivable rather than mysterious.

**The libayatana-appindicator deprecation warning is accepted, not silenced.** On 26.04
the library prints "libayatana-appindicator is deprecated. Please use
libayatana-appindicator-glib" to stderr at startup. Redirecting stderr to hide it would
also hide the ping failures and route errors this program deliberately reports there, so
it stays visible.

Migrating is not a swap, on two independent counts. The replacement typelib
(`AyatanaAppIndicatorGlib-2.0`, from `gir1.2-ayatanaappindicatorglib-2.0`) exists only in
26.04 universe -- it is absent from 24.04, which CI pins and which the `Architecture: all`
package targets so it installs on more machines. And the new library has dropped GTK
entirely: it depends only on gir1.2-gio-2.0 and gir1.2-gobject-2.0, and its typelib
contains no Gtk or Dbusmenu symbols at all, so `set_menu()` cannot be taking a `Gtk.Menu`.
The whole `_set_menu_lines()` row pool -- Gtk.MenuItem widgets, insensitive rows, a growing
pool -- would have to be rewritten against a menu model.

Revisit when 24.04 is no longer worth supporting, or if the old library is actually removed
rather than merely deprecated. Until then the fallback chain already prefers Ayatana over
Canonical's original AppIndicator3, which is the part that mattered.

**Pinned to `ubuntu-24.04`, `setuptools>=61`, PEP 621 `license = { text = ... }`.** A
pure-Python `Architecture: all` package built on the older distro installs on more
machines. PEP 639 (`license = "SPDX"`, `license-files`) needs setuptools >= 77, which the
runner does not have; it emits deprecation warnings on newer systems, which is the
accepted cost.

**Version stays 0.x until the grading defaults have survived other people's networks.**
Several were retuned during initial development in response to a single real link.
