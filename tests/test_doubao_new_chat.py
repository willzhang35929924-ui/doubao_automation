"""New-chat isolation fixtures never launch browsers or send generation requests."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from doubao_automation import doubao_agent as module
from doubao_automation.doubao_agent import DoubaoAgent, GenerationFailed


class Locator:
    def __init__(self, page, kind, index=0):
        self.page, self.kind, self.index = page, kind, index

    def nth(self, index):
        return Locator(self.page, self.kind, index)

    def count(self):
        state = self.page.state
        if self.kind == 'history':
            return state.get('history', 0)
        if self.kind == 'editor':
            return len(state.get('editors', [(True, True)]))
        if self.kind == 'button':
            return int(state.get('button', True))
        return 0

    def is_visible(self):
        if self.kind == 'editor':
            return self.page.state.get('editors', [(True, True)])[self.index][0]
        return bool(self.count())

    def is_enabled(self):
        if self.kind == 'editor':
            return self.page.state.get('editors', [(True, True)])[self.index][1]
        return True

    def click(self, **kwargs):
        self.page.clicks += 1
        self.page.clicked_at = self.page.now


class Page:
    def __init__(self, before=None, after=None):
        self.now = 0
        self.clicks = 0
        self.clicked_at = None
        self.before = before or dict(url='https://www.doubao.com/chat/old', history=1)
        self.after = after or [(0, dict(url='https://www.doubao.com/chat/', history=0))]
        self.gotos = []
        self.selector_calls = []
        self.button_label = '新对话 ⇧ ⌘ K'

    @property
    def state(self):
        if self.clicked_at is None:
            return self.before
        elapsed = self.now - self.clicked_at
        return next(state for when, state in reversed(self.after) if elapsed >= when)

    @property
    def url(self):
        return self.state['url']

    def locator(self, selector):
        self.selector_calls.append(selector)
        if 'receive_message' in selector:
            return Locator(self, 'history')
        if selector == '#new-chat':
            return Locator(self, 'button')
        if 'contenteditable' in selector or 'textbox' in selector:
            return Locator(self, 'editor')
        return Locator(self, 'other')

    def get_by_role(self, role, name):
        return Locator(self, 'button' if role == 'button' and name.search(self.button_label) else 'other')

    def goto(self, url, **kwargs):
        self.gotos.append(url)

    def wait_for_timeout(self, milliseconds):
        self.now += milliseconds / 1000


class NewChatTests(unittest.TestCase):
    def agent(self, page, selectors=None, timeout=3):
        session = SimpleNamespace(page=page, cookies=lambda url: [])
        cfg = {'doubao': {'selectors': selectors or {}, 'new_chat_timeout_seconds': timeout}}
        return DoubaoAgent(session, cfg, 'fixture')

    def run_new_chat(self, page, **kwargs):
        with patch.object(module.time, 'monotonic', side_effect=lambda: page.now):
            self.agent(page, **kwargs).new_chat()

    def test_missing_config_clicks_current_chinese_new_chat_and_proves_stability(self):
        page = Page()
        self.run_new_chat(page)
        self.assertEqual(page.clicks, 1)
        self.assertEqual(page.gotos, [])
        self.assertGreaterEqual(page.now, 1)

    def test_current_english_button_is_supported(self):
        page = Page()
        page.button_label = 'New chat'
        self.run_new_chat(page)
        self.assertEqual(page.clicks, 1)

    def test_configured_string_is_one_selector_not_characters(self):
        page = Page()
        self.run_new_chat(page, selectors={'new_chat': '#new-chat'})
        self.assertIn('#new-chat', page.selector_calls)
        self.assertNotIn('#', page.selector_calls)
        self.assertEqual(page.clicks, 1)

    def test_root_navigation_without_real_control_never_counts_as_new_chat(self):
        page = Page(before=dict(url='https://www.doubao.com/chat/', history=0, button=False))
        with self.assertRaisesRegex(GenerationFailed, '仅访问 /chat/'):
            self.run_new_chat(page, timeout=.7)
        self.assertEqual(page.clicks, 0)
        self.assertEqual(len(page.gotos), 1)

    def test_click_that_leaves_old_history_fails_before_upload_or_send(self):
        page = Page(after=[(0, dict(url='https://www.doubao.com/chat/', history=1))])
        with self.assertRaisesRegex(GenerationFailed, '避免误领旧视频'):
            self.run_new_chat(page, timeout=1.5)
        self.assertEqual(page.clicks, 1)

    def test_click_that_keeps_existing_conversation_route_fails(self):
        page = Page(after=[(0, dict(url='https://www.doubao.com/chat/old', history=0))])
        with self.assertRaises(GenerationFailed):
            self.run_new_chat(page, timeout=1.5)

    def test_transient_empty_dom_before_old_history_hydrates_is_not_accepted(self):
        page = Page(after=[(0, dict(url='https://www.doubao.com/chat/', history=0)),
                           (.5, dict(url='https://www.doubao.com/chat/old', history=1))])
        with self.assertRaises(GenerationFailed):
            self.run_new_chat(page, timeout=1.5)

    def test_waits_for_delayed_empty_composer_and_resets_stable_window(self):
        page = Page(after=[(0, dict(url='https://www.doubao.com/chat/', history=0)),
                           (.5, dict(url='https://www.doubao.com/chat/old', history=1)),
                           (1.2, dict(url='https://www.doubao.com/chat/', history=0))])
        self.run_new_chat(page)
        self.assertGreaterEqual(page.now, 2.2)

    def test_visible_enabled_editor_is_required_and_hidden_first_is_ignored(self):
        page = Page(after=[(0, dict(url='https://www.doubao.com/chat/', history=0,
                                  editors=[(False, True), (True, True)]))])
        self.run_new_chat(page)
        page = Page(after=[(0, dict(url='https://www.doubao.com/chat/', history=0,
                                  editors=[(True, False)]))])
        with self.assertRaises(GenerationFailed):
            self.run_new_chat(page, timeout=1.5)

    def test_untrusted_root_route_is_not_accepted(self):
        page = Page(after=[(0, dict(url='https://example.com/chat/', history=0))])
        with self.assertRaises(GenerationFailed):
            self.run_new_chat(page, timeout=1.5)


if __name__ == '__main__':
    unittest.main()
