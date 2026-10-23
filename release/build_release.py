"""Builds the portable Windows release: dist/Rechepis-<version>-win64.zip.

The archive carries its own Python, every package at the exact version in requirements-release.txt, the
app and the launcher. A user unpacks it and double-clicks "Start Rechepis.bat"; nothing is installed.
The speech model (~430 MB) is not in the archive: the program downloads it on first start.

Run it from anywhere on Windows with any Python 3.10+ that has pip (the development .venv will do)::

    .venv\\Scripts\\python.exe release\\build_release.py

Downloads go to build/cache, the staging folder to build/work, the result to dist/. All three are ignored by git.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import mmap
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"

# The interpreter of the release: the version the app is developed and tested on.
PYTHON_VERSION = "3.14.6"
PYTHON_SERIES = "3.14"
PYTHON_TAG = "314"
EMBED_NAME = f"python-{PYTHON_VERSION}-embed-amd64.zip"
EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{EMBED_NAME}"
# python.org lists the same SHA-256 on the page of the release.
EMBED_SHA256 = "df901e84a896ff1ee720ad03377e0c8d8c2244fda79808aeeaff6316df1cb75c"

# gigaam is installed with --no-deps (its own pins do not install on Python 3.14): the same commit as Install GigaAM.bat.
GIGAAM_ZIP = "https://github.com/salute-developers/GigaAM/archive/7447938d791c4f3e643386ee22c33777004293a5.zip"
# Published on PyPI as source only. Pure Python, so it is built into a wheel here and installed from that.
SOURCE_ONLY = ("antlr4-python3-runtime",)

# Import names of the Microsoft C++ runtime. Neither the embeddable Python nor PyTorch ships msvcp140.dll, and a
# clean Windows may not have it, so whatever the binaries import is copied next to python.exe (app-local deployment).
RUNTIME_IMPORT = re.compile(
    rb"(?i)(?<![A-Za-z0-9_])(?:msvcp|vcruntime|concrt|vcomp|vccorlib|vcamp)[0-9]{3}(?:_[0-9a-z]+)*\.dll"
)

SMOKE_TEST = """
import importlib, sys
for name in ("app.main", "fastapi", "uvicorn", "httpx", "numpy", "scipy", "soundfile", "sentencepiece",
             "torch", "torchaudio", "gigaam", "hydra", "omegaconf"):
    importlib.import_module(name)
