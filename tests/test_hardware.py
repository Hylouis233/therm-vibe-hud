import subprocess
import unittest
from unittest import mock

from sources import hardware


class HardwareMemoryTests(unittest.TestCase):
    def test_memory_usage_excludes_inactive_file_cache(self):
        vm_stat = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages active:                               1000.
Pages inactive:                             2000.
Pages wired down:                             20.
Anonymous pages:                              100.
Pages occupied by compressor:                  10.
"""

        def fake_run(args, **_kwargs):
            if args == ["vm_stat"]:
                return subprocess.CompletedProcess(args, 0, vm_stat, "")
            if args == ["sysctl", "-n", "hw.memsize"]:
                return subprocess.CompletedProcess(args, 0, str(4000 * 4096), "")
            raise AssertionError(f"unexpected command: {args}")

        with mock.patch.object(hardware.subprocess, "run", side_effect=fake_run):
            used_gb, total_gb, available_gb = hardware._mem_usage_gb()

        page_gb = 4096 / 1024**3
        self.assertAlmostEqual(used_gb, 130 * page_gb)
        self.assertAlmostEqual(total_gb, 4000 * page_gb)
        self.assertAlmostEqual(available_gb, 3870 * page_gb)

    def test_active_pages_are_a_conservative_legacy_fallback(self):
        vm_stat = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages active:                                 50.
Pages inactive:                             2000.
Pages wired down:                             20.
Pages occupied by compressor:                 10.
"""

        def fake_run(args, **_kwargs):
            if args == ["vm_stat"]:
                return subprocess.CompletedProcess(args, 0, vm_stat, "")
            if args == ["sysctl", "-n", "hw.memsize"]:
                return subprocess.CompletedProcess(args, 0, str(4000 * 4096), "")
            raise AssertionError(f"unexpected command: {args}")

        with mock.patch.object(hardware.subprocess, "run", side_effect=fake_run):
            used_gb, _total_gb, _available_gb = hardware._mem_usage_gb()

        self.assertAlmostEqual(used_gb, 80 * 4096 / 1024**3)


if __name__ == "__main__":
    unittest.main()
