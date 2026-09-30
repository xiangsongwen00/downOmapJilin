"""The lock must distinguish a running worker from a leftover lock file."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from index import active_instance, single_instance
from src.config_gui import ConfigWindow


class RunLockTests(unittest.TestCase):
    @staticmethod
    def start_worker(lock):
        code = (
            "from index import single_instance; from pathlib import Path; "
            "import sys, time; "
            "with_block = single_instance(Path(sys.argv[1])); "
            "with_block.__enter__(); print('READY', flush=True); time.sleep(30)"
        )
        worker = subprocess.Popen(
            [sys.executable, "-c", code, str(lock)],
            cwd=Path(__file__).resolve().parent.parent,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if worker.stdout.readline().strip() != "READY":
            stderr = worker.stderr.read()
            worker.wait(timeout=10)
            raise AssertionError(f"Worker did not acquire lock: {stderr}")
        return worker

    def test_terminated_worker_releases_lock_without_deleting_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "pipeline.lock"
            worker = self.start_worker(lock)
            try:
                self.assertEqual(active_instance(lock)["pid"], worker.pid)
            finally:
                worker.terminate()
                worker.communicate(timeout=10)
            self.assertTrue(lock.exists())
            self.assertIsNone(active_instance(lock))
            with single_instance(lock):
                self.assertEqual(active_instance(lock)["pid"], os.getpid())
            self.assertIsNone(active_instance(lock))

    def test_stop_button_can_stop_a_worker_from_an_earlier_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "pipeline.lock"
            worker = self.start_worker(lock)
            class Field:
                def get(self):
                    return str(lock)

            class Status:
                def set(self, _):
                    pass

            class Window:
                process = None
                config_path = Path(temporary) / "config.json"
                values = {"lock_file": Field()}
                status = Status()

            try:
                self.assertTrue(ConfigWindow.stop(Window()))
                self.assertIsNone(active_instance(lock))
            finally:
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
