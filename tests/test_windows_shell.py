"""桌面请求必须收到对应确认，不能把发出请求当作打开成功。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from doubao_automation import windows_shell


@unittest.skipUnless(os.name == 'nt', 'requires Windows desktop bridge')
class DesktopRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.tmp')
        self.path=Path(self.tmp.name)
        self.videos=self.path/'videos';self.videos.mkdir()
        self.video=self.videos/"中文 视频's.mp4";self.video.write_bytes(b'video')
        self.status=self.path/'desktop_bridge_status.json';self.status.write_text('{}')
        self.patchers=[patch.object(windows_shell,'TEMP_DIR',self.path),patch.object(windows_shell,'VIDEOS_DIR',self.videos)]
        for p in self.patchers:p.start()

    def tearDown(self):
        for p in self.patchers:p.stop()
        self.tmp.cleanup()

    def respond(self,ok):
        def worker():
            request=self.path/'desktop_folder_request.json';deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                if request.exists():
                    data=json.loads(request.read_text('utf-8'))
                    (self.path/'desktop_folder_response.json').write_text(json.dumps({'id':data['id'],'ok':ok,'error':'Windows denied access'}),encoding='utf-8')
                    return
                time.sleep(.01)
        thread=threading.Thread(target=worker);thread.start();return thread

    def test_matching_window_confirmation_succeeds(self):
        thread=self.respond(True)
        windows_shell.open_video_folder(self.video)
        thread.join(timeout=3);self.assertFalse(thread.is_alive())

    def test_desktop_failure_is_reported(self):
        thread=self.respond(False)
        with self.assertRaisesRegex(OSError,'Windows denied access'):
            windows_shell.open_video_folder(self.video)
        thread.join(timeout=3)

    def test_stale_helper_is_rejected(self):
        old=time.time()-20;os.utime(self.status,(old,old))
        with self.assertRaisesRegex(OSError,'桌面助手未运行'):
            windows_shell.open_video_folder(self.video)
        self.assertFalse((self.path/'desktop_folder_request.json').exists())

    def test_missing_confirmation_never_succeeds(self):
        with patch.object(windows_shell.time,'monotonic',side_effect=[0,11]):
            with self.assertRaisesRegex(OSError,'响应超时'):
                windows_shell.open_video_folder(self.video)


if __name__=='__main__':unittest.main(verbosity=2)
