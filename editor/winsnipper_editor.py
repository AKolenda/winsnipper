#!/usr/bin/env python3
"""Installed launcher: python3 <editor-dir>/winsnipper_editor.py [--capture [--mode MODE] [--delay SECONDS] | PNG …]."""
import sys


def main():
    try:
        from winsnipper.app import EditorApplication
    except (ImportError, ValueError) as error:
        print("WinSnipper needs Python GTK 4 and cairo bindings. On Ubuntu/Zorin, run:\n"
              "  apt install python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-4.0\n"
              f"Details: {error}", file=sys.stderr)
        return 1
    return EditorApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
