"""WorkBuddy's existing entry point, now routed through the control panel owner."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
os.environ.setdefault('ANONYMIZED_TELEMETRY', 'false')
if __name__ == '__main__':
    if sys.argv[1:] not in ([], ['serve']):
        print('Panel mode supports launcher.py serve only.', file=sys.stderr)
        raise SystemExit(2)
    from panel_gateway import main
    main()
