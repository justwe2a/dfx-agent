import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dfx_agent.doctor import inspect_environment


class DoctorTests(unittest.TestCase):
    @patch("dfx_agent.doctor.registered_deveco", return_value=[])
    @patch("dfx_agent.doctor.shutil.which", return_value=None)
    @patch.dict("os.environ", {}, clear=True)
    def test_custom_root_discovers_file_without_running_it(self, *_):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tool = root / "sdk" / "toolchains" / "hdc.exe"
            tool.parent.mkdir(parents=True)
            tool.write_text("Not executable; discovery must not run me", encoding="utf-8")
            report = inspect_environment([root])
            self.assertEqual(Path(report["tools"]["hdc"]["path"]).resolve(), tool.resolve())
            self.assertEqual(report["runtime_readiness"], "NOT_VERIFIED")
            self.assertEqual(report["device_status"], "NOT_PROBED")
            self.assertIn("hvigor", report["missing_in_search_scope"])


if __name__ == "__main__":
    unittest.main()
