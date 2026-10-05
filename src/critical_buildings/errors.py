"""Errors that carry a plain-English message meant for the person running the tool.

The command line prints the message and exits with status 1; the UI shows it. Library code raises these instead of
SystemExit, so callers (the UI, tests, other programs) can catch them like any other exception.
"""


class PipelineError(Exception):
    """Something the user can fix (an unknown area, invalid rules, a missing description): the message says what."""


class ControlError(PipelineError):
    """A rejected change or invalid state in the rules store, runs and releases."""
