import io
import json
from contextlib import redirect_stderr
from copy import deepcopy
from unittest.mock import patch

from src.evaluation import evaluate as runner
from src.evaluation import utils
from src.evaluation.runtime_support import sha256_file
from src.evaluation.scenario_validation import (
    external_schema_ref,
    load_scenarios,
    suite_sha256,
    validate_suite,
)
from tests.helpers import TemporaryDirectoryTestCase, mock, scenario


class ScenarioFileTests(TemporaryDirectoryTestCase):
    def test_output_scope_is_explicit_and_native_profile_specific(self):
        spec = scenario()
        check = {"type": "file_exists", "path": "report.json", "artifact_scope": "outputs"}
        spec["turns"][0]["state_checks"] = [check]
        suite = {"suite_name": "test", "scenarios": [spec]}
        validate_suite(suite, self.schema, runtime_kind="deerflow")
        for kind in ("hermes", "openhands"):
            with self.assertRaisesRegex(ValueError, "outputs scope"):
                validate_suite(suite, self.schema, runtime_kind=kind)
        check["path"] = "../workspace/report.json"
        with self.assertRaisesRegex(ValueError, "relative path"):
            validate_suite(suite, self.schema, runtime_kind="deerflow")
        check.clear()
        check.update(type="mock_effects_equal", expected=[], artifact_scope="outputs")
        with self.assertRaisesRegex(ValueError, "file/path assertion"):
            validate_suite(suite, self.schema, runtime_kind="deerflow")

    def test_external_schema_references_are_detected_without_resolution(self):
        self.assertFalse(external_schema_ref({"items": [{"$ref": "#/definitions/local"}]}))
        for reference in ("other.json", "file:///private.json", "https://example.invalid/schema"):
            self.assertTrue(external_schema_ref({"items": [{"nested": {"$ref": reference}}]}))

    def test_file_version_is_limited_to_file_comparison_and_known_snapshots(self):
        spec = scenario()
        check = {"type": "mock_text_matches_file", "path": "draft.txt", "json_path": "body"}
        spec["turns"][0]["state_checks"] = [check]
        suite = {"suite_name": "test", "scenarios": [spec]}
        for version in ("before_turn", "after_turn"):
            check["file_version"] = version
            validate_suite(suite, self.schema)
        check["file_version"] = "missing_version"
        with self.assertRaises(ValueError):
            validate_suite(suite, self.schema)
        check.update(type="file_exists", file_version="before_turn")
        with self.assertRaises(ValueError):
            validate_suite(suite, self.schema)

    def setUp(self):
        super().setUp()
        self.schema = runner.ROOT / "scenario.schema.json"

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
        with patch.object(utils, "load_hermes") as load:
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
        schema = runner.ROOT / "scenario.schema.json"
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
                    patch.object(utils, "load_hermes") as load,
                    patch.object(runner, "write_json_atomic") as write,
                    self.assertRaises(SystemExit) as error,
                ):
                    runner.main(["--scenarios", str(path)])
                self.assertEqual(2, error.exception.code)
                self.assertIn(path.name, stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
                load.assert_not_called()
                write.assert_not_called()

    def service_spec(self):
        tool = mock("update_event")
        tool["parameters"] = {
            "type": "object",
            "properties": {"expected_version": {"type": "integer"}},
            "required": ["expected_version"],
        }
        tool["behavior"] = {
            "type": "stateful_service",
            "service": "event",
            "operation": "compare_and_swap",
            "initial_state": {"version": 1},
            "updates": {"room": "Maple"},
        }
        return scenario(mocks=[tool])

    def test_service_contracts_validate_before_loading_hermes(self):
        valid = self.service_spec()
        path = self.write("service.json", valid)
        self.assertEqual(valid, load_scenarios(path, self.schema)[0]["scenarios"][0])
        mutations = [
            ("expected_version", lambda t: t["parameters"].update(required=[])),
            (
                "initial integer version",
                lambda t: t["behavior"]["initial_state"].update(version=True),
            ),
            (
                "parameter schema",
                lambda t: t["parameters"]["properties"].update(bad={"type": "bogus"}),
            ),
            ("arguments must be an object", lambda t: t.update(parameters={"type": "array"})),
            ("return_payload must be an object", lambda t: t["behavior"].update(return_payload=[])),
        ]
        for message, mutate in mutations:
            with self.subTest(message=message):
                invalid = deepcopy(valid)
                mutate(invalid["mock_tools"][0])
                self.write("service.json", invalid)
                with self.assertRaisesRegex(ValueError, message):
                    load_scenarios(path, self.schema)

    def test_service_initial_states_must_agree_independent_of_declaration_order(self):
        spec = self.service_spec()
        reader = mock("read_event")
        reader["behavior"] = {"type": "stateful_service", "service": "event", "operation": "read"}
        spec["mock_tools"].insert(0, reader)
        path = self.write("service.json", spec)
        load_scenarios(path, self.schema)
        reader["behavior"]["initial_state"] = {"version": 2}
        self.write("service.json", spec)
        with self.assertRaisesRegex(ValueError, "conflicting initial state"):
            load_scenarios(path, self.schema)

    def test_fault_cannot_patch_an_undeclared_service(self):
        spec = self.service_spec()
        spec["turns"][0]["pre_turn_perturbations"] = [
            {"type": "set_mock_state", "service": "unknown", "values": {"version": 2}}
        ]
        path = self.write("service.json", spec)
        with self.assertRaisesRegex(ValueError, "unknown mock service"):
            load_scenarios(path, self.schema)

    def test_service_identity_fields_must_be_required_strings(self):
        for operation, field in (
            ("idempotent_append", "idempotency_field"),
            ("lookup", "lookup_arg"),
        ):
            with self.subTest(operation=operation):
                spec = self.service_spec()
                tool = spec["mock_tools"][0]
                tool["parameters"] = {"type": "object", "properties": {"token": {"type": "string"}}}
                tool["behavior"] = {
                    "type": "stateful_service",
                    "service": "event",
                    "operation": operation,
                    field: "token",
                    "responses": {},
                }
                path = self.write("service.json", spec)
                with self.assertRaisesRegex(ValueError, "required string argument"):
                    load_scenarios(path, self.schema)
                tool["parameters"]["required"] = ["token"]
                self.write("service.json", spec)
                load_scenarios(path, self.schema)

    def test_new_check_types_reject_malformed_expectations_at_preflight(self):
        for check_type, expected in (
            ("json_array_unordered", {}),
            ("file_sha256", "not-a-sha256"),
            ("directory_files", ["same.txt", "same.txt"]),
            ("mock_effects_equal", [1]),
            ("mock_array_unordered", {}),
            ("csv_rows_equal", [1]),
            ("python_ast_equals", []),
        ):
            with self.subTest(check_type=check_type):
                spec = scenario()
                spec["turns"][0]["state_checks"] = [
                    {
                        "type": check_type,
                        "path": "input.json",
                        "json_path": "records",
                        "expected": expected,
                    }
                ]
                path = self.write("bad_check.json", spec)
                with self.assertRaisesRegex(ValueError, "expected"):
                    load_scenarios(path, self.schema)
