import os
import shutil
import tempfile

# Must happen before the app is imported: it decides where recordings and config.json live.
_DATA = tempfile.mkdtemp(prefix="rechepis-test-")
os.environ["RECHEPIS_DATA"] = _DATA

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_data():
    from app import config, store

    if config.RECORDINGS_DIR.exists():
        for child in config.RECORDINGS_DIR.glob("*"):
            shutil.rmtree(child, ignore_errors=True)
    if config.CONFIG_PATH.exists():
        config.CONFIG_PATH.unlink()
    store._migrated.clear()
    yield


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_DATA, ignore_errors=True)
