from pathlib import Path

import pytest

from flowlens.config import Config, ConfigError, Exclude, load_config, parse_config

REPO_CONFIG = Path(__file__).parent.parent / "flowlens.yaml"


def write(tmp_path, text: str) -> Path:
    path = tmp_path / "flowlens.yaml"
    path.write_text(text)
    return path


def test_defaults_match_the_brief():
    config = Config()

    assert config.started_signal == ("first_assigned", "first_linked_pr", "label:in progress")
    assert config.deployment_source == "releases"
    assert config.exclude.state_reasons == ("NOT_PLANNED", "DUPLICATE")
    assert "duplicate" in config.exclude.labels
    assert config.exclude.bots is True


def test_the_shipped_flowlens_yaml_is_the_defaults_plus_the_demo_repo():
    assert load_config(REPO_CONFIG) == Config()

    streamlit = load_config(REPO_CONFIG, "streamlit/streamlit")

    assert streamlit.labels["bug"] == ("type:bug",)
    assert streamlit.labels["incident"] == ("type:regression",)
    assert streamlit.exclude.state_reasons == ("NOT_PLANNED", "DUPLICATE")  # merged, not lost
    assert "status:cannot-reproduce" in streamlit.exclude.labels
    assert streamlit.ingest_workflows == ("Build Release",)


def test_a_missing_default_file_means_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert load_config() == Config()


def test_a_missing_explicit_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_repo_overrides_match_case_insensitively(tmp_path):
    path = write(
        tmp_path,
        "deployment_source: releases\n"
        "repos:\n"
        "  Acme/Shop:\n"
        "    deployment_source: workflow:deploy.yml\n"
        "    exclude: {bots: false}\n",
    )

    shop = load_config(path, "acme/shop")

    assert shop.deployment_source == "workflow:deploy.yml"
    assert shop.deployment_workflow == "deploy.yml"
    assert shop.ingest_workflows == ("deploy.yml",)
    assert shop.exclude == Exclude(bots=False)
    assert load_config(path, "acme/other").deployment_source == "releases"


def test_label_lists_accept_a_single_string(tmp_path):
    config = load_config(write(tmp_path, "labels:\n  hotfix: urgent\n"))

    assert config.labels["hotfix"] == ("urgent",)
    assert config.labels["bug"] == Config().labels["bug"]  # untouched kinds keep defaults


def test_state_reasons_are_normalised_to_upper_case():
    config = parse_config({"exclude": {"state_reason": ["not_planned"]}})

    assert config.exclude.state_reasons == ("NOT_PLANNED",)


def test_deployment_workflow_is_ingested_once():
    config = parse_config(
        {"deployment_source": "workflow:Build Release", "workflows": ["CI", "Build Release"]}
    )

    assert config.ingest_workflows == ("CI", "Build Release")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"started_signals": []}, "unknown setting started_signals"),
        ({"started_signal": ["first_comment"]}, "unknown signal 'first_comment'"),
        ({"started_signal": ["label:"]}, "unknown signal 'label:'"),
        ({"pr_started_signal": ["first_review"]}, "unknown signal 'first_review'"),
        ({"labels": {"bugs": ["bug"]}}, "unknown setting labels.bugs"),
        ({"labels": {"bug": [{"type": "bug"}]}}, 'Quote names that contain ": "'),
        ({"deployment_source": "tags"}, "must be releases, deployments, or workflow:<name>"),
        ({"deployment_source": "workflow:"}, "must be releases, deployments, or workflow:<name>"),
        ({"exclude": {"bot": True}}, "unknown setting exclude.bot"),
        ({"exclude": {"bots": "yes"}}, "exclude.bots must be true or false"),
        ({"include_prereleases": 1}, "include_prereleases must be true or false"),
    ],
)
def test_bad_settings_are_rejected_with_a_pointer(data, message):
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_config(data)


def test_an_unquoted_label_with_a_colon_explains_itself(tmp_path):
    # YAML reads `- type: bug` as a mapping, a common slip with GitHub's label names.
    path = write(tmp_path, "labels:\n  bug:\n    - type: bug\n")

    with pytest.raises(ConfigError, match=r"flowlens\.yaml: labels\.bug must be a list"):
        load_config(path)


def test_invalid_yaml_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(write(tmp_path, "labels: [unclosed\n"))


def test_an_empty_file_means_defaults(tmp_path):
    assert load_config(write(tmp_path, "")) == Config()
