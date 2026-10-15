"""Errors shared across Spooler's modules.

`jobs.JobError` is this class (jobs re-exports it), so `except jobs.JobError` and `except errors.JobError` catch the same
thing. It lives here so the read-only core can raise it without importing `jobs`, which pulls in the printer modules.
"""


class JobError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
