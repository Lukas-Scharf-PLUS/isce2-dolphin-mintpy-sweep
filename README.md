# isce2-dolphin-mintpy-sweep

Parameter studies for the [isce2-dolphin-mintpy](../isce2-dolphin-mintpy) InSAR pipeline on the ubuntu-server.

You keep **one default `pipeline.yaml`** and write a **list of experiments**. Each experiment lists only the
dolphin or MintPy options it changes; all other options stay as in the default. For every experiment the sweep
runs exactly the same workflow:

1. write the experiment's full config (default + changes),
2. **dolphin stage** (`crop`, `dolphin`): reused if the previous experiment had the same dolphin settings,
   otherwise deleted and recomputed (see [Reusing dolphin output](#reusing-dolphin-output)),
3. **MintPy stage** (`mintpy`, `subpixel`): always run (the pipeline skips `subpixel` unless
   `subpixel.enabled: true`),
4. export the result with [isce2-dolphin-minpty_postprocessing](../isce2-dolphin-minpty_postprocessing)
   (GeoParquet + metadata JSON),
5. save it in `<results_dir>/<experiment name>/`,
6. delete `mintpy/` and `subpixel/`. The disk only ever holds one dolphin output and one MintPy output.

All experiments share one ISCE2 stack, which must be finished before the sweep starts.

This project only calls the two other repositories. It is meant for the server, not for the EOX cloud (that is
`ADUCAT_argo`).

## Setup

No environment of its own: the sweep only needs `pyyaml` (and `pytest` for the tests), which the pipeline
environment already has. It needs two conda environments, given by their Python in the sweep file:

| Environment | Created from | Used for |
| --- | --- | --- |
| isce2-dolphin-mintpy | `isce2-dolphin-mintpy/environment.yml` | the pipeline, and running the sweep itself |
| mintpy-postprocessing | `isce2-dolphin-minpty_postprocessing/environment.yaml` | the export |

```bash
export CONDA_PKGS_DIRS=/media/ssd2tb/lukas/conda_pkgs
mamba env create -f ../isce2-dolphin-minpty_postprocessing/environment.yaml \
      -p /media/ssd2tb/lukas/envs/mintpy-postprocessing
```

No environment variables are required.

## How to run

Start it in `tmux`, so it keeps running when the connection drops:

```bash
tmux new -s sweep
conda activate /media/ssd2tb/lukas/envs/isce2-dolphin-mintpy
cd /media/ssd2tb/lukas/code/isce2-dolphin-mintpy-sweep

python run_sweep.py /media/ssd2tb/lukas/ADUCAT/configs/sweep_S1_073_ASC.yaml --dry-run   # check only
python run_sweep.py /media/ssd2tb/lukas/ADUCAT/configs/sweep_S1_073_ASC.yaml             # run
```

| Option | Meaning |
| --- | --- |
| `--dry-run` | Run all checks and list which experiments would run, without running anything |
| `--clean-stale` | Delete leftover outputs the sweep cannot vouch for (`mintpy/`, `subpixel/`, an incomplete `dolphin/`) before starting |

**Adding experiments later:** append them to the sweep file and start the same command again. Finished
experiments (their folder has `DONE.json`) are skipped. The same applies after a crash or reboot.

**After a failed experiment:** the sweep stops and keeps the outputs for debugging. Look at the logs, fix the
cause, then start again (with `--clean-stale` if it asks for it). The failed experiment runs again; if its
dolphin stage had finished and you did not change dolphin settings, that output is reused.

## Sweep file

See [config/example_sweep.yaml](config/example_sweep.yaml) for every option.

```yaml
base_config: /media/ssd2tb/lukas/ADUCAT/configs/pipeline.yaml
results_dir: /media/ssd2tb/lukas/ADUCAT/results/S1_073_ASC
area_name: vienna
pipeline:       {repo: ..., python: .../envs/isce2-dolphin-mintpy/bin/python}
postprocessing: {repo: ..., python: .../envs/mintpy-postprocessing/bin/python, extra_args: []}
keep_paths: [mintpy/pic]     # extra outputs to keep (relative to work_dir)
min_free_gb: 300             # don't start an experiment with less free disk space
keep_dolphin_after_sweep: false   # true: keep crop/ + dolphin/ for experiments added later
experiments:
  - name: baseline           # no changes: the default itself
  - name: hw11x5_linear
    description: smaller window, linear ramp
    changes:
      dolphin: {phase_linking: {half_window: {x: 11, y: 5}}}
      mintpy:  {mintpy.deramp: linear}
```

How changes are merged into the default:

- Mappings are merged key by key at every level, so `half_window: {x: 11}` changes only `x` and keeps `y`.
- Any other value replaces the default as a whole, including lists (`ntiles: [1, 1]`) and `null`.
- `mintpy.*` options are single keys, exactly as in `pipeline.yaml`.
- Only `dolphin`, `mintpy`, `inversion`, `water_mask` and `subpixel` may change. `paths`, `aoi` and `isce` would
  need a new ISCE2 stack, so the sweep refuses them.
- `inversion: dolphin` (top level) lets dolphin invert the time series; MintPy then only applies the corrections
  that are switched on in the `mintpy` section (e.g. `mintpy.deramp`, `mintpy.topographicResidual`). Unwrapping
  errors are not corrected in that mode. `inversion: mintpy` (default) uses MintPy's network inversion.
- `subpixel: {enabled: true}` adds the sub-pixel PS positions and their shift in meters to the Parquet file
  (columns `lon_subpixel`, `lat_subpixel`, `shift_east`, `shift_north`, ...; see the postprocessing README).
- Experiment names become folder names: letters, digits, `_`, `-` and `.` only, each name once.

## Reusing dolphin output

dolphin takes most of the time of an experiment, while MintPy settings are cheap to try. So the sweep computes
dolphin only once per distinct **dolphin setting**: every section of the experiment's config except `mintpy` and
`subpixel`. In practice that means `dolphin`, `water_mask` and `inversion` (`inversion: dolphin` makes dolphin
also compute its own time series, and the water mask is used in dolphin's unwrapping).

- After the dolphin stage succeeds, the sweep writes `dolphin/sweep_dolphin_settings.json` with exactly those
  settings and the experiment that computed them. Only a `dolphin/` with this file is ever reused.
- An experiment whose dolphin settings equal that file reuses `crop/` and `dolphin/` and only runs MintPy (and
  subpixel). Any other experiment deletes both and recomputes them.
- **Run order:** experiments with the same dolphin settings run one after another, otherwise in sweep-file
  order. Example: `a` (window 20x5), `b` (window 11x5), `c` (window 20x5, linear ramp) runs `a`, `c`, `b`, so dolphin
  runs twice, not three times. `--dry-run` lists the run order and where dolphin is computed or reused.
- `DONE.json` records `"dolphin": "computed"` or `"reused"` and `dolphin_computed_by_experiment`.
- After the last experiment `crop/` and `dolphin/` are deleted to free the disk. With
  `keep_dolphin_after_sweep: true` they stay, so experiments you add later with the same dolphin settings start
  with MintPy right away; they are replaced automatically when the next experiment needs other settings.

## Results

```
<results_dir>/
├── sweep.log                                   log of all sweep runs
└── <experiment name>/
    ├── pipeline.yaml                           full config used (default + changes)
    ├── changes.yaml                            only the changes
    ├── S1_ASC_073_..._vienna.parquet           the points
    ├── S1_ASC_073_..._vienna.metadata.json     metadata, incl. the full pipeline, dolphin and MintPy configs
    ├── logs/                                   this experiment's part of pipeline.log, dolphin.log, mintpy.log
    ├── mintpy_pic/                             from keep_paths
    └── DONE.json                               written last: experiment complete; dolphin computed or reused
```

The metadata (in the Parquet file and the `.metadata.json`) also records the experiment name and the sweep file
(`run.INSAR_RUN_EXPERIMENT`, `run.INSAR_RUN_SWEEP_FILE`).

## How it works

Before the first experiment, `run_sweep.py` checks everything that could otherwise fail hours later:

- the sweep file (names, allowed sections, unknown options),
- that both repositories and Python executables exist,
- that every ISCE2 run file is marked done in `isce/run_status/`,
- **every experiment's config with the pipeline's own config loader**, so a typo in a dolphin or MintPy option
  stops the sweep at once,
- that no `mintpy/` or `subpixel/` and no `dolphin/` without the sweep's settings file exists in the work
  directory. dolphin skips work whose output files already exist, so an incomplete or foreign `dolphin/` would
  silently mix in other settings; `mintpy/` and `subpixel/` could be a manual run you want to keep.

Then, per experiment, it checks the free disk space and runs the workflow above. `crop/` belongs to the dolphin
stage: its window depends on dolphin's `strides` and its water fraction on the `water_mask` options, so it is
reused and rebuilt together with `dolphin/`. The pipeline is called twice per experiment (`--steps crop dolphin`,
then `--steps mintpy subpixel`), so the settings file is written only when dolphin really finished. The pipeline
appends to its log files, so the sweep notes their sizes before an experiment and saves only the part written
during it (empty `dolphin.log` part when dolphin was reused).

`DONE.json` is written only after the export, the logs and the `keep_paths` are saved, and outputs are deleted
only after that. If any step fails, the sweep stops: nothing is deleted, and later experiments do not start. A
results folder without `DONE.json` is replaced on the next run.

YAML is read like the pipeline reads it: values such as `20200101_20200113` or `1:5` stay text instead of
becoming numbers.

## How to run the tests

```bash
python -m pytest tests
```

The tests replace the pipeline and the postprocessing with tiny fake scripts, so they need no SAR data and run in
seconds.

## Project structure

```
run_sweep.py               command line entry point
sweep/
  sweep_config.py          read the sweep file, check it, merge changes into the default config
  runner.py                checks before the start, the per-experiment workflow, saving results
  work_dir.py              ISCE2-finished check, dolphin settings file, stale outputs, clean-up, disk space
config/example_sweep.yaml  documented example of every option
tests/                     tests with fake pipeline and postprocessing tools
```
