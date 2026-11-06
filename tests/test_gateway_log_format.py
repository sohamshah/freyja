"""Gateway log lines carry the date.

The gateway formatted its timestamps as %H:%M:%S, so gateway.log, which
spans days, could not say which day a line was written.
"""

from __future__ import annotations

import logging
import re

import bridge.gateway.run as gateway_run


def test_gateway_log_lines_carry_the_date(capsys) -> None:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        gateway_run._setup_logging()
        logging.getLogger("freyja.gateway").info("slack adapter connected")
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)

    out = capsys.readouterr().out
    assert re.search(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[INFO\] freyja\.gateway: slack adapter connected$",
        out,
        re.M,
    ), out
