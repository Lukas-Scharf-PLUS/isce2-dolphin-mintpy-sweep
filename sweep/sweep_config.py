"""Sweep file: one default pipeline config plus a list of experiments.

A sweep file names the default ``pipeline.yaml`` and lists experiments. Each
experiment gives only the options that differ from the default, in the same
nested layout as ``pipeline.yaml``. The full config of an experiment is the
default with the changes merged over it (see ``merge_changes``).

Only sections that ISCE2 does not depend on may change, because all experiments
share one ISCE2 stack (see ``CHANGEABLE_SECTIONS``).
"""

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

# Sections an experiment may change. They only affect the crop, dolphin, MintPy
# and subpixel steps, which every experiment runs again. paths, aoi and isce
# would need a new ISCE2 stack.
CHANGEABLE_SECTIONS = ("dolphin", "mintpy", "inversion", "water_mask", "subpixel")

# Experiment names become folder names, so only safe characters are allowed.
EXPERIMENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

DEFAULT_KEEP_PATHS = ("mintpy/pic",)
DEFAULT_MIN_FREE_GB = 300.0


class SweepConfigError(ValueError):
    """Raised when the sweep file is incomplete or inconsistent."""


@dataclass
class ToolConfig:
    """Where a tool's code lives and which Python (conda environment) runs it."""

    repo: Path
    python: Path
    extra_args: list = field(default_factory=list)


@dataclass
class Experiment:
    """One parameter setting: a name and its changes to the default config."""

    name: str
    changes: dict = field(default_factory=dict)
    description: str = ""


@dataclass
class SweepConfig:
    """A complete sweep: default config, tools, output location and experiments."""

    sweep_file: Path
    base_config: Path
    results_dir: Path
    pipeline: ToolConfig
    postprocessing: ToolConfig
    experiments: list
    area_name: Optional[str] = None
    keep_paths: tuple = DEFAULT_KEEP_PATHS
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    # Keep crop/ and dolphin/ after the last experiment, so experiments added
    # later with the same dolphin settings can reuse them (costs disk space).
    keep_dolphin_after_sweep: bool = False


