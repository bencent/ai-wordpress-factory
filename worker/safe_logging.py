"""Bounded Worker log projection; never alter logs outside the current execution."""
import logging
from contextlib import contextmanager
from contextvars import ContextVar

_active=ContextVar('factory_safe_logging',default=False)
_standard=frozenset(logging.makeLogRecord({}).__dict__)


class SafeFactoryLog(logging.Filter):
    def filter(self, record):
        if _active.get():
            record.msg='Factory workflow stage update'
            record.args=()
            record.exc_info=record.exc_text=record.stack_info=None
            for name in set(record.__dict__)-_standard:
                del record.__dict__[name]
        return True


@contextmanager
def safe_factory_logs():
    logger=logging.getLogger('ai_wordpress_factory')
    projection=SafeFactoryLog()
    logger.addFilter(projection)
    token=_active.set(True)
    try: yield
    finally:
        _active.reset(token)
        logger.removeFilter(projection)
