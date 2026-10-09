# SPDX-License-Identifier: GPL-3.0-or-later
"""Allow `python3 -m netquality` as well as the installed `netquality` command."""

from netquality.indicator import main

if __name__ == "__main__":
    main()
