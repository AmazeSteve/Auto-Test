"""Offline proofs for serial functional policy and real pytest scope isolation."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import wizbank_api_test

from wizbank_api_test.tests import conftest as fixtures


FUNCTIONAL_FILES = (
    "test_01_login.py",
    "test_02_user_group.py",
    "test_03_api_cases.py",
    "test_04_user_update_contract.py",
    "test_05_user_update_preflight.py",
    "test_06_user_update_lifecycle_acceptance.py",
)


def config_stub(numprocesses=None, *, worker_count=None, invocation=()):
    config = SimpleNamespace(
        option=SimpleNamespace(numprocesses=numprocesses),
        rootpath=Path("/offline/project"),
        invocation_params=SimpleNamespace(args=invocation),
    )
    config.getoption = lambda name, default=None: (
        numprocesses if name in {"numprocesses", "-n"} else default
    )
    if worker_count is not None:
        config.workerinput = {"workercount": worker_count}
    return config


def item_stub(relative_path):
    return SimpleNamespace(path=Path("/offline/project") / relative_path)


@pytest.mark.parametrize("numprocesses", [None, 0, 1])
def test_serial_functional_collection_is_allowed(numprocesses):
    fixtures.assert_serial_functional_tests(
        config_stub(numprocesses),
        [item_stub("tests/" + filename) for filename in FUNCTIONAL_FILES],
    )


@pytest.mark.parametrize("filename", FUNCTIONAL_FILES)
@pytest.mark.parametrize("worker_mode", ["explicit", "auto", "worker"])
def test_functional_items_reject_multiple_workers_before_execution(filename, worker_mode):
    config = {
        "explicit": config_stub(2),
        "auto": config_stub("auto"),
        "worker": config_stub(worker_count=2),
    }[worker_mode]
    with pytest.raises(pytest.UsageError, match="serial"):
        fixtures.assert_serial_functional_tests(config, [item_stub("tests/" + filename)])


@pytest.mark.parametrize(
    "arguments",
    [("-n", "auto"), ("-n", "logical"), ("-nauto",), ("-nlogical",),
     ("-n=auto",), ("-n=logical",), ("--numprocesses=auto",),
     ("--numprocesses=logical",), ("--numprocesses", "auto")],
)
def test_auto_request_stays_blocked_when_resolved_worker_count_is_one(arguments):
    config = config_stub(1, invocation=arguments)
    with pytest.raises(pytest.UsageError, match="serial"):
        fixtures.assert_serial_functional_tests(config, [item_stub("tests/test_03_api_cases.py")])


def test_worker_mainargv_retains_auto_request_even_for_one_worker():
    config = config_stub(worker_count=1)
    config.workerinput["mainargv"] = ["pytest", "-n", "auto"]
    with pytest.raises(pytest.UsageError, match="serial"):
        fixtures.assert_serial_functional_tests(config, [item_stub("tests/test_03_api_cases.py")])


@pytest.mark.parametrize("numprocesses", [2, "auto", "logical"])
def test_unit_only_collection_can_use_multiple_workers(numprocesses):
    fixtures.assert_serial_functional_tests(
        config_stub(numprocesses),
        [item_stub("tests/unit/test_context.py"), item_stub("tests/unit/api_inventory/test_inputs.py")],
    )


def test_mixed_unit_and_functional_selection_is_rejected():
    items = [item_stub("tests/unit/test_context.py"), item_stub("tests/test_05_user_update_preflight.py")]
    with pytest.raises(pytest.UsageError, match="serial"):
        fixtures.assert_serial_functional_tests(config_stub(2), items)


def test_unit_only_selection_after_deselection_is_allowed():
    unit_item = item_stub("tests/unit/test_context.py")
    items = [unit_item, item_stub("tests/test_06_user_update_lifecycle_acceptance.py")]
    items[:] = [unit_item]
    fixtures.assert_serial_functional_tests(config_stub(2), items)


def test_windows_functional_path_is_rejected():
    item = SimpleNamespace(path=r"C:\offline\project\tests\test_03_api_cases.py")
    with pytest.raises(pytest.UsageError, match="serial"):
        fixtures.assert_serial_functional_tests(config_stub(2), [item])


def test_windows_unit_path_is_allowed():
    item = SimpleNamespace(path=r"C:\offline\project\tests\unit\test_context.py")
    fixtures.assert_serial_functional_tests(config_stub(2), [item])


def test_collection_hook_checks_final_items_after_other_hooks_deselect(monkeypatch):
    original = [item_stub("tests/test_03_api_cases.py")]
    events = []

    def policy(config, items):
        events.append((config, tuple(items)))

    monkeypatch.setattr(fixtures, "assert_serial_functional_tests", policy)
    config = config_stub(2)
    wrapper = fixtures.pytest_collection_modifyitems(config, original)
    next(wrapper)
    assert events == []
    unit_item = item_stub("tests/unit/test_context.py")
    original[:] = [unit_item]
    with pytest.raises(StopIteration):
        next(wrapper)
    assert events == [(config, (unit_item,))]


def test_worker_rejection_clears_schedulable_items_and_reports_only_safe_policy():
    config = config_stub(worker_count=2)
    config.workeroutput = {}
    secret_case_id = "SECRET_CASE_DO_NOT_REPORT"
    items = [SimpleNamespace(path=Path("/offline/project/tests/test_03_api_cases.py"),
                             nodeid=secret_case_id)]
    wrapper = fixtures.pytest_collection_modifyitems(config, items)
    next(wrapper)
    with pytest.raises(pytest.UsageError, match="serial") as caught:
        next(wrapper)
    assert items == []
    assert config.workeroutput == {"functional_serial_policy_error": str(caught.value)}
    assert secret_case_id not in str(config.workeroutput)
    assert "test_03_api_cases.py" not in str(config.workeroutput)


def test_controller_converts_worker_policy_rejection_to_usage_error():
    message = "Functional API tests require serial execution"
    node = SimpleNamespace(workeroutput={"functional_serial_policy_error": message})
    with pytest.raises(pytest.UsageError, match="serial") as caught:
        fixtures.pytest_testnodedown(node, error=None)
    assert str(caught.value) == message


@pytest.mark.parametrize("node", [SimpleNamespace(), SimpleNamespace(workeroutput={})])
def test_controller_does_not_reject_ordinary_worker_completion(node):
    fixtures.pytest_testnodedown(node, error=None)


def test_unit_worker_collection_is_not_cleared_or_reported_as_policy_failure():
    config = config_stub(worker_count=2)
    config.workeroutput = {}
    item = item_stub("tests/unit/test_context.py")
    items = [item]
    wrapper = fixtures.pytest_collection_modifyitems(config, items)
    next(wrapper)
    with pytest.raises(StopIteration):
        next(wrapper)
    assert items == [item]
    assert config.workeroutput == {}


def test_actual_functional_xdist_policy_exits_usage_error_before_any_fixture_or_body(tmp_path):
    """Synthetic API-shaped item uses no API; marker proves nothing executed."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    marker = tmp_path / "executed.marker"
    (tests_dir / "test_parallel_probe.py").write_text(
        '''
from pathlib import Path
import pytest

MARKER = Path(''' + repr(str(marker)) + ''')

@pytest.fixture
def observed_fixture():
    MARKER.write_text("fixture", encoding="utf-8")

def test_parallel_probe(observed_fixture):
    MARKER.write_text("body", encoding="utf-8")
''',
        encoding="utf-8",
    )
    env = os.environ.copy()
    package_parent = str(Path(next(iter(wizbank_api_test.__path__))).parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_parent, env.get("PYTHONPATH", "")]))
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "wizbank_api_test.tests.conftest", "-n", "2", "-q",
         "tests/test_parallel_probe.py"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == int(pytest.ExitCode.USAGE_ERROR), output
    assert "Functional API tests require serial execution" in output, output
    assert "INTERNALERROR" not in output, output
    assert not marker.exists(), output


