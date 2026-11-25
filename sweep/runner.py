"""Run all experiments of a sweep, one after another, always with the same workflow.

For every experiment that is not finished yet:

1. write its full pipeline config (default + changes) into its results folder,
2. run the pipeline steps crop, dolphin and mintpy,
3. export the result with the postprocessing (Parquet + metadata JSON),
4. save the experiment's part of the pipeline logs and the ``keep_paths``,
5. write the ``DONE`` marker, then delete crop/, dolphin/ and mintpy/.

Before the first experiment, every experiment config is checked with the
pipeline's own config loader, so a typo fails at once and not after hours.
If anything fails, the sweep stops and keeps the outputs for debugging.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .sweep_config import build_experiment_config, read_yaml, write_yaml
from .work_dir import (
    check_isce_finished,
    find_stale_outputs,
    free_space_gb,
    remove_experiment_outputs,
)

logger = logging.getLogger(__name__)

PIPELINE_STEPS = ("crop", "dolphin", "mintpy")
PIPELINE_MODULE = "isce_dolphin_mintpy"
POSTPROCESSING_SCRIPT = "run_postprocessing.py"

# Files written into every experiment's results folder.
CONFIG_FILE_NAME = "pipeline.yaml"
CHANGES_FILE_NAME = "changes.yaml"
DONE_FILE_NAME = "DONE.json"
LOG_FOLDER_NAME = "logs"
SWEEP_LOG_NAME = "sweep.log"

# Pipeline log files that grow with every run. Only the part written during an
# experiment is copied into its results folder.
APPENDED_LOG_FILES = ("pipeline.log", "dolphin.log", "mintpy.log")
# Small files the pipeline rewrites on every run; copied as a whole.
COPIED_LOG_FILES = ("versions.json",)

# Environment variables starting with this prefix are stored by the
# postprocessing in the metadata (section "run").
METADATA_ENV_PREFIX = "INSAR_RUN_"

# Checks the config exactly like the pipeline does when it starts.
VALIDATE_CONFIG_CODE = (
    "import sys; from isce_dolphin_mintpy.config import load_config; load_config(sys.argv[1])"
)


class SweepError(RuntimeError):
    """Raised when the sweep cannot start or an experiment fails."""


@dataclass
class ExperimentPlan:
    """An experiment together with its full config and results folder."""

    name: str
    description: str
    changes: dict
    config: dict
    results_dir: Path

    @property
    def is_done(self):
        """True if this experiment finished in an earlier sweep run."""
        return (self.results_dir / DONE_FILE_NAME).exists()


def plan_experiments(sweep):
    """Build the full config and results folder of every experiment.

    Parameters
    ----------
    sweep : SweepConfig
        The loaded sweep file.

    Returns
    -------
    list of ExperimentPlan
        In the order of the sweep file.
    """
    base_config = read_yaml(sweep.base_config)
    return [
        ExperimentPlan(
            name=experiment.name,
            description=experiment.description,
            changes=experiment.changes,
            config=build_experiment_config(base_config, experiment),
            results_dir=sweep.results_dir / experiment.name,
        )
        for experiment in sweep.experiments
    ]


def work_dir_of(config):
    """Return the pipeline work directory named in a pipeline config.

    Raises
    ------
    SweepError
        If ``paths.work_dir`` is missing or relative (the pipeline would resolve
        a relative path against its own working directory, which the sweep
        cannot know for certain).
    """
    work_dir = (config.get("paths") or {}).get("work_dir")
    if not work_dir:
        raise SweepError("The default config has no paths.work_dir.")
    work_dir = Path(work_dir).expanduser()
    if not work_dir.is_absolute():
        raise SweepError(f"paths.work_dir must be an absolute path for the sweep, got {work_dir}.")
    return work_dir


def run_command(command, cwd, log_message, env=None):
    """Run one external command; its output goes straight to the terminal.

    Raises
    ------
    SweepError
        If the command exits with an error.
    """
    logger.info("%s: %s", log_message, " ".join(str(part) for part in command))
    result = subprocess.run([str(part) for part in command], cwd=cwd, env=env)
    if result.returncode != 0:
        raise SweepError(f"{log_message} failed with exit code {result.returncode}.")


def check_tools(sweep):
    """Raise if a tool's repository, Python or entry point does not exist."""
    checks = [
        (sweep.pipeline.python, "pipeline.python"),
        (sweep.pipeline.repo / PIPELINE_MODULE, "pipeline.repo (isce_dolphin_mintpy package)"),
        (sweep.postprocessing.python, "postprocessing.python"),
        (sweep.postprocessing.repo / POSTPROCESSING_SCRIPT, "postprocessing.repo (run_postprocessing.py)"),
        (sweep.base_config, "base_config"),
    ]
    for path, option in checks:
        if not path.exists():
            raise SweepError(f"{option} does not exist: {path}")


