"""Checks and clean-up of the pipeline's work directory between experiments.

All experiments share one work directory (``paths.work_dir``). Its ``isce/``
folder and ``logs/`` stay; ``crop/``, ``dolphin/``, ``mintpy/`` and ``subpixel/``
belong to the current experiment and are deleted after its results are saved.
"""

import shutil
from pathlib import Path

# Folders created by the crop, dolphin, MintPy and subpixel steps of one experiment.
# crop/ is small, but its window depends on dolphin's strides and its water
# fraction on the water_mask options, so it is rebuilt for every experiment.
EXPERIMENT_OUTPUT_DIRS = ("crop", "dolphin", "mintpy", "subpixel")

# Outputs left over from an earlier run. dolphin would reuse its files silently,
# and the export would pick up an old subpixel/ in an experiment without the
# subpixel step, so the sweep refuses to start while they exist.
STALE_OUTPUT_DIRS = ("dolphin", "mintpy", "subpixel")

BYTES_PER_GB = 1024 ** 3


class WorkDirError(RuntimeError):
    """Raised when the work directory is not ready for the next experiment."""


def check_isce_finished(work_dir):
    """Raise unless every ISCE2 run file of ``<work_dir>/isce`` is marked done.

    The pipeline marks a finished topsStack run file with
    ``isce/run_status/<run file>.done``.

    Raises
    ------
    WorkDirError
        If there are no run files or some are not finished yet.
    """
    isce_dir = Path(work_dir) / "isce"
    run_files = sorted(path.name for path in (isce_dir / "run_files").glob("run_*")
                       if not path.name.endswith(".job"))
    if not run_files:
        raise WorkDirError(f"No ISCE2 run files in {isce_dir / 'run_files'}. Run the 'isce' step first.")
    missing = [name for name in run_files
               if not (isce_dir / "run_status" / f"{name}.done").exists()]
    if missing:
        raise WorkDirError(
            f"The ISCE2 step is not finished: {len(missing)} of {len(run_files)} run files are not "
            f"done yet (next: {missing[0]}). Wait until the 'isce' step has finished."
        )


def find_stale_outputs(work_dir):
    """Return the dolphin/MintPy output folders that already exist in ``work_dir``."""
    return [Path(work_dir) / name for name in STALE_OUTPUT_DIRS if (Path(work_dir) / name).exists()]


def remove_experiment_outputs(work_dir):
    """Delete ``crop/``, ``dolphin/``, ``mintpy/`` and ``subpixel/`` in ``work_dir``.

    Only these folders are touched; ``isce/`` and ``logs/`` stay.

    Returns
    -------
    list of Path
        The folders that were deleted.
    """
    removed = []
    for name in EXPERIMENT_OUTPUT_DIRS:
        folder = Path(work_dir) / name
        if folder.is_symlink():
            folder.unlink()
        elif folder.exists():
            shutil.rmtree(folder)
        else:
            continue
        removed.append(folder)
    return removed


def free_space_gb(path):
    """Return the free disk space in GB of the file system holding ``path``.

    ``path`` itself may not exist yet; its nearest existing parent is used.
    """
    path = Path(path)
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free / BYTES_PER_GB
