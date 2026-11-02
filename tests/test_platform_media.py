"""Offline tests for media discovery and postprocessing configuration."""
import io
import json
import unittest
from unittest.mock import Mock, patch

from doubao_automation import postprocess, video_utils


class MediaDiscoveryTests(unittest.TestCase):
    def test_system_path_is_the_only_media_source(self):
        with patch.object(video_utils.shutil, 'which', side_effect=lambda x: '/tools/'+x):
            self.assertEqual(video_utils.find_ffmpeg('old-local-path'), '/tools/ffmpeg')
            self.assertEqual(video_utils.find_ffprobe(), '/tools/ffprobe')
        with patch.object(video_utils.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'PATH'):
                video_utils.find_ffmpeg()
            with self.assertRaisesRegex(RuntimeError, 'PATH'):
                video_utils.find_ffprobe()

    def test_comfy_environment_override_retargets_its_queue(self):
        config = {'endpoint': {'preferred': 'http://old:8188', 'queue_check_urls': [
            'http://old:8188/queue', 'http://other:8188/queue']}, 'prompt_template': {}}
        fake = Mock()
        fake.is_file.return_value = True
        fake.read_text.return_value = json.dumps(config)
        with patch.object(postprocess, 'SETUP', fake), patch.dict(postprocess.os.environ,
                {'DOUBAO_COMFY_URL': 'http://localhost:8188/'}, clear=False):
            actual = postprocess._read_setup()
        self.assertEqual(actual['endpoint']['preferred'], 'http://localhost:8188')
        self.assertEqual(actual['endpoint']['queue_check_urls'],
                         ['http://localhost:8188/queue', 'http://other:8188/queue'])

    def test_unvalidated_vsr_path_contract_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'PATH-only'):
            postprocess._assert_vsr_path_contract()


if __name__ == '__main__':
    unittest.main()