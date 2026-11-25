"""Checks and clean-up of the pipeline's work directory between experiments.

All experiments share one work directory (``paths.work_dir``). Its ``isce/``
folder and ``logs/`` always stay. The other outputs come in two stages:

- dolphin stage: ``crop/`` and ``dolphin/``. They depend on every config
  section except ``mintpy`` and ``subpixel``. After a successful dolphin stage
  the sweep writes ``dolphin/sweep_dolphin_settings.json`` with those settings,
  so a later experiment with the same settings can reuse the output.
- MintPy stage: ``mintpy/`` and ``subpixel/``. They are rebuilt for every
  experiment and deleted after its results are saved.
"""

import json
import shutil
from pathlib import Path

DOLPHIN_STAGE_DIRS = ("crop", "dolphin")
MINTPY_STAGE_DIRS = ("mintpy", "subpixel")
EXPERIMENT_OUTPUT_DIRS = DOLPHIN_STAGE_DIRS + MINTPY_STAGE_DIRS

# Config sections that only the MintPy and subpixel steps read. Every other
# section (dolphin, water_mask, inversion, paths, aoi, isce) can change the
# crop or dolphin output, so it is part of the dolphin settings.
MINTPY_STAGE_SECTIONS = ("mintpy", "subpixel")

# Written into dolphin/ after a successful dolphin stage. Its existence means the
# dolphin output is complete; its content says which settings produced it.
DOLPHIN_SETTINGS_FILE = "sweep_dolphin_settings.json"

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


def dolphin_settings_key(config):
    """Return the settings that determine the dolphin output, as comparable text.

    The key is the pipeline config without the MintPy-stage sections, written as
    JSON with sorted keys, so two configs give the same key exactly when their
    crop and dolphin outputs would be the same.
    """
    settings = {section: value for section, value in config.items()
                if section not in MINTPY_STAGE_SECTIONS}
    return json.dumps(settings, sort_keys=True, default=str)


def save_dolphin_settings(work_dir, config, experiment_name):
    """Mark the dolphin output in ``work_dir`` as complete and record its settings."""
    record = {"computed_by_experiment": experiment_name,
              "settings": json.loads(dolphin_settings_key(config))}
    (Path(work_dir) / "dolphin" / DOLPHIN_SETTINGS_FILE).write_text(json.dumps(record, indent=2) + "\n")


def read_dolphin_settings(work_dir):
    """Return the record written by ``save_dolphin_settings``, or None.

    None means there is no dolphin output, or it is not known to be complete
    (e.g. the dolphin step was interrupted, or it was not made by the sweep).
    """
    settings_file = Path(work_dir) / "dolphin" / DOLPHIN_SETTINGS_FILE
    if not settings_file.is_file():
        return None
    return json.loads(settings_file.read_text())


def saved_dolphin_settings_key(work_dir):
    """Return the settings key of the complete dolphin output in ``work_dir``, or None."""
    record = read_dolphin_settings(work_dir)
    if record is None:
        return None
    return json.dumps(record["settings"], sort_keys=True, default=str)


def find_stale_outputs(work_dir):
    """Return outputs in ``work_dir`` that the sweep cannot vouch for.

    These are ``mintpy/`` and ``subpixel/`` (always rebuilt, but possibly a
    manual run you want to keep) and a ``dolphin/`` without the sweep's settings
    file (incomplete or of unknown settings; dolphin would silently reuse its
    files). A complete dolphin output from an earlier sweep run is not stale:
    it is reused or replaced automatically.
    """
    work_dir = Path(work_dir)
    stale = [work_dir / name for name in MINTPY_STAGE_DIRS if (work_dir / name).exists()]
    if (work_dir / "dolphin").exists() and read_dolphin_settings(work_dir) is None:
        stale.append(work_dir / "dolphin")
    return stale


def remove_outputs(work_dir, names):
    """Delete the given output folders of ``work_dir``; ``isce/`` and ``logs/`` are never touched.

    Returns
    -------
    list of Path
        The folders that were deleted.
    """
    removed = []
    for name in names:
        if name not in EXPERIMENT_OUTPUT_DIRS:
            raise ValueError(f"Refusing to delete '{name}': not an experiment output folder.")
        folder = Path(work_dir) / name
        if folder.is_symlink():
            folder.unlink()
        elif folder.exists():
            shutil.rmtree(folder)
        else:
            continue
        removed.append(folder)
    return removed


def remove_experiment_outputs(work_dir):
    """Delete ``crop/``, ``dolphin/``, ``mintpy/`` and ``subpixel/`` in ``work_dir``."""
    return remove_outputs(work_dir, EXPERIMENT_OUTPUT_DIRS)


def free_space_gb(path):
    """Return the free disk space in GB of the file system holding ``path``.

    ``path`` itself may not exist yet; its nearest existing parent is used.
    """
    path = Path(path)
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free / BYTES_PER_GB
