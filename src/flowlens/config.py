"""flowlens.yaml: how FlowLens reads a repository's history.

Every key is optional. Top-level keys apply to every repo; a ``repos:`` section can
override them for one repository (``labels`` and ``exclude`` merge key by key, everything
else is replaced). Unknown keys are errors, so a typo can't silently fall back to a
default.
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


class ConfigError(ValueError):
    """flowlens.yaml is missing, malformed, or has a value FlowLens can't use."""


@dataclass(frozen=True)
class Exclude:
    """Items kept in the model but left out of metrics."""

    state_reasons: tuple[str, ...] = ("NOT_PLANNED", "DUPLICATE")
    labels: tuple[str, ...] = ("duplicate", "wontfix", "invalid")
    bots: bool = True  # items authored by GitHub Apps such as Dependabot


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
        "deployment_environments", "include_prereleases", "workflows", "exclude",
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
    )


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in {"labels", "exclude"} and isinstance(value, dict):
            merged[key] = {**(base.get(key) or {}), **value}
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
