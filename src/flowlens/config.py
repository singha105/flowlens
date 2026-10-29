"""flowlens.yaml: how FlowLens reads a repository's history.

Every key is optional. Top-level keys apply to every repo; a ``repos:`` section can
override them for one repository (``labels``, ``exclude``, ``dora`` and its ``bands``
merge key by key, everything else is replaced). Unknown keys are errors, so a typo can't
silently fall back to a default.

DORA performance bands are the one thing with no built-in default: they come from a
published report, so they live in flowlens.yaml next to the source they cite.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("flowlens.yaml")

ISSUE_SIGNALS = ("first_assigned", "first_linked_pr")  # plus "label:<name>"
PR_SIGNALS = ("first_commit", "opened")
LABEL_KINDS = ("bug", "incident", "hotfix")
DEPLOYMENT_SOURCES = ("releases", "deployments")  # plus "workflow:<name>"
# DORA metrics that take bands, and whether a higher value is better.
DORA_METRICS = {
    "deployments_per_week": True,
    "lead_time_hours": False,  # p50 lead time for changes
    "change_failure_rate": False,  # share of deploys, 0 to 1
    "recovery_hours": False,  # median failed deployment recovery time
}
BAND_NAMES = ("elite", "high", "medium")  # anything past medium is low


class ConfigError(ValueError):
    """flowlens.yaml is missing, malformed, or has a value FlowLens can't use."""


@dataclass(frozen=True)
class Exclude:
    """Items kept in the model but left out of metrics."""

    state_reasons: tuple[str, ...] = ("NOT_PLANNED", "DUPLICATE")
    labels: tuple[str, ...] = ("duplicate", "wontfix", "invalid")
    bots: bool = True  # items authored by GitHub Apps such as Dependabot


@dataclass(frozen=True)
class Band:
    """Limits for one DORA metric. A value gets the best band whose limit it meets."""

    elite: float
    high: float
    medium: float
    source: str  # the report the limits come from
    higher_is_better: bool = False

    def of(self, value: float | None) -> str | None:
        if value is None:
            return None
        for name in BAND_NAMES:
            limit = getattr(self, name)
            if (value >= limit) if self.higher_is_better else (value <= limit):
                return name
        return "low"


@dataclass(frozen=True)
class Dora:
    # A deploy failed if a failure signal fires within this many hours after it.
    failure_window_hours: float = 72.0
    bands: dict[str, Band] = field(default_factory=dict)  # by DORA_METRICS key


@dataclass(frozen=True)
class Config:
    # An issue starts at the earliest of these, never before it was opened.
    started_signal: tuple[str, ...] = ("first_assigned", "first_linked_pr", "label:in progress")
    # A PR starts at the earliest of these.
    pr_started_signal: tuple[str, ...] = ("first_commit", "opened")
    labels: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "bug": ("bug", "type: bug"),
            "incident": ("incident", "sev1", "sev2"),
            "hotfix": ("hotfix",),
        }
    )
    deployment_source: str = "releases"
    deployment_environments: tuple[str, ...] = ("production", "prod")
    include_prereleases: bool = False
    workflows: tuple[str, ...] = ()
    exclude: Exclude = field(default_factory=Exclude)
    dora: Dora = field(default_factory=Dora)

    @property
    def deployment_workflow(self) -> str | None:
        if self.deployment_source.startswith("workflow:"):
            return self.deployment_source.removeprefix("workflow:")
        return None

    @property
    def ingest_workflows(self) -> tuple[str, ...]:
        """Workflows whose runs get ingested: ``workflows`` plus the deployment workflow."""
        names = list(self.workflows)
        if self.deployment_workflow and self.deployment_workflow not in names:
            names.append(self.deployment_workflow)
        return tuple(names)


