import pytest

from app.az_runner import AzValidationError, build_argv


def test_build_argv_basic() -> None:
    argv = build_argv(["apim", "list"], {"--resource-group": "rg1"}, az_path="az")
    assert argv == ["az", "apim", "list", "--output", "json", "--resource-group", "rg1"]


def test_build_argv_flag_true() -> None:
    argv = build_argv(["account", "show"], {"--only-show-errors": True}, az_path="az")
    assert "--only-show-errors" in argv
    # value not appended
    assert argv.count("--only-show-errors") == 1


def test_build_argv_drops_false_and_none() -> None:
    argv = build_argv(
        ["x"], {"--keep": "a", "--drop1": False, "--drop2": None}, az_path="az"
    )
    assert "--drop1" not in argv
    assert "--drop2" not in argv
    assert "--keep" in argv


def test_build_argv_list_repeats() -> None:
    argv = build_argv(["x"], {"--tag": ["a", "b"]}, az_path="az")
    assert argv.count("--tag") == 2


def test_build_argv_rejects_shell_meta_in_value() -> None:
    with pytest.raises(AzValidationError):
        build_argv(["x"], {"--name": "foo;rm -rf /"}, az_path="az")


def test_build_argv_rejects_reserved_param() -> None:
    with pytest.raises(AzValidationError):
        build_argv(["x"], {"--output": "yaml"}, az_path="az")


def test_build_argv_rejects_bad_param_name() -> None:
    with pytest.raises(AzValidationError):
        build_argv(["x"], {"weird name": "v"}, az_path="az")


def test_build_argv_subscription_appended() -> None:
    argv = build_argv(["x"], {}, az_path="az", subscription="00000000-0000-0000-0000-000000000000")
    assert "--subscription" in argv


def test_build_argv_permissive_param_allows_kql() -> None:
    kql = "ApiManagementGatewayLogs | where TimeGenerated > ago(1h) | take 10"
    argv = build_argv(
        ["monitor", "log-analytics", "query"],
        {"--workspace": "abc-123", "--analytics-query": kql},
        az_path="az",
        permissive_params={"--analytics-query"},
    )
    assert kql in argv


def test_build_argv_permissive_still_blocks_nul_and_newline() -> None:
    with pytest.raises(AzValidationError):
        build_argv(
            ["x"], {"--analytics-query": "foo\nbar"}, az_path="az",
            permissive_params={"--analytics-query"},
        )


def test_build_argv_non_permissive_param_still_strict() -> None:
    # Even with one param marked permissive, others are still strict.
    with pytest.raises(AzValidationError):
        build_argv(
            ["x"],
            {"--analytics-query": "a|b", "--name": "evil;rm"},
            az_path="az",
            permissive_params={"--analytics-query"},
        )
