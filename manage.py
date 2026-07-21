#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys


def main() -> None:
    src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
