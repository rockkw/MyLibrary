#!/usr/bin/env python3
"""Run MyLibrary without installing:  python3 mylib.py <command>
(After `pip install -e .` the same CLI is available as `mylib`.)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from mylibrary.cli import main  # noqa: E402

sys.exit(main())
