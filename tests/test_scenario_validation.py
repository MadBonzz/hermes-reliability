import io
import json
from contextlib import redirect_stderr
from unittest.mock import patch

from src import eval_runner as runner
from src.runtime_support import sha256_file
from src.scenario_validation import load_scenarios, suite_sha256, validate_suite
from tests.helpers import TemporaryDirectoryTestCase, mock, scenario


class ScenarioFileTests(TemporaryDirectoryTestCase):
    def setUp(self):
        super().setUp()
        self.schema = runner.PROJECT_ROOT / "scenario.schema.json"

    def write(self, name, spec):
        path = self.root / name
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def test_directory_loads_in_filename_order_with_local_mocks_and_hashes(self):
        second = self.write("02.json", scenario("second", [mock("second_mock")]))
        first = self.write("01.json", scenario("first", [mock("first_mock")]))
        (self.root / "notes.md").write_text("not a scenario")
        suite, sources = load_scenarios(self.root, self.schema)
        self.assertEqual(["first", "second"], [s["id"] for s in suite["scenarios"]])
        self.assertEqual(["first_mock"], [m["name"] for m in suite["scenarios"][0]["mock_tools"]])
        self.assertEqual(["second_mock"], [m["name"] for m in suite["scenarios"][1]["mock_tools"]])
        self.assertEqual([sha256_file(first), sha256_file(second)], [s["sha256"] for s in sources])

    def test_single_file_and_directory_have_equivalent_content(self):
        path = self.write("sample.json", scenario())
        direct, direct_sources = load_scenarios(path, self.schema, "custom")
        directory, directory_sources = load_scenarios(self.root, self.schema, "custom")
        self.assertEqual(direct, directory)
        self.assertEqual(direct_sources, directory_sources)
        self.assertEqual("custom", direct["suite_name"])
        before = suite_sha256(direct)
        path.write_text(json.dumps(scenario(), indent=4), encoding="utf-8")
        self.assertEqual(before, suite_sha256(load_scenarios(path, self.schema, "custom")[0]))

    def test_invalid_sources_and_duplicate_ids_fail_before_execution(self):
        with self.assertRaisesRegex(ValueError, "No scenario JSON"):
            load_scenarios(self.root, self.schema)
        with self.assertRaisesRegex(ValueError, "directory or a JSON"):
            load_scenarios(self.root / "missing.json", self.schema)
        self.write("01.json", scenario())
        self.write("02.json", scenario())
        with self.assertRaisesRegex(ValueError, "duplicate scenario ID"):
            load_scenarios(self.root, self.schema)

    def test_invalid_file_reports_filename_and_rejects_legacy_wrapper(self):
        path = self.write("invalid.json", {"suite_name": "old", "scenarios": [scenario()]})
        with self.assertRaisesRegex(ValueError, "invalid.json"):
            load_scenarios(path, self.schema)
        path.write_text("{broken json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid.json"):
            load_scenarios(path, self.schema)
        invalid = scenario(mocks=[mock()])
        del invalid["mock_tools"][0]["behavior"]
        self.write("invalid.json", invalid)
        with self.assertRaisesRegex(ValueError, "behavior"):
            load_scenarios(path, self.schema)

    def test_cli_validates_single_file_and_selection_without_loading_hermes(self):
        path = self.write("sample.json", scenario())
        with patch.object(runner, "load_hermes") as load:
            self.assertEqual(0, runner.main(["--scenarios", str(path), "--validate-only"]))
            self.assertEqual(
                0,
                runner.main(
                    ["--scenarios", str(self.root), "--scenario-id", "sample", "--validate-only"]
                ),
            )
            with self.assertRaises(SystemExit) as error:
                runner.main(
                    ["--scenarios", str(path), "--scenario-id", "missing", "--validate-only"]
                )
            self.assertEqual(2, error.exception.code)
            load.assert_not_called()

    def test_suite_validation_rejects_bad_checks_and_duplicates(self):
        schema = runner.PROJECT_ROOT / "scenario.schema.json"
        suite = {"suite_name": "test", "scenarios": [scenario()]}
        validate_suite(suite, schema)
        suite["scenarios"].append(scenario())
        with self.assertRaises(ValueError):
            validate_suite(suite, schema)
        suite["scenarios"].pop()
        del suite["scenarios"][0]["turns"][0]["state_checks"][0]["path"]
        with self.assertRaises(Exception):
            validate_suite(suite, schema)

    def test_cli_reports_bad_input_without_creating_run_artifacts(self):
        malformed = self.write("invalid.json", scenario())
        malformed.write_text("{broken json")
        for path in (malformed, self.root / "missing.json"):
            with self.subTest(path=path):
                stderr = io.StringIO()
                with (
                    redirect_stderr(stderr),
                    patch.object(runner, "load_hermes") as load,
                    patch.object(runner, "write_json_atomic") as write,
                    self.assertRaises(SystemExit) as error,
                ):
                    runner.main(["--scenarios", str(path)])
                self.assertEqual(2, error.exception.code)
                self.assertIn(path.name, stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
                load.assert_not_called()
                write.assert_not_called()
