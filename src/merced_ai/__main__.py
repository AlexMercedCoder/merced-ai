"""`python -m merced_ai`: the same CLI as the `merced-ai` command."""

import os
import sys

# `python -m` puts the working directory first on sys.path. Drop it so a project directory
# cannot shadow Merced AI's modules or its dependencies.
if sys.path and sys.path[0] in ("", os.getcwd()):
    sys.path.pop(0)

from merced_ai.cli import app  # noqa: E402

if __name__ == "__main__":
    app()
