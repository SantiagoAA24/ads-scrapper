import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "search_response.json"


@pytest.fixture(scope="session")
def payload_text():
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture()
def payload(payload_text):
    return json.loads(payload_text)
