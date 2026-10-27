from src.runtime_support import current_turn_messages
from tests.helpers import TemporaryDirectoryTestCase


class RuntimeSupportTests(TemporaryDirectoryTestCase):
    def test_turn_boundary_ignores_old_history_length(self):
        messages = [
            {"role": "system", "content": "summary"},
            {"role": "user", "content": "current"},
            {"role": "assistant", "content": "done"},
        ]
        self.assertEqual(messages[1:], current_turn_messages(messages, "current"))