def load_config(path: Path | None = None, repo: str | None = None) -> Config:
    """Read flowlens.yaml, applying the ``repos:`` section for ``repo`` if there is one.

    Without an explicit path, a missing ./flowlens.yaml just means the defaults.
    """
    explicit = path is not None
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        if explicit:
            raise ConfigError(f"{path} not found")
        return Config()
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a mapping of settings")
    repos = data.pop("repos", None) or {}
    if not isinstance(repos, dict):
        raise ConfigError("repos must map owner/name to settings")
    override: dict[str, Any] = {}
    if repo is not None:
        override = next((v for k, v in repos.items() if str(k).lower() == repo.lower()), {}) or {}
    try:
        return parse_config(_merge(data, override))
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def parse_config(data: dict[str, Any]) -> Config:
    """Build a Config from already-parsed YAML, validating every value."""
    known = {
        "started_signal", "pr_started_signal", "labels", "deployment_source",
        "deployment_environments", "include_prereleases", "workflows", "exclude", "dora",
    }  # fmt: skip
    _reject_unknown(data, known, "")
    defaults = Config()

    started = _strings(data, "started_signal", defaults.started_signal)
    for signal in started:
        if signal not in ISSUE_SIGNALS and not (signal.startswith("label:") and signal[6:].strip()):
            raise ConfigError(
                f"started_signal: unknown signal {signal!r}; "
                f"use {', '.join(ISSUE_SIGNALS)}, or label:<name>"
            )
    pr_started = _strings(data, "pr_started_signal", defaults.pr_started_signal)
    for signal in pr_started:
        if signal not in PR_SIGNALS:
            raise ConfigError(
                f"pr_started_signal: unknown signal {signal!r}; use {', '.join(PR_SIGNALS)}"
            )

    labels = dict(defaults.labels)
    raw_labels = data.get("labels") or {}
    if not isinstance(raw_labels, dict):
        raise ConfigError("labels must map bug/incident/hotfix to lists of label names")
    _reject_unknown(raw_labels, set(LABEL_KINDS), "labels.")
    for kind in LABEL_KINDS:
        labels[kind] = _strings(raw_labels, kind, labels[kind], prefix="labels.")

    source = data.get("deployment_source", defaults.deployment_source)
    if not isinstance(source, str) or not (
        source in DEPLOYMENT_SOURCES
        or (source.startswith("workflow:") and source.removeprefix("workflow:").strip())
    ):
        raise ConfigError(
            f"deployment_source: {source!r} must be releases, deployments, or workflow:<name>"
        )

    raw_exclude = data.get("exclude") or {}
    if not isinstance(raw_exclude, dict):
        raise ConfigError("exclude must be a mapping")
    _reject_unknown(raw_exclude, {"state_reason", "labels", "bots"}, "exclude.")
    exclude = Exclude(
        state_reasons=tuple(
            reason.upper()
            for reason in _strings(
                raw_exclude, "state_reason", defaults.exclude.state_reasons, prefix="exclude."
            )
        ),
        labels=_strings(raw_exclude, "labels", defaults.exclude.labels, prefix="exclude."),
        bots=_bool(raw_exclude, "bots", defaults.exclude.bots, prefix="exclude."),
    )

    return Config(
        started_signal=started,
        pr_started_signal=pr_started,
        labels=labels,
        deployment_source=source.strip(),
        deployment_environments=_strings(
            data, "deployment_environments", defaults.deployment_environments
        ),
        include_prereleases=_bool(data, "include_prereleases", defaults.include_prereleases),
        workflows=_strings(data, "workflows", defaults.workflows),
        exclude=exclude,
        dora=_dora(data.get("dora") or {}),
    )


def _dora(data: Any) -> Dora:
    if not isinstance(data, dict):
        raise ConfigError("dora must be a mapping")
    _reject_unknown(data, {"failure_window_hours", "bands"}, "dora.")
    window = data.get("failure_window_hours", Dora.failure_window_hours)
    if not _is_number(window) or window <= 0:
        raise ConfigError("dora.failure_window_hours must be a positive number of hours")
    raw_bands = data.get("bands") or {}
    if not isinstance(raw_bands, dict):
        raise ConfigError("dora.bands must map DORA metrics to their limits")
    _reject_unknown(raw_bands, set(DORA_METRICS), "dora.bands.")
    bands = {metric: _band(metric, spec) for metric, spec in raw_bands.items()}
    return Dora(failure_window_hours=float(window), bands=bands)


def _band(metric: str, spec: Any) -> Band:
    prefix = f"dora.bands.{metric}"
    if not isinstance(spec, dict):
        raise ConfigError(f"{prefix} must set {', '.join(BAND_NAMES)}, and source")
    _reject_unknown(spec, {*BAND_NAMES, "source"}, f"{prefix}.")
    for key in (*BAND_NAMES, "source"):
        if key not in spec:
            raise ConfigError(f"{prefix}.{key} is missing")
    limits = [spec[name] for name in BAND_NAMES]
    if not all(_is_number(limit) and limit >= 0 for limit in limits):
        raise ConfigError(f"{prefix}: {', '.join(BAND_NAMES)} must be numbers, 0 or more")
    higher_is_better = DORA_METRICS[metric]
    if limits != sorted(limits, reverse=higher_is_better):
        order = "at least" if higher_is_better else "at most"
        raise ConfigError(
            f"{prefix}: limits are {order} values, so elite, high, and medium must "
            f"{'decrease' if higher_is_better else 'increase'}"
        )
    if metric == "change_failure_rate" and max(limits) > 1:
        raise ConfigError(f"{prefix} is a share of deploys: use 0.15, not 15")
    source = spec["source"]
    if not isinstance(source, str) or not source.strip():
        raise ConfigError(f"{prefix}.source must name the report the limits come from")
    return Band(
        *(float(limit) for limit in limits),
        source=source.strip(),
        higher_is_better=higher_is_better,
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in {"labels", "exclude", "dora"} and isinstance(value, dict):
            merged[key] = {**(base.get(key) or {}), **value}
            if key == "dora" and isinstance(value.get("bands"), dict):
                base_bands = (base.get(key) or {}).get("bands") or {}
                merged[key]["bands"] = {**base_bands, **value["bands"]}
        else:
            merged[key] = value
    return merged


def _reject_unknown(data: dict[str, Any], known: set[str], prefix: str) -> None:
    unknown = sorted(set(map(str, data)) - known)
    if unknown:
        raise ConfigError(
            f"unknown setting {prefix}{unknown[0]}; expected one of {', '.join(sorted(known))}"
        )


def _strings(
    data: dict[str, Any], key: str, default: tuple[str, ...], prefix: str = ""
) -> tuple[str, ...]:
    if key not in data:
        return default
    value = data[key]
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(
            f"{prefix}{key} must be a list of strings. Quote names that contain "
            f'": ", e.g. "type: bug", or YAML reads them as mappings.'
        )
    return tuple(value)


def _bool(data: dict[str, Any], key: str, default: bool, prefix: str = "") -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{prefix}{key} must be true or false")
    return value
