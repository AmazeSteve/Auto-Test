"""Offline Config startup validation and safe cross-platform failure evidence."""

from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import traceback

import pytest
from requests.adapters import HTTPAdapter
import yaml
import wizbank_api_test

from wizbank_api_test.core import config as config_module
from wizbank_api_test.core.config import Config


SECRET = "SYNTHETIC_B4_CONFIG_PRIVATE_VALUE"
VALID = {
    "base_urls": {"Host-be": "https://example.invalid/api/"},
    "request": {"timeout": 30, "verify_ssl": False},
    "default_headers": {"Accept": "application/json"},
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in B4 Config tests")

    monkeypatch.setattr(HTTPAdapter, "send", deny)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "配置 project with spaces"
    (root / "configs").mkdir(parents=True)
    monkeypatch.setattr(config_module, "__file__", str(root / "core" / "config.py"))
    return root


def write_config(project, data=VALID, env="local"):
    path = project / "configs" / f"env.{env}.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def assert_safe(error):
    evidence = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert SECRET not in evidence


def test_current_shape_preserves_runtime_getters_and_input_values(project):
    original = deepcopy(VALID)
    write_config(project, original)
    reader = Config()
    assert reader.env == "local"
    assert reader.root_dir == project
    assert reader.data == original
    assert reader.get_base_url("Host-be") == "https://example.invalid/api"
    assert reader.get_timeout() == 30
    assert reader.get_verify_ssl() is False
    assert reader.get_default_headers() == {"Accept": "application/json"}


def test_missing_optional_sections_keep_existing_defaults(project):
    write_config(project, {})
    reader = Config()
    assert reader.get_timeout() == 30
    assert reader.get_verify_ssl() is False
    assert reader.get_default_headers() == {}
    with pytest.raises(ValueError):
        reader.get_base_url("Host-be")


@pytest.mark.parametrize("env", ["", " ", "..", "../local", "local/extra", "local\\extra",
                                 "/absolute", "C:\\private\\local", "./local", ".",
                                 None, 1, True, [], {}])
def test_invalid_environment_is_rejected_before_file_access(monkeypatch, env):
    def no_load(*args, **kwargs):
        raise AssertionError("Invalid environment reached the filesystem")

    monkeypatch.setattr(Config, "_load_yaml", no_load)
    with pytest.raises(ValueError, match="[Ee]nvironment"):
        Config(env)


@pytest.mark.parametrize("env", ["local", "uat", "qa_1", "pre-prod-2", "qa.v1", "本地"])
def test_simple_environment_identifiers_work_without_hardcoded_paths(project, env):
    write_config(project, VALID, env)
    assert Config(env).data == VALID


@pytest.mark.parametrize("raw", ["", "null", "[]", "- item", "private-string", "42", "false"])
def test_top_level_must_be_mapping(project, raw):
    (project / "configs" / "env.local.yaml").write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="[Mm]apping"):
        Config()


def test_missing_config_has_safe_path_free_error(project):
    with pytest.raises(FileNotFoundError) as raised:
        Config()
    assert str(project) not in str(raised.value)
    assert "配置" not in str(raised.value)


@pytest.mark.parametrize("section,value", [
    ("base_urls", None), ("base_urls", []), ("base_urls", SECRET),
    ("request", None), ("request", []), ("request", SECRET),
    ("default_headers", None), ("default_headers", []), ("default_headers", SECRET),
])
def test_section_types_fail_safely(project, section, value):
    data = deepcopy(VALID)
    data[section] = value
    write_config(project, data)
    with pytest.raises(ValueError, match=section) as raised:
        Config()
    assert_safe(raised.value)


@pytest.mark.parametrize("key", ["", " ", None, 5, False])
def test_base_url_host_key_is_nonempty_string(project, key):
    write_config(project, {"base_urls": {key: "https://example.invalid"}})
    with pytest.raises(ValueError, match="base_urls"):
        Config()


@pytest.mark.parametrize("url", [None, 5, False, "", " ", "example.invalid", "/relative",
                                 "ftp://example.invalid", "https://", "http:///path",
                                 "https://:443/path", "https://[invalid", SECRET])
