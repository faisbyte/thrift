"""Entry point for the UserPromptSubmit hook (stdlib only).

Kept as a file next to the package (instead of ``-m thriftctx...``) so it can
run under ``python3 -S`` from any interpreter >= 3.9 without the venv.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

try:
    from thriftctx.hook_prompt import main
except Exception:  # never surface errors to the user's prompt
    sys.exit(0)

main()
