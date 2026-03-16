import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Add blogging directory so blog_research_agent tools are importable
BLOGGING_DIR = ROOT / "blogging"
if str(BLOGGING_DIR) not in sys.path:
    sys.path.insert(0, str(BLOGGING_DIR))