def test_base_url_requires_http_scheme_and_host(project, url):
    write_config(project, {"base_urls": {"Host-be": url}})
    with pytest.raises(ValueError, match="base_urls") as raised:
        Config()
    assert_safe(raised.value)


@pytest.mark.parametrize("url", ["http://localhost:8080/api/", "https://example.invalid/",
                                 "http://127.0.0.1:8080", "https://[::1]:8443/api"])
def test_http_and_https_urls_remain_supported(project, url):
    write_config(project, {"base_urls": {"Host-be": url}})
    assert Config().get_base_url("Host-be") == url.rstrip("/")


@pytest.mark.parametrize("timeout", [True, False, "30", None, 0, -1, -0.1, float("nan"), [], {}])
def test_timeout_requires_positive_number_and_rejects_bool(project, timeout):
    write_config(project, {"request": {"timeout": timeout}})
    with pytest.raises(ValueError, match="timeout"):
        Config()


@pytest.mark.parametrize("timeout", [1, 30, 0.25, 2.5])
def test_positive_int_and_float_timeouts_are_preserved(project, timeout):
    write_config(project, {"request": {"timeout": timeout}})
    assert Config().get_timeout() == timeout


@pytest.mark.parametrize("verify_ssl", [0, 1, "true", "false", None, [], {}])
def test_explicit_verify_ssl_must_be_boolean(project, verify_ssl):
    write_config(project, {"request": {"verify_ssl": verify_ssl}})
    with pytest.raises(ValueError, match="verify_ssl"):
        Config()


@pytest.mark.parametrize("verify_ssl", [True, False])
def test_explicit_boolean_ssl_setting_is_preserved(project, verify_ssl):
    write_config(project, {"request": {"verify_ssl": verify_ssl}})
    assert Config().get_verify_ssl() is verify_ssl


def test_default_header_values_are_not_subject_to_new_type_restrictions(project):
    headers = {"X-Number": 7, "X-List": ["one"], "X-Empty": None}
    write_config(project, {"default_headers": headers})
    assert Config().get_default_headers() == headers


def test_utf8_yaml_and_non_ascii_space_path_are_supported(project):
    headers = {"X-Display": "测试 UTF-8"}
    write_config(project, {"base_urls": {}, "default_headers": headers})
    assert Config().get_default_headers() == headers


@pytest.mark.parametrize("invalid_credentials", [None, [], SECRET, {"Host-be": SECRET}])
def test_startup_does_not_eagerly_validate_credentials_or_legacy_passwords(project, monkeypatch, invalid_credentials):
    data = {"users": {"admin": {"password": SECRET}}, "credentials": invalid_credentials}
    write_config(project, data)

    def forbidden(*args, **kwargs):
        raise AssertionError("Startup must not perform credential lookup")

    monkeypatch.setattr(Config, "get_credential_user", forbidden)
    assert Config().data == data


def test_constructor_preserves_host_bound_credentials_and_deep_copy_contract(project):
    legacy = {"username": "LEGACY_USER", "password": SECRET}
    explicit = {"username": "EXPLICIT_USER", "password": SECRET}
    data = {"users": {"admin": legacy}, "credentials": {"Host-other": {"learner": explicit}}}
    write_config(project, data)
    reader = Config()
    assert reader.get_credential_user("Host-be", "admin") == legacy
    actual = reader.get_credential_user("Host-other", "learner")
    assert actual == explicit
    actual["password"] = "changed"
    assert reader.data == data
    with pytest.raises(ValueError):
        reader.get_credential_user("Host-other", "admin")
    with pytest.raises(ValueError):
        reader.get_credential_user("Host-be", "none")


def test_explicit_invalid_credentials_still_fail_only_when_requested(project):
    data = {"users": {"admin": {"username": "legacy", "password": SECRET}},
            "credentials": {"Host-be": {"admin": {"password": SECRET}}}}
    write_config(project, data)
    reader = Config()
    with pytest.raises(ValueError, match="Credential configuration is invalid") as raised:
        reader.get_credential_user("Host-be", "admin")
    assert_safe(raised.value)


