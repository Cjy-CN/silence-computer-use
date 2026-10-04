import time

from ._validation import duration_milliseconds


def wait(seconds=3):
    """Wait locally for a bounded duration without contacting GAC."""
    milliseconds = duration_milliseconds(seconds)
    time.sleep(milliseconds / 1000)