def validate_configs(sweep, plans):
    """Check every experiment config with the pipeline's own config loader.

    The configs are written to a temporary folder for the check, so nothing in
    the results folder changes.

    Raises
    ------
    SweepError
        Naming the first experiment whose config the pipeline rejects.
    """
    with tempfile.TemporaryDirectory(prefix="sweep_check_") as temp_dir:
        for plan in plans:
            config_file = Path(temp_dir) / f"{plan.name}.yaml"
            write_yaml(plan.config, config_file)
            result = subprocess.run(
                [str(sweep.pipeline.python), "-c", VALIDATE_CONFIG_CODE, str(config_file)],
                cwd=sweep.pipeline.repo, capture_output=True, text=True,
            )
            if result.returncode != 0:
                message = (result.stderr.strip().splitlines() or ["(no message)"])[-1]
                raise SweepError(f"Experiment '{plan.name}': the pipeline rejects its config: {message}")
            logger.info("Config of '%s' is valid", plan.name)


def log_sizes(log_dir):
    """Return the current size of each appended pipeline log (0 if missing)."""
    return {name: (log_dir / name).stat().st_size if (log_dir / name).exists() else 0
            for name in APPENDED_LOG_FILES}


def save_logs(log_dir, sizes_before, target_dir):
    """Copy the experiment's part of the pipeline logs into ``target_dir``.

    Parameters
    ----------
    log_dir : Path
        The pipeline's ``<work_dir>/logs``.
    sizes_before : dict
        Log sizes from ``log_sizes`` taken before the experiment started.
    target_dir : Path
        Folder to write the logs to (created if needed).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, start in sizes_before.items():
        source = log_dir / name
        if not source.exists():
            continue
        with open(source, "rb") as log_file:
            log_file.seek(start)
            (target_dir / name).write_bytes(log_file.read())
    for name in COPIED_LOG_FILES:
        if (log_dir / name).exists():
            shutil.copy2(log_dir / name, target_dir / name)


def save_keep_paths(work_dir, keep_paths, target_dir):
    """Copy the configured extra outputs (e.g. ``mintpy/pic``) into ``target_dir``.

    A missing path only gives a warning, because e.g. ``mintpy/pic`` exists only
    with ``mintpy.plot: yes``.
    """
    for keep_path in keep_paths:
        source = work_dir / keep_path
        target = target_dir / keep_path.replace("/", "_")
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            shutil.copy2(source, target)
        else:
            logger.warning("keep_paths: %s does not exist, not saved", source)


def find_exported_files(results_dir):
    """Return the Parquet and metadata files the postprocessing wrote.

    Raises
    ------
    SweepError
        If not exactly one of each exists.
    """
    parquet_files = sorted(results_dir.glob("*.parquet"))
    metadata_files = sorted(results_dir.glob("*.metadata.json"))
    if len(parquet_files) != 1 or len(metadata_files) != 1:
        raise SweepError(
            f"Expected one .parquet and one .metadata.json in {results_dir}, found "
            f"{[path.name for path in parquet_files + metadata_files]}."
        )
    return parquet_files[0], metadata_files[0]


def prepare_results_dir(plan):
    """Create an empty results folder for the experiment.

    A folder without ``DONE.json`` is left over from a failed attempt. It only
    holds files the sweep wrote, so it is replaced.
    """
    if plan.results_dir.exists():
        logger.warning("Replacing unfinished results of an earlier attempt: %s", plan.results_dir)
        shutil.rmtree(plan.results_dir)
    plan.results_dir.mkdir(parents=True)


def run_experiment(sweep, plan, work_dir):
    """Run the full workflow for one experiment (see the module docstring).

    Raises
    ------
    SweepError
        If a step fails. The work directory and the partial results are kept.
    """
    started = time.time()
    prepare_results_dir(plan)
    config_file = plan.results_dir / CONFIG_FILE_NAME
    write_yaml(plan.config, config_file)
    write_yaml(plan.changes, plan.results_dir / CHANGES_FILE_NAME)

    # crop/ is rebuilt by the crop step; a leftover could hold another stride's window.
    remove_experiment_outputs(work_dir)
    sizes_before = log_sizes(work_dir / "logs")

    run_command([sweep.pipeline.python, "-m", PIPELINE_MODULE, "run", config_file,
                 "--steps", *PIPELINE_STEPS, *sweep.pipeline.extra_args],
                cwd=sweep.pipeline.repo, log_message=f"[{plan.name}] pipeline")

    export_command = [sweep.postprocessing.python, POSTPROCESSING_SCRIPT,
                      "--run-dir", work_dir, "--output-dir", plan.results_dir]
    if sweep.area_name:
        export_command += ["--area-name", sweep.area_name]
    environment = dict(os.environ, **{
        f"{METADATA_ENV_PREFIX}EXPERIMENT": plan.name,
        f"{METADATA_ENV_PREFIX}SWEEP_FILE": str(sweep.sweep_file),
    })
    run_command(export_command + sweep.postprocessing.extra_args, cwd=sweep.postprocessing.repo,
                log_message=f"[{plan.name}] export", env=environment)
    parquet_file, metadata_file = find_exported_files(plan.results_dir)

    save_logs(work_dir / "logs", sizes_before, plan.results_dir / LOG_FOLDER_NAME)
    save_keep_paths(work_dir, sweep.keep_paths, plan.results_dir)

    # Written last: its existence means the results folder is complete.
    done = {
        "experiment": plan.name,
        "description": plan.description,
        "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_hours": round((time.time() - started) / 3600, 2),
        "parquet": parquet_file.name,
        "metadata": metadata_file.name,
    }
    (plan.results_dir / DONE_FILE_NAME).write_text(json.dumps(done, indent=2) + "\n")

    removed = remove_experiment_outputs(work_dir)
    logger.info("[%s] done in %.1f h; deleted %s", plan.name, done["duration_hours"],
                ", ".join(path.name for path in removed))


def check_free_space(work_dir, min_free_gb):
    """Raise if the disk of ``work_dir`` has less than ``min_free_gb`` free."""
    free_gb = free_space_gb(work_dir)
    if free_gb < min_free_gb:
        raise SweepError(
            f"Only {free_gb:.0f} GB free on the disk of {work_dir}, need at least {min_free_gb:.0f} GB "
            "(min_free_gb in the sweep file)."
        )


def prepare_sweep(sweep, clean_stale=False):
    """Run all checks that must pass before the first experiment starts.

    Parameters
    ----------
    sweep : SweepConfig
        The loaded sweep file.
    clean_stale : bool
        Delete dolphin/ and mintpy/ outputs left over from an earlier run
        instead of refusing to start.

    Returns
    -------
    tuple (list of ExperimentPlan, Path)
        The experiments and the shared work directory.

    Raises
    ------
    SweepError, WorkDirError
        On the first problem found.
    """
    check_tools(sweep)
    plans = plan_experiments(sweep)
    work_dir = work_dir_of(plans[0].config)
    check_isce_finished(work_dir)
    validate_configs(sweep, [plan for plan in plans if not plan.is_done])

    stale = find_stale_outputs(work_dir)
    if stale and not clean_stale:
        raise SweepError(
            f"Old outputs exist: {[str(path) for path in stale]}. dolphin would reuse them instead "
            "of computing the new settings. Save what you need, then start again with --clean-stale "
            "to delete them."
        )
    if stale:
        logger.warning("Deleting old outputs: %s", ", ".join(str(path) for path in stale))
        remove_experiment_outputs(work_dir)
    return plans, work_dir


def run_sweep(sweep, clean_stale=False, dry_run=False):
    """Check the sweep, then run every unfinished experiment in order.

    Parameters
    ----------
    sweep : SweepConfig
        The loaded sweep file.
    clean_stale : bool
        See ``prepare_sweep``.
    dry_run : bool
        Only run the checks and list what would run.

    Raises
    ------
    SweepError, WorkDirError
        If a check or an experiment fails; later experiments are not started.
    """
    plans, work_dir = prepare_sweep(sweep, clean_stale=clean_stale)
    todo = [plan for plan in plans if not plan.is_done]
    for plan in plans:
        logger.info("%-30s %s", plan.name, "done, skipped" if plan.is_done else "to run")
    if dry_run:
        logger.info("Dry run: %d of %d experiments would run.", len(todo), len(plans))
        return
    for number, plan in enumerate(todo, start=1):
        check_free_space(work_dir, sweep.min_free_gb)
        logger.info("=== Experiment %d/%d: %s ===", number, len(todo), plan.name)
        run_experiment(sweep, plan, work_dir)
    logger.info("Sweep finished: %d experiments run, results in %s", len(todo), sweep.results_dir)
