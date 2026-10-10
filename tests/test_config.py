"""配置加载与启动期自检。"""

from __future__ import annotations

import pytest

from app.config import ConfigError, Settings


def test_defaults_are_development_safe():
    settings = Settings.from_env({})

    assert settings.app_env == "development"
    assert settings.auth_mode == "tokenreview"
    assert settings.auth_allow_insecure is False
    assert settings.log_json is False


def test_log_json_defaults_to_true_only_in_production():
    assert Settings.from_env({"APP_ENV": "production"}).log_json is True
    assert Settings.from_env({"APP_ENV": "development"}).log_json is False


def test_log_json_can_be_overridden_explicitly():
    assert Settings.from_env({"APP_ENV": "production", "LOG_JSON": "false"}).log_json is False
    assert Settings.from_env({"LOG_JSON": "true"}).log_json is True


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"APP_ENV": "staging"}, "APP_ENV"),
        ({"AUTH_MODE": "none"}, "AUTH_MODE"),
        ({"LOG_LEVEL": "VERBOSE"}, "LOG_LEVEL"),
        ({"LOG_JSON": "maybe"}, "LOG_JSON"),
        ({"APP_NAME": "   "}, "APP_NAME"),
        ({"K8S_REQUEST_TIMEOUT_SECONDS": "abc"}, "K8S_REQUEST_TIMEOUT_SECONDS"),
        ({"HELM_TIMEOUT_SECONDS": "0"}, "HELM_TIMEOUT_SECONDS"),
    ],
)
def test_invalid_values_are_rejected_at_startup(env, message):
    with pytest.raises(ConfigError, match=message):
        Settings.from_env(env)


def test_disabled_auth_requires_development_env():
    with pytest.raises(ConfigError, match="APP_ENV=development"):
        Settings.from_env(
            {"AUTH_MODE": "disabled", "APP_ENV": "production", "AUTH_ALLOW_INSECURE": "true"}
        )


def test_disabled_auth_requires_explicit_acknowledgement():
    with pytest.raises(ConfigError, match="AUTH_ALLOW_INSECURE"):
        Settings.from_env({"AUTH_MODE": "disabled", "APP_ENV": "development"})


def test_disabled_auth_is_refused_inside_a_pod():
    """防止免认证配置被误带进集群。"""
    with pytest.raises(ConfigError, match="KUBERNETES_SERVICE_HOST"):
        Settings.from_env(
            {
                "AUTH_MODE": "disabled",
                "APP_ENV": "development",
                "AUTH_ALLOW_INSECURE": "true",
                "KUBERNETES_SERVICE_HOST": "10.96.0.1",
            }
        )


def test_disabled_auth_allowed_only_with_both_locks_in_local_dev():
    settings = Settings.from_env(
        {"AUTH_MODE": "disabled", "APP_ENV": "development", "AUTH_ALLOW_INSECURE": "true"}
    )

    assert settings.auth_disabled is True
    assert settings.is_production is False
