import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

# Every test that touches the DB gets its own file. Importing jobpipe.config
# resolves DB_PATH at import time, so this has to happen before that import.
os.environ.setdefault("JOBPIPE_DB", tempfile.mktemp(suffix=".db"))
