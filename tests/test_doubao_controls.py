"""Exercise the actual browser DOM used by duration selection, without accounts."""
import unittest

from playwright.sync_api import sync_playwright

from doubao_automation.doubao_batch import BatchGenerationMixin
from doubao_automation.paths import use_workspace_temp


class ControlAgent(BatchGenerationMixin):
    dcfg = {}
    cfg = {'video': {'aspect_ratio': '9:16'}}

    def __init__(self, page):
        self.page = page

    def _handle_dialogs(self):
        pass


class DurationControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        use_workspace_temp()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def test_existing_chat_waits_for_delayed_composer_without_toggling_skill(self):
        self.page.set_content('''<div id="composer"></div><script>
          setTimeout(()=>document.querySelector('#composer').innerHTML=
            '<button onclick="this.dataset.clicked=1">视频生成</button>'+
            '<button>9:16 · 15s</button>', 300);
        </script>''')
        ControlAgent(self.page).configure_video_mode()
        self.assertEqual(self.page.get_by_role('button', name='9:16 · 15s').count(), 1)
        self.assertIsNone(self.page.get_by_role('button', name='视频生成').get_attribute('data-clicked'))

    def test_nondefault_duration_uses_real_slider_index_before_returning(self):
        self.page.set_content('''
          <button id="duration" onclick="document.querySelector('section').hidden=false">自动 · 8s</button>
          <section hidden>时长 <span>15s</span>
            <input role="slider" type="range" min="0" max="11" value="4"
              aria-valuemax="11" aria-valuenow="4" oninput="
                this.setAttribute('aria-valuenow',this.value);
                document.querySelector('#duration').textContent='9:16 · '+(Number(this.value)+4)+'s'">
          </section>''')
        ControlAgent(self.page).configure_video_mode()
        self.assertEqual(self.page.get_by_role('slider').get_attribute('aria-valuenow'), '11')
        self.assertEqual(self.page.locator('#duration').inner_text(), '9:16 · 15s')

    def test_slider_waits_for_delayed_saved_duration_label(self):
        self.page.set_content('''
          <button id="duration" onclick="document.querySelector('section').hidden=false">9:16 · 8s</button>
          <section hidden>时长 <span>15s</span>
            <input role="slider" type="range" min="0" max="11" value="4"
              aria-valuemax="11" aria-valuenow="4" oninput="
                this.setAttribute('aria-valuenow',this.value);
                setTimeout(()=>document.querySelector('#duration').textContent='9:16 · 15s',1500)">
          </section>''')
        ControlAgent(self.page).configure_video_mode()
        self.assertEqual(self.page.locator('#duration').inner_text(),'9:16 · 15s')
        self.assertEqual(self.page.get_by_role('slider').get_attribute('aria-valuenow'),'11')

    def test_existing_fifteen_seconds_still_sets_automatic_ratio_to_portrait(self):
        self.page.set_content('''
          <button id="duration" onclick="document.querySelector('section').hidden=false">自动 · 15s</button>
          <section hidden>比例
            <button id="portrait" onclick="this.dataset.clicked=1;
              setTimeout(()=>document.querySelector('#duration').textContent='9:16 · 15s',900)">9:16</button>
          </section>''')
        ControlAgent(self.page).configure_video_mode()
        self.assertEqual(self.page.locator('#portrait').get_attribute('data-clicked'),'1')
        self.assertEqual(self.page.locator('#duration').inner_text(),'9:16 · 15s')

    def test_existing_portrait_fifteen_seconds_does_not_reopen_settings(self):
        self.page.set_content('<button id="duration" onclick="this.dataset.clicked=1">9:16 · 15s</button>')
        ControlAgent(self.page).configure_video_mode()
        self.assertIsNone(self.page.locator('#duration').get_attribute('data-clicked'))

    def test_static_fifteen_seconds_text_is_not_a_selectable_duration(self):
        self.page.set_content('''
          <button id="duration" onclick="document.querySelector('section').hidden=false">9:16 · 8s</button>
          <section hidden>时长 <span id="static" onclick="this.dataset.clicked=1">15s</span></section>''')
        with self.assertRaisesRegex(RuntimeError,'未能确认'):
            ControlAgent(self.page).configure_video_mode()
        self.assertIsNone(self.page.locator('#static').get_attribute('data-clicked'))
        self.assertEqual(self.page.locator('#duration').inner_text(),'9:16 · 8s')

    def test_clicking_duration_option_requires_actual_saved_button_confirmation(self):
        self.page.set_content('''
          <button id="duration" onclick="document.querySelector('section').hidden=false">9:16 · 8s</button>
          <section hidden>时长 <button id="option" onclick="this.dataset.clicked=1">15s</button></section>''')
        with self.assertRaisesRegex(RuntimeError,'未能确认'):
            ControlAgent(self.page).configure_video_mode()
        self.assertEqual(self.page.locator('#option').get_attribute('data-clicked'),'1')
        self.assertEqual(self.page.locator('#duration').inner_text(),'9:16 · 8s')


if __name__ == '__main__':
    unittest.main()
