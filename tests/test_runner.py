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
    with pytest.raises(SweepError, match="fails.*pipeline crop dolphin failed"):
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
    assert "steps=['crop', 'dolphin']" in log and "steps=['mintpy', 'subpixel']" in log
    config = yaml.safe_load((results / "with-subpixel" / "pipeline.yaml").read_text())
    assert config["subpixel"] == {"enabled": True}
    # subpixel/ is deleted with the other outputs after the export.
    assert not (sweep_setup["work_dir"] / "subpixel").exists()


def test_stale_subpixel_output_blocks_the_start(sweep_setup):
    (sweep_setup["work_dir"] / "subpixel").mkdir()
    with pytest.raises(SweepError, match="subpixel"):
        run_sweep(load_sweep(sweep_setup["sweep_file"]))


def _dolphin_runs(sweep_setup):
    """Return the dolphin settings of every dolphin run of the fake pipeline, in order."""
    runs_file = sweep_setup["work_dir"] / "logs" / "dolphin_runs.txt"
    return runs_file.read_text().splitlines() if runs_file.exists() else []


def _done(sweep_setup, name):
    """Return the DONE.json of an experiment."""
    return json.loads((sweep_setup["results_dir"] / name / "DONE.json").read_text())


LINEAR_RAMP = {"mintpy": {"mintpy.deramp": "linear"}}
SMALL_WINDOW = {"dolphin": {"phase_linking": {"half_window": {"x": 11, "y": 5}}}}


def test_mintpy_only_changes_reuse_dolphin(sweep_setup):
    _set_experiments(sweep_setup, [
        {"name": "baseline"},
        {"name": "linear-ramp", "changes": LINEAR_RAMP},
        {"name": "no-coherence-mask", "changes": {"mintpy": {"dolphin_temporal_coherence_threshold": None}}},
    ])
    run_sweep(load_sweep(sweep_setup["sweep_file"]))

    assert len(_dolphin_runs(sweep_setup)) == 1
    assert _done(sweep_setup, "baseline")["dolphin"] == "computed"
    for name in ("linear-ramp", "no-coherence-mask"):
        assert _done(sweep_setup, name)["dolphin"] == "reused"
        assert _done(sweep_setup, name)["dolphin_computed_by_experiment"] == "baseline"
    config = yaml.safe_load((sweep_setup["results_dir"] / "linear-ramp" / "pipeline.yaml").read_text())
    assert config["mintpy"]["mintpy.deramp"] == "linear"


@pytest.mark.parametrize("changes", [
    SMALL_WINDOW,
    {"water_mask": {"max_water_fraction": 0.5}},
    {"inversion": "dolphin"},
])
def test_changes_that_affect_dolphin_recompute_it(sweep_setup, changes):
    _set_experiments(sweep_setup, [{"name": "baseline"}, {"name": "changed", "changes": changes}])
    run_sweep(load_sweep(sweep_setup["sweep_file"]))
    assert len(_dolphin_runs(sweep_setup)) == 2
    assert _done(sweep_setup, "changed")["dolphin"] == "computed"


def test_subpixel_change_reuses_dolphin(sweep_setup):
    _set_experiments(sweep_setup, [
        {"name": "baseline"},
        {"name": "with-subpixel", "changes": {"subpixel": {"enabled": True}}},
    ])
    run_sweep(load_sweep(sweep_setup["sweep_file"]))
    assert len(_dolphin_runs(sweep_setup)) == 1
    assert _done(sweep_setup, "with-subpixel")["dolphin"] == "reused"


def test_experiments_with_the_same_dolphin_settings_run_together(sweep_setup):
    _set_experiments(sweep_setup, [
        {"name": "a-baseline"},
        {"name": "b-small-window", "changes": SMALL_WINDOW},
        {"name": "c-baseline-linear", "changes": LINEAR_RAMP},
        {"name": "d-small-window-linear", "changes": {**SMALL_WINDOW, **LINEAR_RAMP}},
    ])
    run_sweep(load_sweep(sweep_setup["sweep_file"]))

    # In file order (a, b, c, d) dolphin would run 4 times; grouped (a, c, b, d) only twice.
    assert len(_dolphin_runs(sweep_setup)) == 2
    assert _done(sweep_setup, "c-baseline-linear")["dolphin_computed_by_experiment"] == "a-baseline"
    assert _done(sweep_setup, "d-small-window-linear")["dolphin_computed_by_experiment"] == "b-small-window"


def test_outputs_are_deleted_after_the_sweep_by_default(sweep_setup):
    _set_experiments(sweep_setup, [{"name": "baseline"}, {"name": "linear", "changes": LINEAR_RAMP}])
    run_sweep(load_sweep(sweep_setup["sweep_file"]))
    for name in ("crop", "dolphin", "mintpy", "subpixel"):
        assert not (sweep_setup["work_dir"] / name).exists()


def test_kept_dolphin_output_is_reused_by_experiments_added_later(sweep_setup):
    raw = yaml.safe_load(sweep_setup["sweep_file"].read_text())
    raw["keep_dolphin_after_sweep"] = True
    raw["experiments"] = [{"name": "baseline"}]
    sweep_setup["sweep_file"].write_text(yaml.safe_dump(raw))
    run_sweep(load_sweep(sweep_setup["sweep_file"]))
    assert (sweep_setup["work_dir"] / "dolphin").is_dir()
    assert not (sweep_setup["work_dir"] / "mintpy").exists()

    raw["experiments"].append({"name": "linear", "changes": LINEAR_RAMP})
    sweep_setup["sweep_file"].write_text(yaml.safe_dump(raw))
    run_sweep(load_sweep(sweep_setup["sweep_file"]))
    assert len(_dolphin_runs(sweep_setup)) == 1
    assert _done(sweep_setup, "linear")["dolphin"] == "reused"


def test_restart_after_a_mintpy_failure_reuses_the_finished_dolphin_output(sweep_setup):
    _set_experiments(sweep_setup, [{"name": "broken-mintpy", "changes": {"mintpy": {"fail": True}}}])
    with pytest.raises(SweepError, match="pipeline mintpy"):
        run_sweep(load_sweep(sweep_setup["sweep_file"]))
    assert (sweep_setup["work_dir"] / "dolphin").is_dir()

    _set_experiments(sweep_setup, [{"name": "broken-mintpy", "changes": {"mintpy": {"fail": False}}}])
    run_sweep(load_sweep(sweep_setup["sweep_file"]))  # mintpy/ was never created, so nothing is stale
    assert len(_dolphin_runs(sweep_setup)) == 1
    assert _done(sweep_setup, "broken-mintpy")["dolphin"] == "reused"
    assert _done(sweep_setup, "broken-mintpy")["dolphin_computed_by_experiment"] == "broken-mintpy"


def test_incomplete_dolphin_output_is_stale(sweep_setup):
    (sweep_setup["work_dir"] / "dolphin").mkdir()  # no settings file: interrupted or manual
    with pytest.raises(SweepError, match="--clean-stale"):
        run_sweep(load_sweep(sweep_setup["sweep_file"]))
