# Keep the suite off the real ~/.whytokens (config.json, DB): config reads this at import.
import os
import tempfile

os.environ.setdefault("WHYTOKENS_HOME", tempfile.mkdtemp(prefix="whytokens-test-"))
os.environ.pop("WHYTOKENS_DB", None)
os.environ.pop("WHYTOKENS_EXTENSIONS", None)
