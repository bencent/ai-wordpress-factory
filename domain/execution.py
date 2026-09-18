"""Execution ownership, independent of Factory and storage implementation."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RunLease:
    run_id: str
    task_id: str
    owner_id: str
    fencing_token: int


class LeaseLost(RuntimeError):
    def __init__(self):
        super().__init__("Execution ownership is no longer valid")


def validate_interval(value):
    from math import isfinite
    if type(value) not in (int,float) or not isfinite(value) or value <= 0:
        raise ValueError("Timing interval must be finite and positive")
