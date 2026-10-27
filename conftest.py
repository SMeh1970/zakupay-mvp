"""Release unreachable SQLite connections before Windows removes test folders."""
import gc
import sys
import tempfile

if sys.platform == 'win32':
    _cleanup = tempfile.TemporaryDirectory.cleanup

    def _collect_then_cleanup(self):
        gc.collect()
        _cleanup(self)

    tempfile.TemporaryDirectory.cleanup = _collect_then_cleanup
