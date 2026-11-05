"""Local browser fixtures only: no accounts, external uploads, or model requests."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

from doubao_automation.gemini_web import GeminiError, GeminiSubmissionUncertain, GeminiWeb
from doubao_automation.paths import use_workspace_temp


EDITOR = '<rich-textarea><div contenteditable="true" role="textbox"></div></rich-textarea>'
MANUAL_SCRIPT = ('Script 01：本地夹具（总时长15秒）\n总时长：15秒\n'
                 '00:00-00:05\n画面：展示产品。\n口播：第一段。\n'
                 '00:05-00:10\n画面：使用产品。\n口播：第二段。\n'
                 '00:10-00:15\n画面：产品定格。\n口播：第三段。')


class GeminiUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        use_workspace_temp()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gemini_upload_test_')
        self.addCleanup(self.temp.cleanup)
        self.video = Path(self.temp.name) / 'synthetic.mp4'
        self.video.write_bytes(b'local fixture; never sent to a server')
        self.page = self.browser.new_page()
        self.page.route('**/*', lambda route: route.abort())
        self.addCleanup(self.page.close)
        self.cfg = {'gemini': {'audit_dir': self.temp.name,
                              'upload_timeout_seconds': 2,
                              'upload_settle_seconds': 0,
                              'upload_control_timeout_seconds': 2,
                              'send_ready_timeout_seconds': 1,
                              'send_ack_timeout_seconds': 1}}
        self.agent = GeminiWeb(SimpleNamespace(page=self.page), self.cfg)

    def fixture(self, extra='', script=''):
        self.page.set_content('<div data-node-type="input-area">' + EDITOR + extra +
                              '</div><script>' + script + '</script>')

    def media_input(self, onchange=None):
        onchange = onchange or self._render_previews_script()
        return '<input id="media" type="file" multiple accept="image/*,video/*" onchange="' + onchange + '">'

    @staticmethod
    def _render_previews_script():
        return ("const root=document.querySelector('#attachments');root.innerHTML='';"
            "for(const f of this.files){const v=f.type.startsWith('video/');"
            "root.insertAdjacentHTML('beforeend',`<uploader-file-preview title=${f.name} style=display:block><gem-media-attachment>"
            "<img src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=='>"
            "<span class=gem-attachment-content>${v?'<div class=time-overlay>0:15</div>':''}</span>"
            "</gem-media-attachment></uploader-file-preview>`)}")

    def test_empty_config_waits_for_delayed_opener_and_chooses_media_input(self):
        markup = ('<button aria-label="上传和工具" onclick="document.querySelector(\'#menu\').hidden=false"></button>'
                  '<div id="menu" hidden><input type="file" accept=".pdf,.txt">' +
                  self.media_input() + '</div>')
        self.fixture('<div id="controls"></div><span id="attachments"></span>',
                     'setTimeout(()=>document.querySelector("#controls").innerHTML=' + json.dumps(markup) + ',200)')
        self.agent.upload_files([self.video])
        self.assertEqual(self.page.locator('uploader-file-preview').get_attribute('title'), self.video.name)
        self.assertEqual(self.page.locator('input[accept=".pdf,.txt"]').evaluate('(e)=>e.files.length'), 0)
        self.assertEqual(self.page.locator('#media').evaluate('(e)=>e.files.length'), 1)
        self.assertEqual(self.page.locator('user-query').count(), 0)

    def test_direct_input_ignores_document_uploader_even_when_it_is_first(self):
        self.fixture('<input type="file" accept=".pdf">' + self.media_input() + '<span id="attachments"></span>')
        self.agent.upload_files([self.video])
        self.assertEqual(self.page.locator('#media').evaluate('(e)=>e.files[0].name'), self.video.name)

    def test_direct_file_chooser_and_string_opener_override(self):
        self.agent.sel['open_upload'] = '#custom-upload'
        self.fixture('<button id="custom-upload">Add</button>'
                     '<span id="attachments"></span>',
                     'document.querySelector("#custom-upload").addEventListener("click",()=>{'
                     'if (!document.querySelector("#media")) {'
                     'const input=document.createElement("input");input.type="file";input.id="media";'
                     'input.accept="video/*";input.onchange=()=>document.querySelector("#attachments").innerHTML=' +
                     json.dumps(self._video_preview(self.video.name)) + ';'
                     'document.querySelector("[data-node-type]").append(input);input.click();}} ,true)')
        self.agent.upload_files([self.video])
        self.assertEqual(self.page.locator('uploader-file-preview').get_attribute('title'), self.video.name)

    def test_multiple_files_require_an_input_that_supports_multiple(self):
        other = Path(self.temp.name) / 'other.mp4'
        other.write_bytes(b'local fixture')
        self.fixture('<input type="file" accept="video/*">' + self.media_input() +
            '<div id="attachments"></div>')
        self.agent.upload_files([self.video, other])
        self.assertEqual(self.page.locator('input').first.evaluate('(e)=>e.files.length'), 0)
        self.assertEqual(self.page.locator('#media').evaluate('(e)=>e.files.length'), 2)

    @staticmethod
    def _video_preview(name='clip.mp4'):
        return (f'<uploader-file-preview title="{name}" style="display:block"><gem-media-attachment>'
                '<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">'
                '<span class="gem-attachment-content"><div class="time-overlay">0:15</div></span>'
                '</gem-media-attachment></uploader-file-preview>')

    def test_mixed_six_images_and_one_video_require_each_loaded_preview(self):
        files=[]
        for index in range(6):
            path=Path(self.temp.name)/f'image-{index}.webp';path.write_bytes(b'local fixture');files.append(path)
        video=Path(self.temp.name)/'clip.mp4';video.write_bytes(b'local fixture');files.append(video)
        self.fixture(self.media_input()+'<div id="attachments"></div>')
        self.agent.upload_files(files)
        self.assertEqual(self.page.locator('uploader-file-preview').count(),7)
        self.assertEqual(self.page.locator('.time-overlay').count(),1)

    def test_mixed_media_missing_preview_does_not_pass(self):
        self.cfg['gemini']['upload_timeout_seconds']=.2
        image=Path(self.temp.name)/'still.webp';image.write_bytes(b'fixture')
        video=Path(self.temp.name)/'clip.mp4';video.write_bytes(b'fixture')
        self.fixture(self.media_input("document.querySelector('#attachments').innerHTML=window.tile")+'<div id="attachments"></div>')
        self.page.evaluate('(tile)=>window.tile=tile',self._video_preview())
        with self.assertRaisesRegex(GeminiError,'未在时限内确认'):
            self.agent.upload_files([image,video])

    def test_mixed_media_busy_or_alert_never_passes(self):
        self.cfg['gemini']['upload_timeout_seconds']=.2
        image=Path(self.temp.name)/'still.webp';image.write_bytes(b'fixture')
        video=Path(self.temp.name)/'clip.mp4';video.write_bytes(b'fixture')
        for marker in ('<div role="progressbar"></div>','<div role="alert">upload rejected</div>'):
            with self.subTest(marker=marker):
                self.fixture(self.media_input()+'<div id="attachments"></div>'+marker)
                with self.assertRaises(GeminiError):self.agent.upload_files([image,video])

    def test_progress_must_finish_even_when_filename_is_already_visible(self):
        self.fixture(self.media_input(
            self._render_previews_script() + ";"
            "document.querySelector('#progress').hidden=false;"
            "setTimeout(()=>{document.querySelector('#progress').hidden=true;window.finished=true},700)") +
            '<span id="attachments"></span><div id="progress" role="progressbar" hidden>Uploading</div>')
        self.agent.upload_files([self.video])
        self.assertTrue(self.page.evaluate('window.finished'))

    def nameless_video_tile(self, loading=False):
        return ('<uploader-file-preview style="display:block"><gem-media-attachment>'
                '<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">'
                '<span class="gem-attachment-content' + (' loading' if loading else '') + '">'
                '<div class="time-overlay">0:15</div></span>'
                '</gem-media-attachment></uploader-file-preview>')

    def test_real_video_tile_without_filename_advances_after_loading_finishes(self):
        tile = self.nameless_video_tile(loading=True)
        self.fixture(self.media_input(
            "document.querySelector('#attachments').innerHTML=window.tile;"
            "setTimeout(()=>{document.querySelector('.gem-attachment-content').classList.remove('loading');"
            "window.finished=true},700)") + '<div id="attachments"></div>')
        self.page.evaluate('(tile)=>window.tile=tile', tile)
        self.agent.upload_files([self.video])
        self.assertTrue(self.page.evaluate('window.finished'))
        self.assertEqual(self.page.locator('uploader-file-preview').inner_text(), '0:15')
        self.assertNotIn(self.video.name, self.page.locator('body').inner_text())

    def test_existing_nameless_video_tile_is_not_proof_of_new_upload(self):
        self.cfg['gemini']['upload_timeout_seconds'] = .2
        self.fixture(self.media_input('void 0') + self.nameless_video_tile())
        with self.assertRaisesRegex(GeminiSubmissionUncertain, '已有未识别附件'):
            self.agent.upload_files([self.video])
        self.assertEqual(self.page.locator('input').evaluate('(e)=>e.files.length'), 0)

    def test_nameless_video_tile_count_must_cover_all_new_files(self):
        self.cfg['gemini']['upload_timeout_seconds'] = .2
        other = Path(self.temp.name) / 'other.mp4'
        other.write_bytes(b'local fixture')
        self.fixture(self.media_input("document.querySelector('#attachments').innerHTML=window.tile") +
                     '<div id="attachments"></div>')
        self.page.evaluate('(tile)=>window.tile=tile', self.nameless_video_tile())
        with self.assertRaisesRegex(GeminiError, '未在时限内确认'):
            self.agent.upload_files([self.video, other])

    def test_nameless_loading_tile_never_advances_even_without_spinner(self):
        self.cfg['gemini']['upload_timeout_seconds'] = .2
        self.fixture(self.media_input("document.querySelector('#attachments').innerHTML=window.tile") +
                     '<div id="attachments"></div>')
        self.page.evaluate('(tile)=>window.tile=tile', self.nameless_video_tile(loading=True))
        with self.assertRaisesRegex(GeminiError, '未在时限内确认'):
            self.agent.upload_files([self.video])

    def test_upload_error_stops_before_prompt_entry(self):
        self.fixture(self.media_input(
            "document.querySelector('#error').textContent='Upload failed'") + '<div id="error" role="alert"></div>')
        with self.assertRaisesRegex(GeminiError, '上传报错'):
            self.agent.upload_files([self.video])
        self.assertEqual(self.page.get_by_role('textbox').inner_text(), '')

    def test_file_input_selection_alone_is_not_upload_success(self):
        self.cfg['gemini']['upload_timeout_seconds'] = .2
        self.fixture(self.media_input('void 0'))
        with self.assertRaisesRegex(GeminiError, '未在时限内确认'):
            self.agent.upload_files([self.video])

    def test_history_or_draft_filename_is_not_attachment_proof(self):
        self.cfg['gemini']['upload_timeout_seconds'] = .2
        self.fixture(self.media_input('void 0'))
        self.page.get_by_role('textbox').fill(self.video.name)
        self.page.evaluate('(name)=>{const e=document.createElement("user-query");e.textContent=name;document.body.prepend(e)}', self.video.name)
        with self.assertRaisesRegex(GeminiError, '未在时限内确认'):
            self.agent.upload_files([self.video])

    def test_incompatible_file_input_is_not_used(self):
        self.cfg['gemini']['upload_control_timeout_seconds'] = .2
        self.fixture('<input type="file" accept=".pdf,.txt">')
        with self.assertRaisesRegex(GeminiError, '无法找到'):
            self.agent.upload_files([self.video])
        self.assertEqual(self.page.locator('input').evaluate('(e)=>e.files.length'), 0)

    def test_missing_file_is_rejected_before_touching_upload_ui(self):
        self.fixture()
        with self.assertRaisesRegex(GeminiError, '文件不存在'):
            self.agent.upload_files([Path(self.temp.name) / 'missing.mp4'])

    def manual_submission_fixture(self, attachment_count):
        self.fixture(self.media_input("setTimeout(()=>window.manualSubmit(),100)") +
            '<div id="attachments"></div><button aria-label="发送" '
            'onclick="window.autoSendCount=(window.autoSendCount||0)+1">Send</button>',
            '''window.manualSubmit=()=>{
              const editor=document.querySelector('[contenteditable]');
              const turn=document.createElement('user-query');
              const query=document.createElement('div');query.className='query-text';
              const label=document.createElement('h5');label.className='screen-reader-user-query-label';
              label.textContent='你说 shortened prompt';query.append(label);
              const line=document.createElement('p');line.className='query-text-line';
              line.textContent=editor.innerText;query.append(line);turn.append(query);
              for(let i=0;i<''' + str(attachment_count) + ''';i++){
                const preview=document.createElement('user-query-file-preview');
                preview.textContent='0:15';turn.append(preview);
              }
              editor.innerText='';document.querySelector('#attachments').replaceChildren();
              document.body.append(turn);
              const answer=document.createElement('model-response');
              answer.style.whiteSpace='pre-wrap';answer.style.display='block';
              answer.textContent=window.fixtureReply;document.body.append(answer);
            };''')
        self.page.evaluate('(text)=>window.fixtureReply=text', MANUAL_SCRIPT)

    def test_manual_send_during_upload_uses_real_class_selector_and_never_resends(self):
        self.manual_submission_fixture(1)
        self.agent.open=lambda:None
        self.cfg['gemini']['response_stable_seconds']=0
        self.assertEqual(self.agent.generate([self.video], 'fixture instruction'), MANUAL_SCRIPT)
        self.assertEqual(self.page.locator('user-query').count(),1)
        self.assertFalse(self.page.evaluate('Boolean(window.autoSendCount)'))
        self.assertTrue(self.agent._external_submission_accepted)

    def test_matching_manual_prompt_missing_attachment_stops_without_resend(self):
        self.manual_submission_fixture(0)
        self.agent.open=lambda:None
        with self.assertRaisesRegex(GeminiSubmissionUncertain,'全部附件'):
            self.agent.generate([self.video], 'fixture instruction')
        self.assertEqual(self.page.locator('user-query').count(),1)
        self.assertFalse(self.page.evaluate('Boolean(window.autoSendCount)'))

    def test_current_chinese_send_label_and_visible_editor_are_used_once(self):
        self.fixture('<button aria-label="发送" onclick="this.dataset.clicks=Number(this.dataset.clicks||0)+1;'
                     'document.body.insertAdjacentHTML(\'beforeend\',\'<user-query>new request</user-query>\')"></button>')
        self.page.evaluate('()=>{const e=document.createElement("textarea");e.hidden=true;document.body.prepend(e)}')
        self.agent.send_prompt('synthetic local prompt')
        self.assertEqual(self.page.get_by_role('textbox').inner_text(), 'synthetic local prompt')
        self.assertEqual(self.page.get_by_role('button', name='发送').get_attribute('data-clicks'), '1')

    def test_missing_send_button_does_not_fall_back_to_enter(self):
        self.fixture(script='document.addEventListener("keydown",e=>{if(e.key==="Enter")window.enterPressed=true})')
        with self.assertRaisesRegex(GeminiError, '未就绪'):
            self.agent.send_prompt('synthetic local prompt')
        self.assertFalse(self.page.evaluate('Boolean(window.enterPressed)'))
        self.assertFalse(self.agent._submission_attempted)


if __name__ == '__main__':
    unittest.main()
