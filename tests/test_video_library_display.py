"""Video library presentation uses disposable files and state only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard


class VideoLibraryDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='video-library-display-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.videos = self.root / 'videos'
        self.videos.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.patches = [patch.object(dashboard, 'VIDEOS_DIR', self.videos),
                        patch.object(dashboard, 'STATE_DIR', self.state),
                        patch.object(dashboard, 'readonly_postprocess_snapshots', return_value={})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = dashboard.app.test_client()

    def save(self, value):
        (self.state / 'pipeline_state.json').write_text(json.dumps(value))

    def video(self, name):
        path = self.videos / name
        path.write_bytes(b'disposable fixture, not playable video')
        return path

    def listing(self):
        response = self.client.get('/api/videos', headers={'Host': 'localhost'})
        self.assertEqual(response.status_code, 200)
        return response.json['videos']

    def test_finished_clip_hides_only_its_raw_copy_and_preserves_both_files(self):
        raw = self.video('current_seg0.raw.mp4')
        final = self.video('current_seg0.mp4')
        pending = self.video('pending_seg0.raw.mp4')
        result = self.listing()
        self.assertEqual({v['name'] for v in result}, {final.name, pending.name})
        self.assertTrue(raw.is_file())
        self.assertEqual(raw.read_bytes(), b'disposable fixture, not playable video')

    def test_current_clips_have_script_and_segment_labels_without_renaming(self):
        first = self.video('long_run_script_one_seg0.mp4')
        second = self.video('long_run_script_two_seg1.raw.mp4')
        self.save({'scripts': [{'id': 'long_run_script_one', 'keyword': 'OWALA保温杯'},
                               {'id': 'long_run_script_two', 'keyword': 'OWALA保温杯'}],
                   'tasks': [{'script_id': 'long_run_script_one', 'seg_index': 0,
                              'seg_total': 1, 'video': str(first)},
                             {'script_id': 'long_run_script_two', 'seg_index': 1,
                              'seg_total': 3}]})
        result = {v['name']: v for v in self.listing()}
        self.assertEqual(result[first.name]['label'], 'OWALA保温杯 · 脚本1 · 片段1/1')
        self.assertEqual(result[second.name]['label'], 'OWALA保温杯 · 脚本2 · 片段2/3（原始下载）')
        self.assertEqual(result[second.name]['path'], str(second.resolve()))
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())

    def test_unmapped_history_keeps_its_actual_filename(self):
        old = self.video('历史成片.mp4')
        self.save({'scripts': [], 'tasks': []})
        result = self.listing()
        self.assertEqual(result[0]['label'], old.name)
        self.assertEqual(result[0]['name'], old.name)


if __name__ == '__main__':
    unittest.main()
