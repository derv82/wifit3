"""A ``TRACE`` log level (below ``DEBUG``) built into logger.trace()."""
from __future__ import annotations

import logging

TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def _trace(self: logging.Logger, msg: str, *args, **kwargs) -> None:
    if self.isEnabledFor(TRACE):
        self._log(TRACE, msg, args, **kwargs)

logging.Logger.trace = _trace
