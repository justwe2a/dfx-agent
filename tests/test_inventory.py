import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from dfx_agent.cases import discover
from dfx_agent.cli import main


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data_B"
        for split in ("data_B1", "data_B2", "data_B3"):
            (self.root / split).mkdir(parents=True)

    def write(self, relative, content=""):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def app(self, relative):
        self.write(relative + "/build-profile.json5", '{"targetSdkVersion":"6.0.2(22)","storePassword":"DO_NOT_EXPORT"}')
        self.write(relative + "/AppScope/app.json5", '{"bundleName":"example.app"}')

    def test_duplicate_id_different_apps_remain_separate_and_never_execute_tests(self):
        for app in ("First", "Second"):
            prefix = f"data_B1/{app}"
            self.app(prefix)
            self.write(prefix + "/testcases/TC_JSError_001.py", 'raise RuntimeError("must never execute")')
            self.write(prefix + f"/TC_JSError_001-{app}/faultlogger/jscrash.log", "Error name:TypeError")
        report = discover(self.root)
        self.assertEqual(report["summary"]["case_candidate_count"], 2)
        self.assertEqual(report["summary"]["asset_status_counts"], {"PAIRED_ASSETS": 2})
        self.assertEqual(report["summary"]["validated_case_count"], 0)
        self.assertNotIn("DO_NOT_EXPORT", json.dumps(report))
        self.assertTrue(all(c["agent_visible_files"] == [] for c in report["cases"]))

    def test_id_mismatch_is_not_silently_merged(self):
        prefix = "data_B2/Drama"
        self.app(prefix)
        self.write(prefix + "/testcases/TC_JSRangeError_001.py")
        self.write(prefix + "/TC_JSRangeError_024-Drama/faultlogger/jscrash.log", "log")
        report = discover(self.root)
        self.assertEqual({c["asset_status"] for c in report["cases"]}, {"TEST_ONLY", "LOG_ONLY"})
        self.assertIn("POSSIBLE_ID_ALIAS", [i["code"] for i in report["issues"]])

    def test_supplemental_directory_not_counted_as_app(self):
        self.write("data_B1/DFXBenchMark_TC/TC_JSError_001.py")
        self.app("data_B1/School")
        report = discover(self.root)
        self.assertEqual(report["summary"]["app_count"], 1)
        self.assertEqual(report["summary"]["case_candidate_count"], 0)
        self.assertEqual(len(report["supplemental_directories"]), 1)

    def test_scene_name_and_answer_markers_require_review(self):
        self.app("data_B1/Notes")
        self.write("data_B1/Notes/testcases/TC_JSError_006.py", '# 故障位置: secret.ets')
        self.write("data_B1/Notes/TC_JSError_006-News/faultlogger/jscrash.log", "log")
        item = discover(self.root)["cases"][0]
        self.assertIn("SCENE_APP_NAME_MISMATCH", item["issues"])
        self.assertTrue(item["test_scripts"][0]["answer_marker_detected"])
        self.assertEqual(item["context_review"], "PENDING")

    def test_b3_zip_metadata_missing_declared_case_and_entry_conflict(self):
        base = "data_B3/benchmarkfordfx-better"
        self.app(base + "/App/Exam")
        path = self.write(base + "/Logs/故障现场/TC_JSError_019-Exam.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("TC_JSError_019-Exam/faultlogger/jscrash.log", "log")
        self.write(base + "/HypiumProject/testcases/TC_JSError_019.py", "# RunResult")
        self.write(base + "/HypiumProject/main.py", 'main_process("TC_JSError_018")')
        self.write(base + "/HypiumProject/config/user_config.xml", '<testcase name="TC_JSError_021"/>')
        self.write(base + "/README.md", "| TC_JSError_013 | Exam | error |\n| TC_JSError_019 | Exam | error |")
        before = path.read_bytes()
        report = discover(self.root)
        self.assertEqual(report["cases"][0]["asset_status"], "PAIRED_ASSETS")
        self.assertEqual(before, path.read_bytes())
        codes = {i["code"] for i in report["issues"]}
        self.assertIn("DECLARED_CASE_WITHOUT_ARTIFACT_MAPPING", codes)
        self.assertIn("TEST_ENTRY_CONFIG_DIFFER", codes)
        self.assertEqual(len(report["cases"]), 1)

    def test_unsafe_and_invalid_archives_are_not_valid_log_assets(self):
        base = "data_B3/benchmarkfordfx-better"
        self.app(base + "/App/Example")
        unsafe = self.write(base + "/Logs/故障现场/TC_JSError_001-Example.zip")
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("../jscrash.log", "bad")
            archive.writestr("faultlogger/jscrash.log", "log")
        self.write(base + "/Logs/故障现场/TC_JSError_002-Example.zip", "not zip")
        report = discover(self.root)
        self.assertEqual({c["asset_status"] for c in report["cases"]}, {"INCOMPLETE"})
        self.assertIn("UNSAFE_ARCHIVE_MEMBER", report["cases"][0]["issues"])
        self.assertIn("UNREADABLE_ARCHIVE", report["cases"][1]["issues"])

    def test_ambiguous_b3_test_not_assigned_to_multiple_apps(self):
        base = "data_B3/benchmarkfordfx-better"
        for app in ("One", "Two"):
            self.app(base + "/App/" + app)
            path = self.write(base + f"/Logs/故障现场/TC_JSError_001-{app}.zip")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("faultlogger/jscrash.log", "log")
        self.write(base + "/HypiumProject/testcases/TC_JSError_001.py")
        report = discover(self.root)
        self.assertEqual({c["asset_status"] for c in report["cases"]}, {"LOG_ONLY"})
        self.assertIn("UNMAPPED_TEST", [i["code"] for i in report["issues"]])

    def test_zip_with_source_instead_of_log_is_reported(self):
        base = "data_B3/benchmarkfordfx-better"
        self.app(base + "/App/Image")
        path = self.write(base + "/Logs/故障现场/TC_JSError_018-Image.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("MainEntry.ets", "source, not a crash log")
        self.write(base + "/HypiumProject/testcases/TC_JSError_018.py")
        report = discover(self.root)
        self.assertEqual(report["cases"][0]["asset_status"], "TEST_ONLY")
        self.assertEqual(report["cases"][0]["log_archives"][0]["member_preview"], ["MainEntry.ets"])
        self.assertIn("ARCHIVE_LOG_COUNT_NOT_ONE", [i["code"] for i in report["issues"]])

    def test_reports_cannot_be_written_into_original_dataset(self):
        with self.assertRaises(SystemExit) as result:
            main(["inventory", "--dataset", str(self.root), "--out", str(self.root / "reports")])
        self.assertEqual(result.exception.code, 2)
        self.assertFalse((self.root / "reports").exists())

    def test_testcase_bundle_is_not_misclassified_as_missing_crash_log(self):
        prefix = "data_B1/Tool"
        self.app(prefix)
        self.write(prefix + "/testcases/TC_JSError_001.py")
        path = self.write(prefix + "/testcases/TC_JSError_001.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("TC_JSError_001.py", "test source")
            archive.writestr("TC_JSError_001.json", "{}")
        report = discover(self.root)
        item = report["cases"][0]
        self.assertEqual(item["asset_status"], "TEST_ONLY")
        self.assertEqual(len(item["test_archives"]), 1)
        self.assertEqual(item["log_archives"], [])
        self.assertNotIn("ARCHIVE_LOG_COUNT_NOT_ONE", [i["code"] for i in report["issues"]])

    def test_invalid_dataset_has_no_success_report(self):
        with self.assertRaises(ValueError):
            discover(Path(self.temp.name) / "missing")


if __name__ == "__main__":
    unittest.main()
