"""Offline bootstrap shared by the shell and PowerShell entry points."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_collab.installer import main

sys.exit(main())
