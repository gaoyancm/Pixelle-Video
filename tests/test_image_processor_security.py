import ast
import logging
import re
from pathlib import Path

import pytest

from pixelle_video.services.api_services.image_processor import ImageProcessor

SOURCE_PATH = (
    Path(__file__).parents[1]
    / "pixelle_video"
    / "services"
    / "api_services"
    / "image_processor.py"
)
TEST_API_KEY = "test-dashscope-key-do-not-use"


class FakeResponse:
    def __init__(self, status_code: int, *, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_image_processor_constructor_has_no_hardcoded_api_key_default() -> None:
    source = SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    constructor = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    argument_names = [argument.arg for argument in constructor.args.args]
    default_names = argument_names[-len(constructor.args.defaults) :]
    defaults = dict(zip(default_names, constructor.args.defaults))

    assert isinstance(defaults["api_key"], ast.Constant)
    assert defaults["api_key"].value is None
    assert re.search(r"\bsk-[A-Za-z0-9_-]{20,}\b", source) is None


def test_missing_dashscope_key_fails_before_network(monkeypatch, caplog, capsys) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    network_called = False

    def unexpected_get(*args, **kwargs):
        nonlocal network_called
        network_called = True
        raise AssertionError("network must not be called without an API key")

    monkeypatch.setattr(
        "pixelle_video.services.api_services.image_processor.requests.get",
        unexpected_get,
    )
    processor = ImageProcessor()

    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        processor.get_upload_policy()

    captured = capsys.readouterr()
    assert network_called is False
    assert TEST_API_KEY not in captured.out
    assert TEST_API_KEY not in captured.err
    assert TEST_API_KEY not in caplog.text


def test_explicit_placeholder_key_builds_authorization_header(monkeypatch) -> None:
    captured_headers = {}

    def fake_get(*args, **kwargs):
        captured_headers.update(kwargs["headers"])
        return FakeResponse(200, payload={"data": {"upload_dir": "test"}})

    monkeypatch.setattr(
        "pixelle_video.services.api_services.image_processor.requests.get",
        fake_get,
    )
    processor = ImageProcessor(api_key=TEST_API_KEY)

    assert processor.get_upload_policy() == {"upload_dir": "test"}
    assert captured_headers["Authorization"] == f"Bearer {TEST_API_KEY}"


def test_dashscope_upload_error_does_not_leak_key(monkeypatch, caplog, capsys) -> None:
    def fake_get(*args, **kwargs):
        return FakeResponse(401, text=f"rejected credential: {TEST_API_KEY}")

    monkeypatch.setattr(
        "pixelle_video.services.api_services.image_processor.requests.get",
        fake_get,
    )
    processor = ImageProcessor(api_key=TEST_API_KEY)

    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError) as error:
        processor.get_upload_policy()

    captured = capsys.readouterr()
    assert TEST_API_KEY not in str(error.value)
    assert TEST_API_KEY not in captured.out
    assert TEST_API_KEY not in captured.err
    assert TEST_API_KEY not in caplog.text
