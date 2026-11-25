"""Tests for the sweep workflow, run end to end with fake pipeline and postprocessing tools."""

import json

import pytest
import yaml

from sweep.runner import SweepError, run_sweep
from sweep.sweep_config import load_sweep
from sweep.work_dir import WorkDirError


def test_sweep_runs_every_experiment_and_cleans_up(sweep_setup):
    run_sweep(load_sweep(sweep_setup["sweep_file"]))

    work_dir = sweep_setup["work_dir"]
    for name in ("crop", "dolphin", "mintpy"):
        assert not (work_dir / name).exists()
    assert (work_dir / "isce").exists()

    result = sweep_setup["results_dir"] / "hw11x5"
    config = yaml.safe_load((result / "pipeline.yaml").read_text())
    assert config["dolphin"]["phase_linking"] == {"half_window": {"x": 11, "y": 5}, "ministack_size": 15}
    assert config["isce"] == {"num_proc": 8}
    assert yaml.safe_load((result / "changes.yaml").read_text()) == {
        "dolphin": {"phase_linking": {"half_window": {"x": 11, "y": 5}}}}
    assert len(list(result.glob("*.parquet"))) == 1
    assert len(list(result.glob("*.metadata.json"))) == 1
    assert (result / "mintpy_pic" / "velocity.png").exists()
    assert json.loads((result / "DONE.json").read_text())["experiment"] == "hw11x5"


def test_saved_log_holds_only_the_experiments_part(sweep_setup):
    run_sweep(load_sweep(sweep_setup["sweep_file"]))
    log = (sweep_setup["results_dir"] / "hw11x5" / "logs" / "pipeline.log").read_text()
    assert "ISCE2 history" not in log
    assert "'x': 11" in log and "'x': 20" not in log


def test_finished_experiments_are_skipped(sweep_setup):
    sweep = load_sweep(sweep_setup["sweep_file"])
    run_sweep(sweep)
    done_file = sweep_setup["results_dir"] / "baseline" / "DONE.json"
    first_finish = done_file.read_text()
    run_sweep(sweep)
    assert done_file.read_text() == first_finish


def test_invalid_config_fails_before_any_experiment_runs(sweep_setup):
    raw = yaml.safe_load(sweep_setup["sweep_file"].read_text())
    raw["experiments"].append({"name": "broken", "changes": {"dolphin": {"typo": 1}}})
    sweep_setup["sweep_file"].write_text(yaml.safe_dump(raw))
    with pytest.raises(SweepError, match="broken.*typo"):
        run_sweep(load_sweep(sweep_setup["sweep_file"]))
    assert not (sweep_setup["results_dir"] / "baseline").exists()


def test_failed_experiment_stops_sweep_and_keeps_outputs(sweep_setup):
    raw = yaml.safe_load(sweep_setup["sweep_file"].read_text())
    raw["experiments"].insert(1, {"name": "fails", "changes": {"dolphin": {"fail": True}}})
    sweep_setup["sweep_file"].write_text(yaml.safe_dump(raw))
    with pytest.raises(SweepError, match="fails.*pipeline failed"):
        run_sweep(load_sweep(sweep_setup["sweep_file"]))
    assert (sweep_setup["results_dir"] / "baseline" / "DONE.json").exists()
    assert not (sweep_setup["results_dir"] / "fails" / "DONE.json").exists()
    assert not (sweep_setup["results_dir"] / "hw11x5").exists()


def test_stale_outputs_block_the_start_unless_cleaned(sweep_setup):
    (sweep_setup["work_dir"] / "dolphin").mkdir()
    sweep = load_sweep(sweep_setup["sweep_file"])
    with pytest.raises(SweepError, match="--clean-stale"):
        run_sweep(sweep)
    run_sweep(sweep, clean_stale=True)
    assert (sweep_setup["results_dir"] / "hw11x5" / "DONE.json").exists()


def test_unfinished_isce_blocks_the_start(sweep_setup):
    (sweep_setup["work_dir"] / "isce" / "run_status" / "run_02_merge.done").unlink()
    with pytest.raises(WorkDirError, match="run_02_merge"):
        run_sweep(load_sweep(sweep_setup["sweep_file"]))


def test_dry_run_changes_nothing(sweep_setup):
    run_sweep(load_sweep(sweep_setup["sweep_file"]), dry_run=True)
    assert not (sweep_setup["results_dir"] / "baseline").exists()


def test_too_little_free_space_stops_the_sweep(sweep_setup):
    sweep = load_sweep(sweep_setup["sweep_file"])
    sweep.min_free_gb = 10 ** 9
    with pytest.raises(SweepError, match="min_free_gb"):
        run_sweep(sweep)


def _set_experiments(sweep_setup, experiments):
    """Replace the experiments of the fixture's sweep file."""
    raw = yaml.safe_load(sweep_setup["sweep_file"].read_text())
    raw["experiments"] = experiments
    sweep_setup["sweep_file"].write_text(yaml.safe_dump(raw))


def test_inversion_and_subpixel_can_change_per_experiment(sweep_setup):
    _set_experiments(sweep_setup, [
        {"name": "dolphin-inversion", "changes": {"inversion": "dolphin"}},
        {"name": "with-subpixel", "changes": {"subpixel": {"enabled": True}}},
    ])
    run_sweep(load_sweep(sweep_setup["sweep_file"]))

    results = sweep_setup["results_dir"]
    config = yaml.safe_load((results / "dolphin-inversion" / "pipeline.yaml").read_text())
    assert config["inversion"] == "dolphin"
    log = (results / "dolphin-inversion" / "logs" / "pipeline.log").read_text()
    assert "inversion=dolphin" in log
    assert "steps=['crop', 'dolphin', 'mintpy', 'subpixel']" in log
    config = yaml.safe_load((results / "with-subpixel" / "pipeline.yaml").read_text())
    assert config["subpixel"] == {"enabled": True}
    # subpixel/ is deleted with the other outputs after the export.
    assert not (sweep_setup["work_dir"] / "subpixel").exists()


def test_stale_subpixel_output_blocks_the_start(sweep_setup):
    (sweep_setup["work_dir"] / "subpixel").mkdir()
    with pytest.raises(SweepError, match="subpixel"):
        run_sweep(load_sweep(sweep_setup["sweep_file"]))
