"""Atomic replacement with a short Windows sharing-violation retry."""
import os
import time


def replace(source, target):
    for attempt in range(5):
        try:
            source.replace(target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 4:
                raise
            time.sleep(0.1)
