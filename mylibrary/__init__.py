"""MyLibrary — local, LLM-assisted movie library organizer.

Design rule: this package can create folders, create hard links (safety
snapshots) and rename files without overwriting. It contains no code that
deletes files. tests/test_no_delete.py fails the build if that changes.
"""

__version__ = "0.1.0"
