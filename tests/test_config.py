"""Configuration: precedence, the stored file, and never leaking the key."""

from __future__ import annotations

import os
import stat

import pytest

from iota_hub import DEFAULT_BASE_URL, AuthError, IotaHubError, key_prefix, resolve
from iota_hub.config import (
    config_file_path,
    delete_profile,
    load_config,
    save_profile,
)

FLAG_KEY = "iotahub_flagid_flagsecret"
ENV_KEY = "iotahub_envid_envsecret"
PROFILE_KEY = "iotahub_profid_profsecret"
DEV_URL = "https://abc123.execute-api.us-west-2.amazonaws.com/dev/api"

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")


@pytest.fixture
def config_path(tmp_path):
    """A config file of our own: no test ever reads the real one."""
    path = tmp_path / "iota-hub" / "config.toml"
    save_profile("dev", api_key=PROFILE_KEY, base_url=DEV_URL, config_path=path)
    return path


# -- precedence -------------------------------------------------------------


def test_the_flag_wins_over_the_environment_and_the_profile(config_path):
    settings = resolve(
        api_key=FLAG_KEY,
        env={"IOTA_HUB_API_KEY": ENV_KEY},
        config_path=config_path,
    )

    assert settings.api_key == FLAG_KEY
    assert settings.source_of_key == "flag"
    # The profile is still the default target: only the key was overridden.
    assert settings.base_url == DEV_URL
    assert settings.profile_name == "dev"


def test_the_environment_wins_over_the_profile(config_path):
    settings = resolve(env={"IOTA_HUB_API_KEY": ENV_KEY}, config_path=config_path)

    assert settings.api_key == ENV_KEY
    assert settings.source_of_key == "env"


def test_the_profile_supplies_the_key_and_the_base_url_together(config_path):
    settings = resolve(env={}, config_path=config_path)

    assert settings.api_key == PROFILE_KEY
    assert settings.base_url == DEV_URL
    assert settings.source_of_key == "profile"
    assert settings.is_default_target is False


def test_an_explicit_base_url_overrides_the_profiles(config_path):
    settings = resolve(
        base_url="http://localhost:8000/", env={}, config_path=config_path
    )

    assert settings.base_url == "http://localhost:8000"
    assert settings.api_key == PROFILE_KEY


def test_the_environment_names_the_profile(tmp_path):
    path = tmp_path / "config.toml"
    save_profile(
        "default", api_key="iotahub_a_b", base_url=DEFAULT_BASE_URL, config_path=path
    )
    save_profile("dev", api_key=PROFILE_KEY, base_url=DEV_URL, config_path=path)

    settings = resolve(env={"IOTA_HUB_PROFILE": "dev"}, config_path=path)

    assert settings.profile_name == "dev"
    assert settings.api_key == PROFILE_KEY


def test_without_a_config_the_target_is_production(tmp_path):
    settings = resolve(
        env={"IOTA_HUB_API_KEY": ENV_KEY}, config_path=tmp_path / "absent.toml"
    )

    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.is_default_target is True
    assert settings.profile_name is None


def test_a_profile_that_was_asked_for_and_is_not_there(config_path):
    with pytest.raises(IotaHubError) as excinfo:
        resolve(profile="staging", env={}, config_path=config_path)

    assert excinfo.value.code == "unknown_profile"
    assert excinfo.value.details["known"] == ["dev"]


def test_no_key_anywhere_is_an_auth_error(tmp_path):
    with pytest.raises(AuthError) as excinfo:
        resolve(env={}, config_path=tmp_path / "absent.toml")

    error = excinfo.value
    assert error.code == "missing_api_key"
    assert "IOTA_HUB_API_KEY" in error.hint


# -- the file ---------------------------------------------------------------


def test_a_profile_round_trips(tmp_path):
    path = tmp_path / "nested" / "config.toml"

    save_profile("dev", api_key=PROFILE_KEY, base_url=DEV_URL + "/", config_path=path)

    assert load_config(path) == {
        "default_profile": "dev",
        "profiles": {"dev": {"base_url": DEV_URL, "api_key": PROFILE_KEY}},
    }
    assert resolve(env={}, config_path=path).api_key == PROFILE_KEY


def test_a_second_profile_does_not_steal_the_default(config_path):
    save_profile(
        "prod",
        api_key="iotahub_p_s",
        base_url=DEFAULT_BASE_URL,
        config_path=config_path,
    )

    config = load_config(config_path)
    assert config["default_profile"] == "dev"
    assert sorted(config["profiles"]) == ["dev", "prod"]


def test_deleting_a_profile_moves_the_default_off_it(config_path):
    save_profile(
        "prod",
        api_key="iotahub_p_s",
        base_url=DEFAULT_BASE_URL,
        config_path=config_path,
    )

    assert delete_profile("dev", config_path=config_path) is True
    assert delete_profile("dev", config_path=config_path) is False

    config = load_config(config_path)
    assert config["default_profile"] == "prod"
    assert "dev" not in config["profiles"]


def test_deleting_the_last_profile_leaves_a_valid_file(config_path):
    delete_profile("dev", config_path=config_path)

    # Nothing left to write, so the file is empty - and empty TOML is valid.
    assert load_config(config_path) == {}
    assert config_path.read_text(encoding="utf-8").strip() == ""


def test_a_broken_config_is_reported_not_raised_raw(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("default_profile = \n", encoding="utf-8")

    with pytest.raises(IotaHubError) as excinfo:
        load_config(path)

    assert excinfo.value.code == "invalid_config"


def test_the_default_path_is_under_the_platform_config_dir():
    path = config_file_path()

    assert path.name == "config.toml"
    assert path.parent.name == "iota-hub"


@posix_only
def test_the_file_is_not_readable_by_anyone_else(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    path.chmod(0o644)  # a file that already exists is tightened too

    save_profile("dev", api_key=PROFILE_KEY, base_url=DEV_URL, config_path=path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# -- the key is never printed -----------------------------------------------


def test_key_prefix_keeps_the_secret():
    assert key_prefix("iotahub_abc123_supersecrettail") == "iotahub_abc123_..."
    assert "supersecret" not in key_prefix("iotahub_abc123_supersecrettail")
    assert key_prefix("garbage") == "<unrecognized key format>"


def test_no_error_ever_carries_the_key(config_path):
    with pytest.raises(IotaHubError) as excinfo:
        resolve(api_key=FLAG_KEY, profile="nope", env={}, config_path=config_path)

    error = excinfo.value
    rendered = f"{error} {error.details} {error.hint}"
    assert FLAG_KEY not in rendered
    assert PROFILE_KEY not in rendered
