"""Tests for loading sweep files and merging experiment changes."""

import pytest
import yaml

from sweep.sweep_config import SweepConfigError, load_sweep, merge_changes, read_yaml


def test_merge_changes_replaces_only_named_options():
    base = {"dolphin": {"phase_linking": {"half_window": {"x": 20, "y": 5}, "ministack_size": 15}},
            "mintpy": {"mintpy.deramp": "quadratic"}}
    merged = merge_changes(base, {"dolphin": {"phase_linking": {"half_window": {"x": 11}}}})
    assert merged["dolphin"]["phase_linking"] == {"half_window": {"x": 11, "y": 5}, "ministack_size": 15}
    assert merged["mintpy"] == {"mintpy.deramp": "quadratic"}
    assert base["dolphin"]["phase_linking"]["half_window"]["x"] == 20  # default unchanged


def test_merge_changes_replaces_lists_and_allows_null():
    base = {"dolphin": {"snaphu_options": {"ntiles": [2, 2]}}, "mintpy": {"threshold": 0.6}}
    merged = merge_changes(base, {"dolphin": {"snaphu_options": {"ntiles": [1, 1]}},
                                  "mintpy": {"threshold": None}})
    assert merged["dolphin"]["snaphu_options"]["ntiles"] == [1, 1]
    assert merged["mintpy"]["threshold"] is None


def test_mintpy_keys_with_dots_are_merged_as_single_keys():
    base = {"mintpy": {"mintpy.deramp": "quadratic", "mintpy.plot": "yes"}}
    merged = merge_changes(base, {"mintpy": {"mintpy.deramp": "linear"}})
    assert merged["mintpy"] == {"mintpy.deramp": "linear", "mintpy.plot": "yes"}


def test_read_yaml_keeps_mintpy_values_as_text(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("a: 20080520_20090817\nb: 1:5\nc: 16\nd: 0.7\n")
    assert read_yaml(path) == {"a": "20080520_20090817", "b": "1:5", "c": 16, "d": 0.7}


def test_load_sweep_reads_experiments(sweep_setup):
    sweep = load_sweep(sweep_setup["sweep_file"])
    assert [experiment.name for experiment in sweep.experiments] == ["baseline", "hw11x5"]
    assert sweep.experiments[0].changes == {}
    assert sweep.keep_paths == ("mintpy/pic",)
    assert sweep.area_name == "vienna"


def _write_sweep(sweep_setup, experiments):
    """Replace the experiments of the fixture's sweep file."""
    raw = yaml.safe_load(sweep_setup["sweep_file"].read_text())
    raw["experiments"] = experiments
    sweep_setup["sweep_file"].write_text(yaml.safe_dump(raw))


@pytest.mark.parametrize("experiments, message", [
    ([{"name": "a"}, {"name": "a"}], "used twice"),
    ([{"name": "bad name/"}], "may only contain"),
    ([{"name": "a", "changes": {"isce": {"num_proc": 4}}}], "share one ISCE2 stack"),
    ([{"name": "a", "chnages": {}}], "unknown option"),
    ([], "non-empty list"),
])
def test_load_sweep_rejects_invalid_experiments(sweep_setup, experiments, message):
    _write_sweep(sweep_setup, experiments)
    with pytest.raises(SweepConfigError, match=message):
        load_sweep(sweep_setup["sweep_file"])


def test_relative_paths_are_resolved_against_the_sweep_file(sweep_setup, tmp_path):
    raw = yaml.safe_load(sweep_setup["sweep_file"].read_text())
    raw["results_dir"] = "my_results"
    sweep_setup["sweep_file"].write_text(yaml.safe_dump(raw))
    assert load_sweep(sweep_setup["sweep_file"]).results_dir == tmp_path / "my_results"
