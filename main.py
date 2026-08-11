#!/usr/bin/env python3
"""Punkt wejscia aplikacji TVHeadend GNOME Client."""
import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from ui.app import TvhGnomeApp


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    app = TvhGnomeApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
