"""Real, tiny FFmpeg fixtures: exact clip windows and ordered 50/70s assembly."""
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from doubao_automation.video_utils import find_ffmpeg, trim_video, concat_videos, _probe_video


class VideoTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        cls.directory = tempfile.TemporaryDirectory(prefix='timeline_tests_', dir=ROOT / '.tmp')
        cls.work = Path(cls.directory.name)
        cls.ffmpeg = find_ffmpeg()
        cls.sources = []
        for index, color in enumerate(('red', 'lime', 'blue', 'yellow')):
            path = cls.work / f'raw_{index}.mp4'
            cls.make_video(path, color, 15.05, frames=361)
            cls.sources.append(path)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    @classmethod
    def run_ffmpeg(cls, arguments):
        result = subprocess.run([cls.ffmpeg, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error']
                                + [str(a) for a in arguments], capture_output=True,
                                creationflags=0x08000000 if sys.platform == 'win32' else 0)
        if result.returncode:
            raise AssertionError(result.stderr.decode('utf-8', errors='replace')[-2000:])
        return result.stdout

    @classmethod
    def make_video(cls, path, color, duration, frames=None, audio=True, codec='libx264'):
        args = ['-f', 'lavfi', '-i', f'color=c={color}:s=96x64:r=24:d={duration}']
        if audio:
            args += ['-f', 'lavfi', '-i', f'sine=frequency=440:sample_rate=48000:duration={duration}',
                     '-c:a', 'aac']
        args += ['-c:v', codec, '-pix_fmt', 'yuv420p', '-threads', '2']
        if codec == 'libx264':
            args += ['-preset', 'ultrafast', '-bf', '0']
        if frames is not None:
            args += ['-frames:v', str(frames)]
        args += ['-video_track_timescale', '12288', path]
        cls.run_ffmpeg(args)

    def test_five_second_window_keeps_raw_and_exact_frames(self):
        raw = self.sources[0]
        digest = hashlib.sha256(raw.read_bytes()).hexdigest()
        output = self.work / 'five_seconds.mp4'
        trim_video(raw, output, 5, self.ffmpeg)
        info = _probe_video(output, self.ffmpeg)
        self.assertEqual(info['frames'], 120)
        self.assertAlmostEqual(info['video_seconds'], 5, places=6)
        self.assertLessEqual(abs(info['duration'] - 5), 1 / 24 + .002)
        self.assertTrue(info['audio'])
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), digest)

    def test_short_source_fails_without_padding_or_overwriting(self):
        output = self.work / 'preserve_previous.mp4'
        output.write_bytes(b'previous output')
        with self.assertRaisesRegex(RuntimeError, '不足'):
            trim_video(self.sources[0], output, 20, self.ffmpeg)
        self.assertEqual(output.read_bytes(), b'previous output')

    def test_variable_frame_rate_is_rejected_instead_of_changing_speed(self):
        base, variable = self.work / 'vfr_base.mp4', self.work / 'vfr_source.mp4'
        self.make_video(base, 'red', 2, frames=48, audio=False)
        self.run_ffmpeg(['-i', base, '-vf',
                         'setpts=if(lt(N\\,24)\\,N/(24*TB)\\,(1+(N-24)/12)/TB)',
                         '-fps_mode', 'vfr', '-c:v', 'libx264', variable])
        with self.assertRaisesRegex(RuntimeError, '变帧率'):
            trim_video(variable, self.work / 'vfr_trim.mp4', 2, self.ffmpeg)

    def test_fifty_and_seventy_second_timelines(self):
        for total, windows in ((50, [15, 15, 15, 5]), (70, [15, 15, 15, 15, 10])):
            clips = []
            for index, duration in enumerate(windows):
                output = self.work / f'{total}_seg{index}.mp4'
                trim_video(self.sources[index % 4], output, duration, self.ffmpeg)
                clips.append(output)
            final = self.work / f'{total}_final.mp4'
            concat_videos(clips, final, self.ffmpeg, target_seconds=total)
            info = _probe_video(final, self.ffmpeg)
            self.assertEqual(info['frames'], total * 24)
            self.assertEqual(info['fps'], 24)
            self.assertLessEqual(abs(info['duration'] - total), 1 / 24 + .002)
            self.assertTrue(info['audio'])
            # Distinct solid colors make wrong segment order visible to a pixel assertion.
            expected = ((250, 0, 0), (0, 250, 0), (0, 0, 250), (250, 250, 0))
            for index in range(len(windows)):
                at = sum(windows[:index]) + .5
                pixels = self.run_ffmpeg(['-ss', str(at), '-i', final, '-frames:v', '1',
                                          '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'])
                center = (32 * 96 + 48) * 3
                actual = pixels[center:center + 3]
                self.assertEqual(len(actual), 3)
                for a, e in zip(actual, expected[index % 4]):
                    self.assertLess(abs(a - e), 12)

    def test_mixed_codec_and_missing_audio_reencode_without_losing_frames(self):
        first, second = self.work / 'mixed_first.mp4', self.work / 'mixed_second.mp4'
        self.make_video(first, 'red', 2, frames=48, audio=True)
        self.make_video(second, 'blue', 2, frames=48, audio=False, codec='mpeg4')
        final = self.work / 'mixed_final.mp4'
        concat_videos([first, second], final, self.ffmpeg, target_seconds=4)
        info = _probe_video(final, self.ffmpeg)
        self.assertEqual(info['frames'], 96)
        self.assertTrue(info['audio'])
        self.assertLessEqual(abs(info['duration'] - 4), 1 / 24 + .002)

    def test_first_container_four_ticks_short_does_not_shift_following_clip(self):
        base, shortened = self.work / 'tick_base.mp4', self.work / 'tick_short.mp4'
        next_clip, final = self.work / 'tick_next.mp4', self.work / 'tick_final.mp4'
        self.make_video(base, 'red', 2, frames=48, audio=False)
        self.make_video(next_clip, 'blue', 2, frames=48, audio=False)
        self.run_ffmpeg(['-i', base, '-map', '0:v:0', '-c', 'copy', '-bsf:v',
                         r'setts=duration=if(eq(N\,47)\,508\,512)', shortened])
        concat_videos([shortened, next_clip], final, self.ffmpeg, target_seconds=4)
        info = _probe_video(final, self.ffmpeg)
        self.assertEqual(info['frames'], 96)
        self.assertEqual(info['fps'], 24)
        self.assertLessEqual(abs(info['video_seconds'] - 4), 1 / 24)

    def test_mixed_constant_frame_rates_preserve_all_frames_and_timing(self):
        first, second = self.work / 'rate24.mp4', self.work / 'rate30.mp4'
        self.make_video(first, 'red', 2, frames=48, audio=False)
        self.run_ffmpeg(['-f', 'lavfi', '-i', 'color=c=blue:s=96x64:r=30:d=2',
                         '-frames:v', '60', '-c:v', 'libx264', '-preset', 'ultrafast', second])
        output = self.work / 'mixed_rates.mp4'
        concat_videos([first, second], output, self.ffmpeg, target_seconds=4)
        info = _probe_video(output, self.ffmpeg, allow_vfr=True)
        self.assertEqual(info['frames'], 108)
        self.assertLessEqual(abs(info['video_seconds'] - 4), 1 / 24 + .002)


if __name__ == '__main__':
    unittest.main(verbosity=2)