@pytest.mark.parametrize(
    ("outcome", "expected_code", "expected_summary"),
    [("success", 0, "2 passed"), ("exception", 1, "1 failed, 1 passed"), ("skip", 0, "1 passed, 1 skipped")],
)
def test_default_pytest_fixture_isolates_items_and_unbinds_after_every_outcome(
    tmp_path, outcome, expected_code, expected_summary,
):
    """A separate pytest invocation exercises the actual autouse fixtures."""
    (tmp_path / "conftest.py").write_text(
        '''
import pytest
import requests
from wizbank_api_test.core.runtime_context import get_runtime_context

observed = []

@pytest.fixture(autouse=True)
def forbid_real_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Fixture isolation proof must not use real HTTP")
    monkeypatch.setattr(requests.Session, "request", forbidden)

@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    yield
    with pytest.raises(RuntimeError, match="No active RuntimeContext"):
        get_runtime_context()
    observed.append(item.name)

def pytest_sessionfinish(session, exitstatus):
    assert observed == ["test_first", "test_second"]
''',
        encoding="utf-8",
    )
    ending = {
        "success": "return",
        "exception": 'raise RuntimeError("expected isolation exception")',
        "skip": 'pytest.skip("expected isolation skip")',
    }[outcome]
    (tmp_path / "test_fixture_scope.py").write_text(
        '''
import pytest
from wizbank_api_test.core.context import Context
from wizbank_api_test.core.runtime_context import get_runtime_context

contexts = []

def test_first(runtime_context):
    assert get_runtime_context() is runtime_context
    contexts.append(runtime_context)
    Context.set_variable("first_only", "value")
    Context.mark_case_passed("FIRST_ONLY")
    Context.mark_effect("FIRST_ONLY", state=Context.EFFECT_APPLIED)
    ''' + ending + '''

def test_second(runtime_context):
    assert get_runtime_context() is runtime_context
    assert runtime_context is not contexts[0]
    assert not Context.has_variable("first_only")
    assert not Context.is_case_passed("FIRST_ONLY")
    assert Context.get_effect("FIRST_ONLY") is None
    assert Context.all() == {"variables": {}, "cases": {}, "effects": {}}
    assert Context.snapshot() == {"size": 0, "data": Context.all()}
''',
        encoding="utf-8",
    )
    env = os.environ.copy()
    package_parent = str(Path(next(iter(wizbank_api_test.__path__))).parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_parent, env.get("PYTHONPATH", "")]))
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "wizbank_api_test.tests.conftest", "-q", "test_fixture_scope.py"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == expected_code, output
    assert expected_summary in output, output
    assert "ERROR" not in output, output
