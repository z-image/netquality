#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate man/netquality.1 from the argparse parser, so the two cannot drift.

Usage: python3 tools/gen-manpage.py   (requires pandoc)
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from netquality import __version__
from netquality.indicator import parse_args  # noqa: E402

import argparse


def build_parser():
    """Re-create the parser without running it, by intercepting parse_args."""
    holder = {}
    real = argparse.ArgumentParser.parse_args

    def capture(self, *a, **kw):
        holder["parser"] = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture
    try:
        parse_args([])
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.parse_args = real
    return holder["parser"]


def options_markdown(parser):
    out = []
    for action in parser._actions:
        if not action.option_strings:
            continue
        flags = ", ".join(f"**{f}**" for f in action.option_strings)
        takes_value = action.nargs != 0
        if takes_value:
            flags += f" *{action.metavar or action.dest.upper()}*"
        help_text = (action.help or "").replace("%%", "%")
        default = action.default
        # argparse.SUPPRESS is a sentinel, not a real default -- never print it.
        if takes_value and default not in (None, False, True, argparse.SUPPRESS):
            help_text = f"{help_text}. Default: `{default}`" if help_text else f"Default: `{default}`"
        out.append(f"{flags}\n:   {help_text or 'See --help.'}\n")
    return "\n".join(out)


DOC = """% NETQUALITY(1) netquality {version} | User Commands

# NAME

netquality - tray indicator grading network quality for interactive use

# SYNOPSIS

**netquality** \\[**--target** *HOST*] \\[*options*]

# DESCRIPTION

**netquality** puts coloured signal bars in the system tray. It grades the
connection as good, fair, poor, bad or down.

**netquality** does not measure throughput. It measures the conditions that make
an interactive session usable. It puts each probe in one of three classes: OK,
LATE or LOST. The class depends on a deadline (**--deadline**). A reply that
comes too late to use is an impairment. It is not a success.

**netquality** also counts the longest run of bad probes in sequence. A frozen
session and scattered packet loss can have the same loss rate. The run length
tells them apart.

The worst impairment sets the grade. The menu gives the name of the rule that
set the colour. Thus the colour is always clear.

The correct deadline depends on the traffic. Therefore **--profile** sets the
deadline, the limits and the menu words together. Use *ssh* for typed commands,
*realtime* for voice and video, and *web* for a browser. A limit that you give on
the command line replaces the limit from the profile. There is no profile for
throughput, because ICMP round-trip time cannot measure bandwidth.

The probe rate changes with the conditions. The rate is slow when the link is
good. The rate increases when the link becomes unstable. A link that is slow but
steady does not increase the rate, because more probes give no new data. Thus you
can use **netquality** on a metered link. The idle cost is approximately 1.9 MB
each day.

You can give **--target** more than one time. The first target sets the icon and
the grade. **netquality** probes the other targets only during an alert, thus
more targets do not increase the cost when the link is good. When the link is good,
the menu shows each other target as "standby". More than one target shows if a
fault is one destination or all traffic.

**netquality** also probes the first hop to each target, and judges that hop
against stricter local limits. This shows if a fault is on your own link or after
it. **netquality** asks the kernel which hop it uses for each target. It does not
use the default route, which can be a different link on a machine with more than
one uplink. **netquality** probes each address one time, thus a hop that more than
one target uses is not probed two times.

You do not configure a tunnel. A tunnel is only a first hop that goes out through
a tunnel device. **netquality** does not apply local-link limits to such a hop,
and never reports it as down, because many tunnel endpoints discard ICMP but carry
traffic correctly. To find how much delay the path to a VPN adds, and how much the
tunnel adds, give the VPN server as a second **--target**.

**netquality** needs an AppIndicator host. On MATE, add the Indicator Applet
Complete to the panel. The Notification Area applet does not show **netquality**.

# OPTIONS

{options}

# EXAMPLES

Point it at a host you actually connect to; a public resolver can be healthy
while your real destination is not:

    netquality --target your.ssh.host

Reduce traffic on a metered mobile link to roughly 0.5 MB/day:

    netquality --calm-interval 15 --no-hops

Flag sub-second freezes, which the default 1000 ms deadline tolerates:

    netquality --deadline 500

# FILES

*$XDG_CACHE_HOME/netquality-indicator/*
:   Generated tray icons, one PNG per quality level. Regenerated at every start.

# SEE ALSO

**ping**(8), **mtr**(8), **ip-route**(8)

# BUGS

Report issues at <https://github.com/z-image/netquality/issues>.
"""

parser = build_parser()
markdown = DOC.format(version=__version__, options=options_markdown(parser))
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
subprocess.run(["pandoc", "-s", "-f", "markdown-smart", "-t", "man", "-o", os.path.join(here, "man", "netquality.1")],
               input=markdown, text=True, check=True)
# stderr, not stdout: this tool writes the file itself, so anything printed on stdout
# is silently prepended to man/netquality.1 by a well-meaning `> man/netquality.1`.
print("wrote man/netquality.1", file=sys.stderr)
