import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Tests must be free and deterministic: never call a paid API (and never read .env keys).
os.environ["SS_NO_DOTENV"] = "1"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
os.environ.pop("MOUSER_API_KEY", None)
os.environ.pop("CRUSTDATA_API_KEY", None)  # Crustdata tests set a fake key and a mocked HTTP layer