def test_invalid_yaml_hides_parser_snippet_and_secret(project, capsys):
    (project / "configs" / "env.local.yaml").write_text(
        f'password: "{SECRET}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="YAML") as raised:
        Config()
    assert_safe(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_invalid_utf8_has_safe_error(project):
    (project / "configs" / "env.local.yaml").write_bytes(b"password: \xff")
    with pytest.raises(ValueError) as raised:
        Config()
    assert "0xff" not in str(raised.value)
    assert str(project) not in str(raised.value)


def test_file_read_failure_hides_os_error_payload(project, monkeypatch):
    write_config(project)

    def denied(*args, **kwargs):
        raise PermissionError(SECRET)

    monkeypatch.setattr("builtins.open", denied)
    with pytest.raises(OSError) as raised:
        Config()
    assert_safe(raised.value)
    assert raised.value.__suppress_context__ is True


def test_subprocess_failure_from_different_cwd_hides_yaml_secret_and_private_path(project, tmp_path):
    private_root = project / SECRET
    (private_root / "configs").mkdir(parents=True)
    (private_root / "configs" / "env.local.yaml").write_text(
        f'password: "{SECRET}\n', encoding="utf-8")
    unrelated = tmp_path / "不同 cwd with spaces"
    unrelated.mkdir()
    env = os.environ.copy()
    package_parent = Path(next(iter(wizbank_api_test.__path__))).parent
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(package_parent), str(Path(__file__).resolve().parents[3]), env.get("PYTHONPATH", ""),
    ]))
    source = (
        "import sys; from wizbank_api_test.core import config as module; "
        "module.__file__ = sys.argv[1]; module.Config()"
    )
    completed = subprocess.run(
        [sys.executable, "-c", source, str(private_root / "core" / "config.py")],
        cwd=unrelated, env=env, capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 1
    assert "ValueError" in completed.stderr
    assert SECRET not in completed.stdout + completed.stderr
    assert str(private_root) not in completed.stdout + completed.stderr


@pytest.mark.parametrize("failure_kind", ["yaml", "schema", "env"])
def test_nested_pytest_failure_output_does_not_expose_raw_yaml_or_private_path(project, tmp_path, failure_kind):
    private_root = project / SECRET
    (private_root / "configs").mkdir(parents=True)
    raw = (f'password: "{SECRET}\n' if failure_kind == "yaml" else
           yaml.safe_dump({"base_urls": SECRET, "users": {"admin": {"password": SECRET}}}))
    (private_root / "configs" / "env.local.yaml").write_text(raw, encoding="utf-8")
    unrelated = tmp_path / "嵌套 pytest cwd"
    unrelated.mkdir()
    config = unrelated / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    test_file = unrelated / "test_startup_failure.py"
    test_file.write_text(
        "import os\nfrom wizbank_api_test.core import config as module\n"
        "def test_invalid_config(monkeypatch):\n"
        "    monkeypatch.setattr(module, '__file__', os.environ['B4_CONFIG_ENTRY'])\n"
        "    module.Config(os.environ.get('B4_CONFIG_ENV', 'local'))\n", encoding="utf-8")
    env = os.environ.copy()
    package_parent = Path(next(iter(wizbank_api_test.__path__))).parent
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(package_parent), str(Path(__file__).resolve().parents[3]), env.get("PYTHONPATH", ""),
    ]))
    env["B4_CONFIG_ENTRY"] = str(private_root / "core" / "config.py")
    env["B4_CONFIG_ENV"] = f"../{SECRET}/private" if failure_kind == "env" else "local"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(config), str(test_file), "-q"],
        cwd=unrelated, env=env, capture_output=True, text=True, timeout=60,
    )
    evidence = completed.stdout + completed.stderr
    assert completed.returncode == 1
    assert "1 failed" in completed.stdout
    expected = {
        "yaml": "Configuration YAML is invalid",
        "schema": "Configuration base_urls must be a Mapping",
        "env": "Configuration environment identifier is invalid",
    }
    assert expected[failure_kind] in evidence
    assert SECRET not in evidence
    assert str(private_root) not in evidence
