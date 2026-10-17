import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class ProcessOwnerTests(unittest.TestCase):
    def test_monitor_does_not_block_spawned_scientific_workers(self):
        source = f'''import sys
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, {str(ROOT / "scripts")!r})
from web_process_owner import watch_owner
def compute(value):
    import numpy as np
    return int(np.array([value]).sum())
if __name__ == '__main__':
    watch_owner()
    with ProcessPoolExecutor(max_workers=2, initializer=watch_owner) as pool:
        print(list(pool.map(compute, [1, 2, 3])), flush=True)
'''
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "owner_pool.py"
            script.write_text(source, encoding="utf-8")
            result = subprocess.run([sys.executable, str(script)], env={**os.environ, "FINANCE_OWNER_PID": str(os.getpid())}, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[1, 2, 3]", result.stdout)

if __name__ == "__main__":
    unittest.main()
