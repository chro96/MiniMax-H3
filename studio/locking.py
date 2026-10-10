"""Exclusive worker lease using only the platform's standard library."""
import errno
import os


class FileLock:
    def __init__(self, handle, release):
        self.handle = handle
        self.release = release

    @classmethod
    def acquire(cls, path):
        handle = open(path, "a+b", buffering=0)
        try:
            if os.name == "nt":
                import msvcrt
                # Windows locks a byte range starting at the current position.
                # Keep a persistent byte; never truncate another worker's lock.
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

                def release():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                def release():
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return cls(handle, release)
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise BlockingIOError("Studio worker is already running") from exc
            raise
        except BaseException:
            handle.close()
            raise

    def close(self):
        if self.handle.closed:
            return
        try:
            self.release()
        finally:
            self.handle.close()
