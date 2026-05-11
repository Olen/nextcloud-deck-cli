"""
Make the project root importable so tests can `import imap_deck_sync`
without installing the project.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
