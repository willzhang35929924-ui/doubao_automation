"""User-message diagnostics use a fake page and private temporary files only."""
import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from doubao_automation.doubao_agent import DoubaoAgent


class UserMessageInspectionTests(unittest.TestCase):
    def test_user_diagnostic_preserves_requested_text_with_private_permissions(self):
        payload = [dict(data_testid='send_message', data_message_role=None, id='node-1',
                        closest_message_id='message-1', innerText='private-marker',
                        textContent='private-marker full body',
                        message_text_contents=[dict(innerText='private-marker',
                                                    textContent='private-marker full body')])]
        empty = Mock()
        empty.count.return_value = 0
        page = Mock()
        page.url = 'https://www.doubao.com/chat/fixture'
        page.frames = []
        page.locator.return_value = empty
        page.evaluate.return_value = payload
        agent = DoubaoAgent(SimpleNamespace(page=page), {}, 'fixture')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture_user_messages.json'
            path.write_text('old')
            path.chmod(0o644)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                agent.inspect_dump(directory)
            self.assertEqual(json.loads(path.read_text()), payload)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn('private-marker', output.getvalue())
            self.assertIn('1 条', output.getvalue())
            self.assertIn(str(path), output.getvalue())
        script = page.evaluate.call_args.args[0]
        for excluded in ('document.cookie', 'localStorage', 'sessionStorage', '.src', '.url', 'location'):
            self.assertNotIn(excluded, script)


if __name__ == '__main__':
    unittest.main()
