"""The release build is a script, not a package: these tests cover its pure helpers and the files it consumes."""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_build_script():
    spec = importlib.util.spec_from_file_location("build_release", ROOT / "release" / "build_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # only defines things: main() runs under __main__
    return module


build = _load_build_script()


def _requirement_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            names.add(build.canonical(re.split(r"[\[<>=!~ ;]", line, maxsplit=1)[0]))
    return names


def test_the_lock_pins_every_package_exactly():
    pins = build.read_lock()
    assert pins
    for pin in pins:
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[0-9A-Za-z.!+_-]+", pin), pin


def test_the_lock_covers_the_loose_requirements_and_agrees_with_their_pins():
    locked = dict(pin.split("==") for pin in build.read_lock())
    by_name = {build.canonical(k): v for k, v in locked.items()}
    wanted = _requirement_names(ROOT / "requirements.txt") | _requirement_names(ROOT / "requirements-gigaam.txt")
    assert wanted <= set(by_name), wanted - set(by_name)
    for line in (ROOT / "requirements-gigaam.txt").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s#]+)", line.split("#", 1)[0].strip())
        if match:
            assert by_name[build.canonical(match.group(1))] == match.group(2), line


def test_the_gigaam_commit_is_the_one_the_install_script_uses():
    assert build.GIGAAM_ZIP in (ROOT / "Install GigaAM.bat").read_text(encoding="utf-8")


def test_the_embedded_python_finds_the_app_and_its_packages():
    lines = build.pth_text().splitlines()
    assert lines[0] == f"python{build.PYTHON_TAG}.zip"
    assert ".." in lines  # the app sits next to the python folder
    assert r"Lib\site-packages" in lines and "import site" in lines


def test_batch_files_are_staged_with_crlf_whatever_the_tree_holds():
    assert build.crlf(b"a\nb\r\nc") == b"a\r\nb\r\nc"
    assert build.crlf(build.crlf(b"x\ny\n")) == b"x\r\ny\r\n"


def test_runtime_dlls_are_found_by_the_names_the_binaries_import(tmp_path):
    (tmp_path / "a.pyd").write_bytes(b"MZ\0\0KERNEL32.dll\0MSVCP140.dll\0VCRUNTIME140_1.dll\0xx\0msvcp140_atomic_wait.dll\0")
    (tmp_path / "b.dll").write_bytes(b"\0python314.dll\0api-ms-win-crt-runtime-l1-1-0.dll\0")
    (tmp_path / "empty.dll").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"msvcp999.dll")  # not a binary: ignored
    assert build.imported_runtime_dlls(tmp_path) == {"msvcp140.dll", "vcruntime140_1.dll", "msvcp140_atomic_wait.dll"}


def test_license_report_collects_the_texts_and_names_packages_without_one(tmp_path):
    site = tmp_path / "site-packages"
    alpha = site / "alpha-1.0.dist-info"
    (alpha / "licenses").mkdir(parents=True)
    (alpha / "METADATA").write_text("Name: alpha\nVersion: 1.0\nLicense-Expression: MIT\n", encoding="utf-8")
    (alpha / "licenses" / "LICENSE").write_text("ALPHA LICENSE TEXT", encoding="utf-8")
    beta = site / "beta-2.0.dist-info"
    beta.mkdir()
    (beta / "METADATA").write_text("Name: beta\nVersion: 2.0\n", encoding="utf-8")

    report, missing = build.license_report(site, None)

    assert "alpha 1.0 — MIT" in report and "ALPHA LICENSE TEXT" in report
    assert "beta 2.0" in report and missing == ["beta"]


def test_a_package_without_a_license_file_gets_the_text_from_release_licenses(tmp_path):
    site = tmp_path / "site-packages"
    gamma = site / "Gamma_Pkg-3.0.dist-info"
    gamma.mkdir(parents=True)
    (gamma / "METADATA").write_text("Name: Gamma_Pkg\nVersion: 3.0\n", encoding="utf-8")
    supplied = tmp_path / "licenses"
    supplied.mkdir()
    (supplied / "gamma-pkg.txt").write_text("GAMMA LICENSE FROM UPSTREAM", encoding="utf-8")

    report, missing = build.license_report(site, None, supplied)

    assert "GAMMA LICENSE FROM UPSTREAM" in report and missing == []


def test_the_license_texts_kept_for_packages_that_ship_none_are_the_real_ones():
    licenses = ROOT / "release" / "licenses"
    assert "BSD" in (licenses / "antlr4-python3-runtime.txt").read_text(encoding="utf-8")
    assert "Apache License" in (licenses / "sentencepiece.txt").read_text(encoding="utf-8")


def test_the_launcher_never_reads_errorlevel_inside_a_block():
    # %ERRORLEVEL% in a ( ... ) block is expanded before the block runs, so it cannot report the command inside it
    lines = (ROOT / "Start Rechepis.bat").read_text(encoding="utf-8").splitlines()
    code = [line for line in lines if not line.strip().lower().startswith("rem ")]
    assert not any("%ERRORLEVEL%" in line for line in code)
