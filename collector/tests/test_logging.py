import logging

from serverroom_monitor.logging_setup import LogfmtFormatter


def test_logfmt_includes_fields_and_quotes() -> None:
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "hello world", None, None)
    record.fields = {"temperature": 22.5, "reason": "a=b"}
    out = LogfmtFormatter().format(record)
    assert out == 'level=warning logger=x msg="hello world" temperature=22.5 reason="a=b"'
