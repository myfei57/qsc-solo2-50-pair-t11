"""最小 pytest 兼容运行器（离线环境无 pytest 时用于回归验证）。

支持本仓库用到的子集：模块级 ``clock`` / ``platform`` / ``tmp_path`` fixture、
``pytest.raises``、以 ``test_`` 开头的函数。不是 pytest 的替代品，只是离线
交付时的回归门禁；CI 仍以 requirements 里的 pytest 为准。
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import pathlib
import sys
import tempfile
import traceback
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Raises:
    def __init__(self, exc_type) -> None:
        self.exc_type = exc_type
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"期望抛出 {self.exc_type.__name__}，但没有")
        if not issubclass(exc_type, self.exc_type):
            return False
        self.value = exc
        return True


class _Mark:
    def parametrize(self, argnames, argvalues):
        names = [n.strip() for n in argnames.split(",")]

        def decorate(fn):
            cases = getattr(fn, "_lite_cases", [])
            expanded = []
            for values in argvalues:
                if not isinstance(values, tuple):
                    values = (values,)
                expanded.append(dict(zip(names, values)))
            fn._lite_cases = cases + expanded
            return fn

        return decorate


pytest = types.ModuleType("pytest")
pytest.raises = _Raises
pytest.fixture = lambda *a, **k: (lambda f: f)
pytest.mark = _Mark()
sys.modules["pytest"] = pytest


def _load(rel: str):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


conftest = _load("tests/conftest.py")


def make_clock():
    return conftest.clock()


def make_platform(tmp_path):
    clock = conftest.clock()
    return conftest.build_platform(
        conftest.default_config(), clock=clock, storage_root=str(tmp_path / "state")
    )


FIXTURES = {
    "clock": lambda: conftest.clock(),
}


def run_module(rel: str) -> tuple[int, int, list[str]]:
    module = _load(rel)
    passed = 0
    failed = 0
    failures: list[str] = []
    for name, fn in sorted(vars(module).items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        params = inspect.signature(fn).parameters

        def invoke(case_name: str, extra: dict) -> None:
            kwargs = {}
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = pathlib.Path(tmp)
                if "platform" in params:
                    kwargs["platform"] = make_platform(tmp_path)
                if "clock" in params:
                    kwargs["clock"] = conftest.clock()
                if "tmp_path" in params:
                    kwargs["tmp_path"] = tmp_path
                kwargs.update(extra)
                fn(**kwargs)

        cases = getattr(fn, "_lite_cases", None)
        if cases:
            for index, extra in enumerate(cases):
                try:
                    invoke(f"{name}[{index}]", extra)
                    passed += 1
                except Exception:  # noqa: BLE001
                    failed += 1
                    failures.append(f"{rel}::{name}[{index}]\n{traceback.format_exc()}")
            continue

        try:
            invoke(name, {})
            passed += 1
        except Exception:  # noqa: BLE001
            failed += 1
            failures.append(f"{rel}::{name}\n{traceback.format_exc()}")
    return passed, failed, failures


def main(argv: list[str]) -> int:
    targets = argv[1:] or sorted(str(p.relative_to(ROOT)) for p in (ROOT / "tests").glob("test_*.py"))
    total_pass = 0
    total_fail = 0
    all_failures: list[str] = []
    for rel in targets:
        passed, failed, failures = run_module(rel)
        total_pass += passed
        total_fail += failed
        all_failures.extend(failures)
        print(f"{rel}: {passed} passed, {failed} failed")
    if all_failures:
        print("\n=== FAILURES ===")
        for item in all_failures:
            print(item)
    print(f"\n总计 {total_pass} passed, {total_fail} failed")
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