def merge_changes(base, changes):
    """Return a copy of ``base`` with ``changes`` merged over it.

    Mappings are merged key by key at every level, so a change only replaces the
    options it names. Any other value, including lists and ``null``, replaces
    the default value as a whole.

    Parameters
    ----------
    base : dict
        The default config (not modified).
    changes : dict
        Options to change, in the same nested layout as ``base``.

    Returns
    -------
    dict
        The merged config.
    """
    merged = copy.deepcopy(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_changes(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class NumberSafeYamlLoader(yaml.SafeLoader):
    """Safe YAML loader that recognises numbers like YAML 1.2, as the pipeline does.

    PyYAML follows YAML 1.1, which reads ``20080520_20090817`` as the integer
    2008052020090817 and ``1:5`` as 65. Both are normal MintPy values. The
    pipeline's own loader (``PipelineYamlLoader``) keeps them as text; this
    loader does the same, so merging and writing a config never changes them.
    """


_INT_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"
NumberSafeYamlLoader.yaml_implicit_resolvers = {
    first_char: [(tag, regexp) for tag, regexp in resolvers if tag not in (_INT_TAG, _FLOAT_TAG)]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
NumberSafeYamlLoader.add_implicit_resolver(
    _INT_TAG, re.compile(r"^[-+]?[0-9]+$"), list("-+0123456789"))
NumberSafeYamlLoader.add_implicit_resolver(
    _FLOAT_TAG,
    re.compile(r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
               r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"),
    list("-+0123456789."),
)


def read_yaml(path):
    """Read a YAML file and return its content (an empty file gives ``{}``).

    Uses ``NumberSafeYamlLoader``, so values are read exactly as the pipeline
    reads them.
    """
    with open(path) as stream:
        return yaml.load(stream, Loader=NumberSafeYamlLoader) or {}


def write_yaml(data, path):
    """Write ``data`` as YAML, keeping the key order of the input."""
    with open(path, "w") as stream:
        yaml.safe_dump(data, stream, sort_keys=False, default_flow_style=False)


def build_experiment_config(base_config, experiment):
    """Return the full pipeline config of one experiment (default + changes)."""
    return merge_changes(base_config, experiment.changes)


def _resolve(path_value, relative_to):
    """Turn a path from the sweep file into an absolute path.

    Relative paths are resolved against the folder of the sweep file, so the
    sweep behaves the same from every working directory.
    """
    path = Path(path_value).expanduser()
    return path if path.is_absolute() else (relative_to / path).resolve()


def _require(raw, key, where="sweep file"):
    """Return ``raw[key]`` or raise a clear error if it is missing."""
    if raw.get(key) in (None, ""):
        raise SweepConfigError(f"'{key}' is required in the {where}.")
    return raw[key]


def _build_tool(raw, section, sweep_dir):
    """Create a ToolConfig from the ``pipeline`` or ``postprocessing`` section."""
    values = raw.get(section)
    if not isinstance(values, dict):
        raise SweepConfigError(f"Section '{section}' with 'repo' and 'python' is required.")
    unknown = set(values) - {"repo", "python", "extra_args"}
    if unknown:
        raise SweepConfigError(f"Unknown option(s) in '{section}': {sorted(unknown)}")
    extra_args = values.get("extra_args") or []
    if not isinstance(extra_args, list):
        raise SweepConfigError(f"'{section}.extra_args' must be a list, e.g. [\"--all-pixels\"].")
    return ToolConfig(
        repo=_resolve(_require(values, "repo", f"'{section}' section"), sweep_dir),
        python=_resolve(_require(values, "python", f"'{section}' section"), sweep_dir),
        extra_args=[str(argument) for argument in extra_args],
    )


def _check_changes(name, changes):
    """Raise if an experiment changes a section that all experiments must share."""
    if not isinstance(changes, dict):
        raise SweepConfigError(f"Experiment '{name}': 'changes' must be a mapping.")
    fixed = sorted(set(changes) - set(CHANGEABLE_SECTIONS))
    if fixed:
        raise SweepConfigError(
            f"Experiment '{name}' changes {fixed}. Only {list(CHANGEABLE_SECTIONS)} may change: "
            "all experiments share one ISCE2 stack, so paths, aoi and isce must stay as in "
            "the default config."
        )


def _build_experiments(raw_experiments):
    """Create the experiment list, checking names and changed sections."""
    if not isinstance(raw_experiments, list) or not raw_experiments:
        raise SweepConfigError("'experiments' must be a non-empty list.")
    experiments = []
    seen_names = set()
    for index, values in enumerate(raw_experiments, start=1):
        if not isinstance(values, dict):
            raise SweepConfigError(f"Experiment {index} must be a mapping with 'name' and 'changes'.")
        unknown = set(values) - {"name", "changes", "description"}
        if unknown:
            raise SweepConfigError(f"Experiment {index}: unknown option(s) {sorted(unknown)}")
        name = str(_require(values, "name", f"experiment {index}"))
        if not EXPERIMENT_NAME_PATTERN.match(name):
            raise SweepConfigError(
                f"Experiment name '{name}' may only contain letters, digits, '_', '-' and '.', "
                "because it becomes a folder name."
            )
        if name in seen_names:
            raise SweepConfigError(f"Experiment name '{name}' is used twice.")
        seen_names.add(name)
        changes = values.get("changes") or {}
        _check_changes(name, changes)
        experiments.append(Experiment(name=name, changes=changes,
                                      description=str(values.get("description") or "")))
    return experiments


def load_sweep(sweep_file):
    """Load and check a sweep file.

    Parameters
    ----------
    sweep_file : str | Path
        Path to the sweep YAML file.

    Returns
    -------
    SweepConfig
        The sweep with absolute paths.

    Raises
    ------
    SweepConfigError
        If options are missing or unknown, names are invalid or an experiment
        changes a shared section.
    """
    sweep_file = Path(sweep_file).expanduser().resolve()
    raw = read_yaml(sweep_file)
    known = {"base_config", "results_dir", "area_name", "pipeline", "postprocessing",
             "keep_paths", "min_free_gb", "keep_dolphin_after_sweep", "experiments"}
    unknown = set(raw) - known
    if unknown:
        raise SweepConfigError(f"Unknown option(s) in the sweep file: {sorted(unknown)}")

    sweep_dir = sweep_file.parent
    keep_dolphin = raw.get("keep_dolphin_after_sweep", False)
    if not isinstance(keep_dolphin, bool):
        raise SweepConfigError(f"keep_dolphin_after_sweep must be true or false, got {keep_dolphin!r}.")
    keep_paths = raw.get("keep_paths", list(DEFAULT_KEEP_PATHS)) or []
    for keep_path in keep_paths:
        if Path(keep_path).is_absolute() or ".." in Path(keep_path).parts:
            raise SweepConfigError(
                f"keep_paths entry '{keep_path}' must be relative to the work_dir, e.g. mintpy/pic."
            )
    return SweepConfig(
        sweep_file=sweep_file,
        base_config=_resolve(_require(raw, "base_config"), sweep_dir),
        results_dir=_resolve(_require(raw, "results_dir"), sweep_dir),
        pipeline=_build_tool(raw, "pipeline", sweep_dir),
        postprocessing=_build_tool(raw, "postprocessing", sweep_dir),
        experiments=_build_experiments(raw.get("experiments")),
        area_name=raw.get("area_name") or None,
        keep_paths=tuple(str(keep_path) for keep_path in keep_paths),
        min_free_gb=float(raw.get("min_free_gb", DEFAULT_MIN_FREE_GB)),
        keep_dolphin_after_sweep=keep_dolphin,
    )
