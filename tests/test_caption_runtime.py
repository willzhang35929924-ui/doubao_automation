import contextlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doubao_automation import caption_runtime as runtime


class TestCaptionSetup(unittest.TestCase):
    def test_incomplete_install_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            media = root / "local.jpg"
            media.write_bytes(b"image")
            with (
                patch.object(runtime, "_RUNTIME_DIR", root),
                patch.object(runtime.sys, "platform", "darwin"),
                patch.object(runtime.platform, "machine", return_value="arm64"),
                patch.object(runtime.platform, "mac_ver", return_value=("26.0", (), "")),
            ):
                with self.assertRaisesRegex(runtime.CaptionRuntimeError, "scripts/setup_caption_runtime.sh"):
                    runtime.generate_caption([media], "images", "product")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires local FFmpeg tools")
class TestCaptionVideoFrames(unittest.TestCase):
    def setUp(self):
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(contextlib.chdir(self.scratch))

    def create_video(self, name, duration, codec):
        path = self.scratch / name
        subprocess.run([
            self.ffmpeg, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=160x90:r=25", "-t", duration, "-c:v", codec, str(path),
        ], check=True, timeout=10)
        return path

    def test_samples_first_frame_and_caps_longer_clip_at_six(self):
        for duration, count in (("0.04", 1), ("2", 6)):
            with self.subTest(duration=duration):
                video = self.create_video(f"clip-{count}.mp4", duration, "mpeg4")
                frames = runtime._video_frames(video, self.ffmpeg, self.ffprobe)
                self.assertEqual(len(frames), count)
                for frame in frames:
                    self.assertTrue(frame.read_bytes().startswith(b"\x89PNG"))
                    frame.unlink()

    def test_rejects_playlist_even_with_local_segments(self):
        self.create_video("segment.ts", "1", "mpeg2video")
        playlist = self.scratch / "input.m3u8"
        playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n")
        with self.assertRaisesRegex(runtime.CaptionRuntimeError, "无法从该视频抽帧"):
            runtime._video_frames(playlist, self.ffmpeg, self.ffprobe)
