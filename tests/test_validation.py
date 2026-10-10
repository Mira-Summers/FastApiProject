"""资源名校验。

这一层同时承担 helm 的参数注入防护，所以「以 - 开头一律拒绝」是硬要求。
"""

from __future__ import annotations

import pytest

from app.errors import BadRequest
from app.validation import validate_namespace, validate_pod_name, validate_release_name


@pytest.mark.parametrize("value", ["default", "kube-system", "ns1", "a", "a-b-c", "0"])
def test_valid_namespaces(value):
    assert validate_namespace(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "Default", "with_underscore", "-leading", "trailing-", "has space", "dot.name", "a" * 64],
)
def test_invalid_namespaces(value):
    with pytest.raises(BadRequest):
        validate_namespace(value)


def test_release_name_is_limited_to_53_chars():
    assert validate_release_name("a" * 53) == "a" * 53
    with pytest.raises(BadRequest):
        validate_release_name("a" * 54)


def test_pod_name_accepts_subdomain_form():
    assert validate_pod_name("web-7d9f8b6c4d-x2k9p") == "web-7d9f8b6c4d-x2k9p"


@pytest.mark.parametrize(
    "validator", [validate_namespace, validate_release_name, validate_pod_name]
)
def test_flag_like_values_are_rejected_by_every_validator(validator):
    """helm 参数注入的根防线：任何以 `-` 开头的值都不合法。"""
    with pytest.raises(BadRequest):
        validator("--kubeconfig=evil")
    with pytest.raises(BadRequest):
        validator("-n")
