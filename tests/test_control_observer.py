import threading
from types import SimpleNamespace

from src.control_observer import NativeControlObserver, control_coverage
from tests.helpers import TemporaryDirectoryTestCase


class ControlObserverTests(TemporaryDirectoryTestCase):
    def setup_observer(self, function, kind="refusal_reason"):
        module = SimpleNamespace(check=function)
        observer = NativeControlObserver(
            {"fake": module}, [("fake", "check", "test_control", kind)]
        )
        events = []

        def sink(event, **fields):
            events.append({"event": event, **fields})

        return module, observer, events, sink

    def test_records_allows_refusals_and_restores_without_extra_invocations(self):
        calls = []

        def original(filepath, content=None):
            calls.append(filepath)
            return "blocked" if filepath == "bad" else None

        module, observer, events, sink = self.setup_observer(original)
        with observer:
            observer.start_call(sink, "one", "write_file")
            self.assertIsNone(module.check("good", content="do not copy content"))
            self.assertEqual("blocked", module.check("bad"))
            observer.end_call("one")
        self.assertIs(module.check, original)
        self.assertEqual(["good", "bad"], calls)
        results = [e for e in events if e["event"] == "native_control_result"]
        self.assertEqual(
            ["no_refusal_returned", "refusal_returned"], [e["disposition"] for e in results]
        )
        self.assertNotIn("do not copy content", str(events))

    def test_thread_contexts_do_not_mix_call_ids(self):
        barrier = threading.Barrier(2)

        def original(filepath):
            barrier.wait(timeout=3)
            return filepath

        module, observer, events, sink = self.setup_observer(original)

        def worker(name):
            observer.start_call(sink, name, "write_file")
            module.check(name)
            observer.end_call(name)

        with observer:
            threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        results = [e for e in events if e["event"] == "native_control_result"]
        self.assertEqual(2, len(results))
        self.assertTrue(all(e["call_id"] == e["result"] for e in results))

    def test_logging_failure_does_not_change_native_decision(self):
        def original(filepath):
            return None

        module, observer, _, _ = self.setup_observer(original)

        def broken_sink(*args, **kwargs):
            raise OSError("disk full")

        with observer:
            observer.start_call(broken_sink, "x", "write_file")
            self.assertIsNone(module.check("safe"))
            observer.end_call("x")
        self.assertTrue(observer.errors)
        self.assertIs(module.check, original)

    def test_exceptions_are_preserved_and_no_events_without_call_context(self):
        def original(filepath):
            raise ValueError("native error")

        module, observer, events, sink = self.setup_observer(original)
        with observer:
            with self.assertRaisesRegex(ValueError, "native error"):
                module.check("outside")
            self.assertEqual([], events)
            observer.start_call(sink, "x", "write_file")
            with self.assertRaisesRegex(ValueError, "native error"):
                module.check("inside")
            observer.end_call("x")
        self.assertEqual("native_control_exception", events[-1]["event"])

    def test_missing_hooks_and_shell_mutations_do_not_automatically_imply_failure(self):
        with NativeControlObserver({}, [("missing", "check", "test", "predicate")]) as observer:
            self.assertEqual(["missing.check"], observer.manifest["unavailable"])
        rows = control_coverage(
            [{"id": "c", "name": "terminal"}],
            [
                {"event": "tool_state_before", "id": "c", "workspace": {}},
                {"event": "tool_state_after", "id": "c", "workspace": {"a": {"sha256": "new"}}},
            ],
        )
        self.assertEqual(["a"], rows[0]["changed_workspace_paths"])
        self.assertEqual("not_inferred", rows[0]["enforcement_failure"])
