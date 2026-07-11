"""Allow the package to run with ``python -m timelapse``."""

from timelapse.cli import main

raise SystemExit(main())
