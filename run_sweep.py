"""Command line entry point: run a parameter sweep of the isce2-dolphin-mintpy pipeline.

Usage::

    python run_sweep.py <sweep.yaml>                 # run all unfinished experiments
    python run_sweep.py <sweep.yaml> --dry-run       # only check and list them
    python run_sweep.py <sweep.yaml> --clean-stale   # delete old dolphin/mintpy outputs first
"""

import argparse
import logging
import sys

from sweep.runner import SWEEP_LOG_NAME, SweepError, run_sweep
from sweep.sweep_config import SweepConfigError, load_sweep
from sweep.work_dir import WorkDirError


def parse_arguments(argv=None):
    """Parse the command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sweep_file", help="Sweep YAML file (default config + experiments).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Check everything and list the experiments, but run nothing.")
    parser.add_argument("--clean-stale", action="store_true",
                        help="Delete dolphin/ and mintpy/ left over from an earlier run before starting.")
    return parser.parse_args(argv)


def setup_logging(log_file):
    """Log to the terminal and append to ``log_file``."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s sweep: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_file)],
    )


def main(argv=None):
    """Run the sweep; return a process exit code (0 ok, 1 failed, 2 bad sweep file)."""
    arguments = parse_arguments(argv)
    try:
        sweep = load_sweep(arguments.sweep_file)
    except (SweepConfigError, OSError) as error:
        print(f"Sweep file error: {error}", file=sys.stderr)
        return 2

    setup_logging(sweep.results_dir / SWEEP_LOG_NAME)
    try:
        run_sweep(sweep, clean_stale=arguments.clean_stale, dry_run=arguments.dry_run)
    except (SweepError, WorkDirError) as error:
        logging.getLogger("sweep").error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