import numpy, torch
from app.providers.gigaam import is_installed
assert is_installed(), "the app reports the GigaAM packages as missing"
print("python", sys.version.split()[0], "| torch", torch.__version__, "| numpy", numpy.__version__)
"""


# -- small pure helpers (covered by tests/test_release.py) -----------------------------------------


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_version() -> str:
    text = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        raise SystemExit("app/__init__.py has no __version__")
    return match.group(1)


def read_lock(path: Path | None = None) -> list[str]:
    pins = []
    for line in (path or ROOT / "requirements-release.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pins.append(line)
    return pins


def pth_text() -> str:
    """python3xx._pth of the embeddable distribution.

    Its presence switches Python into isolated mode (no PYTHONPATH, no current directory), so everything the app
    needs is listed here, relative to the file: the app itself sits next to the ``python`` folder.
    """
    return "\n".join([f"python{PYTHON_TAG}.zip", ".", "..", r"Lib\site-packages", "", "import site", ""])


def crlf(data: bytes) -> bytes:
    """cmd.exe is unreliable with LF-only batch files; the working tree may hold either."""
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def imported_runtime_dlls(root: Path) -> set[str]:
    """Names of the MSVC runtime DLLs that any .dll/.pyd under ``root`` mentions (lower case)."""
    found: set[str] = set()
    for path in root.rglob("*"):
        if path.suffix.lower() not in (".dll", ".pyd") or not path.is_file() or path.stat().st_size == 0:
            continue
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            found.update(m.group(0).decode("ascii").lower() for m in RUNTIME_IMPORT.finditer(mapped))
    return found


LICENSE_FILE = re.compile(r"(?i)^(licen[cs]e|copying|notice|authors)([-_. ].*)?$")


def license_report(site: Path, python_license: Path | None, supplied: Path | None = None) -> tuple[str, list[str]]:
    """THIRD-PARTY-LICENSES.txt: every installed distribution with the license texts it carries.

    A package that ships without a license file gets ``<supplied>/<canonical name>.txt`` instead, if there is one
    (release/licenses holds texts copied from the projects' own repositories); the names still without are returned.
    """
    rule = "=" * 78
    out = [
        "Third-party components shipped in this archive",
        rule,
        "Речепись itself is under the MIT License (see LICENSE). The components below are included unchanged",
        "under their own licenses. The speech model is not part of this archive: it is downloaded from its",
        "publisher on first start (GigaAM, https://github.com/salute-developers/GigaAM).",
        "",
        "The Microsoft Visual C++ runtime DLLs next to python.exe (msvcp140.dll and friends) are redistributed",
        "as app-local runtime files of the Microsoft Visual C++ Redistributable.",
        "",
    ]
    missing: list[str] = []
    if python_license and python_license.exists():
        out += [rule, "Python " + PYTHON_VERSION + " (python.org, PSF License)", rule, python_license.read_text("utf-8", "replace"), ""]
    for dist in sorted(site.glob("*.dist-info"), key=lambda p: p.name.lower()):
        meta_file = dist / "METADATA"
        meta = email.parser.Parser().parsestr(meta_file.read_text("utf-8", "replace"), headersonly=True) if meta_file.exists() else {}
        name, version = meta.get("Name", dist.name), meta.get("Version", "")
        license_field = (meta.get("License") or "").strip().splitlines()
        declared = meta.get("License-Expression") or (license_field[0] if license_field else "see the project page")
        out += [rule, f"{name} {version} — {declared[:120]}", meta.get("Home-page") or "", rule]
        texts = [f for f in sorted(dist.rglob("*")) if f.is_file() and LICENSE_FILE.match(f.name)]
        extra = supplied / f"{canonical(name)}.txt" if supplied else None
        if not texts and extra and extra.exists():
            out += [f"--- license text from the project's repository (release/licenses/{extra.name}) ---", extra.read_text("utf-8", "replace").strip(), ""]
        elif not texts:
            missing.append(name)
            out.append("(the package carries no license file; see its project page)")
        for f in texts:
            out += [f"--- {f.relative_to(dist).as_posix()} ---", f.read_text("utf-8", "replace").strip(), ""]
        out.append("")
    return "\n".join(out), missing


# -- steps -----------------------------------------------------------------------------------------


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_python(cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / EMBED_NAME
    if target.exists() and sha256_of(target) == EMBED_SHA256:
        print(f"cached: {target.name}")
        return target
    target.unlink(missing_ok=True)
    part = target.with_name(target.name + ".part")
    print(f"downloading {EMBED_URL}")
    request = urllib.request.Request(EMBED_URL, headers={"User-Agent": "Rechepis-build"})
    with urllib.request.urlopen(request, timeout=60) as response, open(part, "wb") as out:
        shutil.copyfileobj(response, out)
    got = sha256_of(part)
    if got != EMBED_SHA256:
        part.unlink()
        raise SystemExit(f"checksum of {EMBED_NAME} does not match: got {got}, expected {EMBED_SHA256}")
    part.replace(target)
    return target


def run(*cmd: str, **kwargs) -> None:
    shown = " ".join(cmd)
    print("+", shown if len(shown) < 300 else shown[:300] + " …", flush=True)
    subprocess.run(cmd, check=True, **kwargs)


def pip(*args: str) -> None:
    run(sys.executable, "-m", "pip", "--disable-pip-version-check", *args)


def install_packages(site: Path, wheels: Path) -> None:
    pins = read_lock()
    source_only = [p for p in pins if canonical(p.split("==")[0]) in SOURCE_ONLY]
    binary = [p for p in pins if p not in source_only]
    cross = [
        "--only-binary=:all:", "--platform", "win_amd64", "--python-version", PYTHON_SERIES,
        "--implementation", "cp", "--abi", f"cp{PYTHON_TAG}",
    ]
    target = ["--target", str(site), "--no-deps", "--no-compile"]

    step("Building the pure-Python packages that PyPI only has as source (and gigaam) into wheels")
    shutil.rmtree(wheels, ignore_errors=True)
    wheels.mkdir(parents=True)
    pip("wheel", "--no-deps", "--wheel-dir", str(wheels), *source_only, GIGAAM_ZIP)
    built = sorted(wheels.glob("*.whl"))
    not_pure = [w.name for w in built if not w.name.endswith("-none-any.whl")]
    if not_pure or len(built) != len(source_only) + 1:
        raise SystemExit(f"expected {len(source_only) + 1} pure-Python wheels, got {[w.name for w in built]}")

    step(f"Installing {len(binary)} packages for Windows x64 / Python {PYTHON_SERIES} (this downloads PyTorch, ~110 MB)")
    pip("install", *target, *cross, *binary)
    # console-script stubs that pip --target leaves in bin/; the launcher runs "python -m" instead
    shutil.rmtree(site / "bin", ignore_errors=True)

    step("Installing the wheels built above")
    pip("install", *target, *cross, *[str(w) for w in built])
    shutil.rmtree(site / "bin", ignore_errors=True)


def unpack_python(archive: Path, python_dir: Path) -> None:
    python_dir.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(python_dir)
    (python_dir / f"python{PYTHON_TAG}._pth").write_text(pth_text(), encoding="ascii", newline="\r\n")
    (python_dir / "Lib" / "site-packages").mkdir(parents=True)


def copy_project(stage: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "app", stage / "app", ignore=ignore)
    shutil.copytree(ROOT / "web", stage / "web", ignore=ignore)
    shutil.copy2(ROOT / "LICENSE", stage / "LICENSE")
    (stage / "Start Rechepis.bat").write_bytes(crlf((ROOT / "Start Rechepis.bat").read_bytes()))
    guide = (ROOT / "release" / "README.txt").read_bytes()
    (stage / "README.txt").write_bytes(b"\xef\xbb\xbf" + crlf(guide))  # BOM: old Notepad guesses the encoding


def bundle_runtime_dlls(python_dir: Path) -> None:
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    for name in sorted(imported_runtime_dlls(python_dir)):
        if (python_dir / name).exists():
            continue
        source = system32 / name
        if source.exists():
            shutil.copy2(source, python_dir / name)
            print(f"bundled {name}")
        else:
            print(f"warning: {name} is mentioned in the binaries but not in {system32}; skipped")


def precompile(python_dir: Path, stage: Path) -> None:
    """Byte-code for everything, so that the first start does not spend a minute compiling PyTorch."""
    py = python_dir / "python.exe"
    result = subprocess.run(
        [str(py), "-m", "compileall", "-q", "-j", "0", str(python_dir / "Lib" / "site-packages"), str(stage / "app")],
        capture_output=True, text=True,
    )
    if result.returncode:  # a few vendored files are intentionally not valid Python
        print("compileall reported errors in some files (harmless):", result.stdout.strip().splitlines()[:3])


def smoke_test(python_dir: Path) -> None:
    py = python_dir / "python.exe"
    with tempfile.TemporaryDirectory(prefix="rechepis-smoke-") as scratch:
        env = {**os.environ, "RECHEPIS_DATA": scratch}
        run(str(py), "-c", SMOKE_TEST, env=env, cwd=scratch, timeout=900)


def make_zip(stage: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                z.write(path, Path(stage.name) / path.relative_to(stage))
    # LF, not the platform's CRLF: "sha256sum -c" reads the file name up to the line break
    (out.with_name(out.name + ".sha256")).write_text(f"{sha256_of(out)}  {out.name}\n", encoding="ascii", newline="\n")


def mb(path: Path) -> str:
    return f"{path.stat().st_size / 1048576:.0f} MB"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-precompile", action="store_true", help="skip byte-code compilation (smaller archive, slower first start)")
    parser.add_argument("--skip-smoke-test", action="store_true", help="do not import the packages with the bundled Python")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("The release is for Windows: build it on Windows (it runs the bundled python.exe and copies its runtime DLLs).")

    started = time.monotonic()
    version = read_version()
    name = f"Rechepis-{version}-win64"
    stage = BUILD / "work" / name
    python_dir = stage / "python"
    site = python_dir / "Lib" / "site-packages"

    step(f"Building {name}")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)

    step(f"Python {PYTHON_VERSION} (embeddable)")
    unpack_python(fetch_python(BUILD / "cache"), python_dir)

    install_packages(site, BUILD / "cache" / "wheels")

    step("Copying the app, the launcher and the guide")
    copy_project(stage)

    step("Bundling the C++ runtime the binaries import")
    bundle_runtime_dlls(python_dir)

    step("Writing THIRD-PARTY-LICENSES.txt")
    report, missing = license_report(site, python_dir / "LICENSE.txt", ROOT / "release" / "licenses")
    (stage / "THIRD-PARTY-LICENSES.txt").write_bytes(b"\xef\xbb\xbf" + crlf(report.encode("utf-8")))
    if missing:
        print("packages without a license file:", ", ".join(missing))

    if not args.no_precompile:
        step("Compiling byte-code")
        precompile(python_dir, stage)

    if not args.skip_smoke_test:
        step("Smoke test with the bundled Python")
        smoke_test(python_dir)

    step("Creating the archive")
    archive = DIST / f"{name}.zip"
    make_zip(stage, archive)
    print(f"\nDone in {time.monotonic() - started:.0f} s: {archive} ({mb(archive)}), unpacked folder: {stage}")


if __name__ == "__main__":
    main()
