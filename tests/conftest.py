"""Shared test fixtures: make the repository importable and build a fake sweep setup.

The fake setup replaces the real pipeline and postprocessing with tiny Python
scripts that only write the folders and files the real tools would write, so the
whole sweep workflow can be tested in seconds without SAR data.
"""

import sys
import textwrap
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Fake pipeline: "python -m isce_dolphin_mintpy run <config> --steps ..." for the
# runner, and "python -c <validation code> <config>" for the config check
# (both are run from the fake repo, so the fake package is imported).
FAKE_PIPELINE_MAIN = '''
import json, sys, yaml
from pathlib import Path
config_file = Path(sys.argv[2])
config = yaml.safe_load(config_file.read_text())
steps = sys.argv[sys.argv.index("--steps") + 1:]
work_dir = Path(config["paths"]["work_dir"])
if config["dolphin"].get("fail") and "dolphin" in steps:
    sys.exit(3)
if (config.get("mintpy") or {}).get("fail") and "mintpy" in steps:
    sys.exit(4)
if "crop" in steps:
    (work_dir / "crop").mkdir(parents=True, exist_ok=True)
if "dolphin" in steps:
    (work_dir / "dolphin").mkdir(parents=True, exist_ok=True)
    with open(work_dir / "logs" / "dolphin_runs.txt", "a") as runs:
        runs.write(json.dumps(config["dolphin"], sort_keys=True) + "\\n")
if "mintpy" in steps:
    if not (work_dir / "dolphin").is_dir():
        sys.exit(5)  # like the real pipeline: MintPy needs dolphin's output
    (work_dir / "mintpy" / "pic").mkdir(parents=True, exist_ok=True)
    (work_dir / "mintpy" / "pic" / "velocity.png").write_text("png")
if "subpixel" in steps and (config.get("subpixel") or {}).get("enabled"):
    (work_dir / "subpixel").mkdir(exist_ok=True)
(work_dir / "pipeline_config.yaml").write_text(config_file.read_text())
with open(work_dir / "logs" / "pipeline.log", "a") as log:
    log.write(f"run with {config['dolphin']} inversion={config.get('inversion')} steps={steps}\\n")
'''

FAKE_CONFIG_MODULE = '''
import yaml
from pathlib import Path
def load_config(path):
    config = yaml.safe_load(Path(path).read_text())
    if config["dolphin"].get("typo"):
        raise ValueError("Unknown option 'typo' in section 'dolphin'")
    return config
'''

FAKE_POSTPROCESSING = '''
import argparse, json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--run-dir"); parser.add_argument("--output-dir"); parser.add_argument("--area-name")
args = parser.parse_args()
out = Path(args.output_dir)
(out / "S1_ASC_073_IW1_VV_20200106_20241228_vienna.parquet").write_text("points")
(out / "S1_ASC_073_IW1_VV_20200106_20241228_vienna.metadata.json").write_text(json.dumps({"ok": True}))
'''


@pytest.fixture
def sweep_setup(tmp_path):
    """Create fake tools, a finished fake ISCE2 work dir, a default config and a sweep file.

    Returns
    -------
    dict
        Paths: ``sweep_file``, ``work_dir``, ``results_dir``, ``base_config``.
    """
    pipeline_repo = tmp_path / "pipeline"
    package = pipeline_repo / "isce_dolphin_mintpy"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(FAKE_PIPELINE_MAIN)
    (package / "config.py").write_text(FAKE_CONFIG_MODULE)

    postprocessing_repo = tmp_path / "postprocessing"
    postprocessing_repo.mkdir()
    (postprocessing_repo / "run_postprocessing.py").write_text(FAKE_POSTPROCESSING)

    work_dir = tmp_path / "work"
    for run_file in ("run_01_unpack", "run_02_merge"):
        (work_dir / "isce" / "run_files").mkdir(parents=True, exist_ok=True)
        (work_dir / "isce" / "run_files" / run_file).write_text("")
        (work_dir / "isce" / "run_status").mkdir(parents=True, exist_ok=True)
        (work_dir / "isce" / "run_status" / f"{run_file}.done").write_text("")
    (work_dir / "logs").mkdir()
    (work_dir / "logs" / "pipeline.log").write_text("ISCE2 history\n")

    base_config = tmp_path / "pipeline.yaml"
    base_config.write_text(yaml.safe_dump({
        "paths": {"work_dir": str(work_dir)},
        "isce": {"num_proc": 8},
        "dolphin": {"phase_linking": {"half_window": {"x": 20, "y": 5}, "ministack_size": 15}},
        "mintpy": {"mintpy.deramp": "quadratic", "mintpy.reference.date": "auto"},
    }, sort_keys=False))

    results_dir = tmp_path / "results"
    sweep_file = tmp_path / "sweep.yaml"
    sweep_file.write_text(textwrap.dedent(f"""
        base_config: {base_config}
        results_dir: {results_dir}
        area_name: vienna
        min_free_gb: 0
        pipeline:
          repo: {pipeline_repo}
          python: {sys.executable}
        postprocessing:
          repo: {postprocessing_repo}
          python: {sys.executable}
        experiments:
          - name: baseline
          - name: hw11x5
            changes:
              dolphin: {{phase_linking: {{half_window: {{x: 11, y: 5}}}}}}
    """))
    return {"sweep_file": sweep_file, "work_dir": work_dir,
            "results_dir": results_dir, "base_config": base_config}
