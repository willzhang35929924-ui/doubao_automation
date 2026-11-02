"""Deterministic duration planning; pure text tests with no remote generation."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doubao_automation.script_splitter import (  # noqa: E402
    build_segments, detect_total_seconds, extract_narration, parse_scripts,
)


CFG = {'video': {'chars_per_second': 4.5, 'target_segment_seconds': 13}}


def timestamp(seconds):
    return f'{seconds // 60:02d}:{seconds % 60:02d}'


def block(start, end, speech, visual='产品沿桌面缓慢移动。'):
    return (f'{timestamp(start)} - {timestamp(end)}｜摄影与构图：中景。\n'
            f'视觉与动作：{visual}\n音频与口播：{speech}\n')


class DurationPlanTests(unittest.TestCase):
    def assert_plan(self, segments, durations):
        self.assertEqual([segment['est_seconds'] for segment in segments], durations)
        self.assertEqual([segment['index'] for segment in segments], list(range(len(durations))))
        self.assertTrue(all(segment['total'] == len(durations) for segment in segments))
        cursor = 0
        for segment, seconds in zip(segments, durations):
            self.assertEqual(segment['start_seconds'], cursor)
            cursor += seconds
            self.assertEqual(segment['end_seconds'], cursor)
            self.assertLessEqual(seconds, 15)
        for previous, current in zip(segments, segments[1:]):
            self.assertEqual(current['start_frame_desc'], previous['end_frame_desc'])

    def test_titles_preserve_fifty_and_seventy_as_body_metadata(self):
        scripts = parse_scripts('### 脚本1：五十秒广告（50秒）\n口播文案：甲乙丙丁。\n'
                                'Script 02: The second ad (70 seconds)\n口播文案：戊己庚辛。')
        self.assertEqual(len(scripts), 2)
        self.assertTrue(all(isinstance(script, str) for script in scripts))
        self.assertEqual([detect_total_seconds(script) for script in scripts], [50, 70])
        self.assertTrue(all('【脚本总时长：' in script for script in scripts))
        self.assertNotIn('### 脚本', scripts[0])
        self.assertEqual([extract_narration(script) for script in scripts], ['甲乙丙丁。', '戊己庚辛。'])

    def test_bold_titles_with_duration_outside_bold_keep_metadata(self):
        scripts = parse_scripts('**脚本1**（50秒）\n口播：一二三四。\n'
                                '**Script 02** (70s)\n口播：五六七八。')
        self.assertEqual(len(scripts), 2)
        self.assertEqual([detect_total_seconds(script) for script in scripts], [50, 70])

    def test_single_fifty_second_block_is_four_nonoverlapping_windows(self):
        speech = ''.join(chr(0x4E00 + index) for index in range(100)) + '。'
        script = '总时长：50秒\n' + block(0, 50, speech)
        segments = build_segments(script, CFG)
        self.assert_plan(segments, [15, 15, 15, 5])
        self.assertEqual(''.join(segment['narration'] for segment in segments), speech)
        self.assertTrue(all(speech not in segment['prompt'] for segment in segments))
        self.assertTrue(all('00:00 - 00:50' not in segment['prompt'] for segment in segments))

    def test_no_timestamp_seventy_second_script_preserves_all_words(self):
        speech = ' '.join('word' + str(index) for index in range(70)) + '.'
        script = '总时长：70秒\n口播文案：' + speech + '\n画面风格：电商广告。'
        segments = build_segments(script, CFG)
        self.assert_plan(segments, [15, 15, 15, 15, 10])
        self.assertEqual(''.join(segment['narration'] for segment in segments), speech)
        self.assertEqual(''.join(segment['narration'] for segment in segments).split(), speech.split())
        self.assertTrue(all(speech not in segment['prompt'] for segment in segments))

    def test_unlabelled_narration_does_not_include_total_duration_metadata(self):
        speech = '这段没有单独口播标签，但所有原始文字都应保留而不重复。' * 8
        script = '总时长：70秒\n' + speech
        segments = build_segments(script, CFG)
        self.assert_plan(segments, [15, 15, 15, 15, 10])
        self.assertEqual(''.join(segment['narration'] for segment in segments), speech)
        self.assertTrue(all('总时长' not in segment['narration'] for segment in segments))

    def test_cross_window_spoken_sentence_is_neither_repeated_nor_dropped(self):
        speech_a = '开场只说一次。'
        speech_b = ''.join(chr(0x5000 + index) for index in range(45)) + '。'
        speech_c = ''.join(chr(0x5200 + index) for index in range(75)) + '。'
        script = block(0, 10, speech_a) + block(10, 25, speech_b) + block(25, 50, speech_c)
        segments = build_segments(script, CFG)
        self.assert_plan(segments, [15, 15, 15, 5])
        self.assertEqual(''.join(segment['narration'] for segment in segments), speech_a + speech_b + speech_c)
        self.assertNotIn(speech_b, segments[0]['prompt'])
        self.assertNotIn(speech_b, segments[1]['prompt'])
        self.assertEqual(sum(segment['prompt'].count(speech_a) for segment in segments), 1)

    def test_five_second_blocks_land_on_exact_fifteen_second_boundaries(self):
        speeches = [f'第{index}个独立声音。' for index in range(10)]
        script = ''.join(block(index * 5, (index + 1) * 5, speech)
                         for index, speech in enumerate(speeches))
        segments = build_segments(script, CFG)
        self.assert_plan(segments, [15, 15, 15, 5])
        self.assertEqual([segment['narration'] for segment in segments],
                         [''.join(speeches[0:3]), ''.join(speeches[3:6]),
                          ''.join(speeches[6:9]), speeches[9]])
        self.assertIn('00:10 - 00:15', segments[0]['prompt'])
        self.assertNotIn('00:15 - 00:20', segments[0]['prompt'])
        self.assertIn('00:45 - 00:50', segments[-1]['prompt'])

    def test_single_seventy_second_timestamp_alone_establishes_duration(self):
        script = block(0, 70, '完整台词不会因为只有一个时间块而按语速重新估算。')
        self.assertEqual(detect_total_seconds(script), 70)
        self.assert_plan(build_segments(script, CFG), [15, 15, 15, 15, 10])

    def test_multiline_spoken_payload_is_retained_and_direction_is_excluded(self):
        script = ('总时长：70秒\n口播文案：First line stays complete.\n'
                  'Second line also stays complete.\n画面风格：桌面。')
        expected = 'First line stays complete.\nSecond line also stays complete.'
        segments = build_segments(script, CFG)
        self.assertEqual(extract_narration(script), expected)
        self.assertEqual(''.join(segment['narration'] for segment in segments), expected)
        self.assertNotIn('画面风格', ''.join(segment['narration'] for segment in segments))

    def test_spoken_paragraph_is_not_repeated_as_product_preamble(self):
        expected = '第一段口播。\n\n产品表现是下一段口播内容，不能被当成每段公共说明。'
        script = '总时长：70秒\n口播文案：' + expected + '\n画面风格：桌面。'
        segments = build_segments(script, CFG)
        self.assertEqual(''.join(segment['narration'] for segment in segments), expected)
        self.assertTrue(all(expected.split('\n\n')[1] not in segment['prompt'] for segment in segments))

    def test_delivery_cue_and_outer_quotes_are_not_spoken(self):
        script = block(0, 50, '(温柔) “这句话应只出现一次。”')
        self.assertEqual(extract_narration(script), '这句话应只出现一次。')
        self.assertEqual(''.join(segment['narration'] for segment in build_segments(script, CFG)),
                         '这句话应只出现一次。')

    def test_overlapping_or_out_of_declared_duration_blocks_are_rejected(self):
        with self.assertRaises(ValueError):
            build_segments(block(0, 20, '甲。') + block(15, 30, '乙。'), CFG)
        with self.assertRaises(ValueError):
            build_segments('总时长：50秒\n' + block(0, 70, '甲。'), CFG)

    def test_incidental_duration_in_spoken_prose_is_not_a_plan(self):
        self.assertIsNone(detect_total_seconds('产品保温70秒只是台词的一部分，不能据此改变脚本时长。'))

    def test_timestamp_gaps_and_missing_tail_are_rejected_before_generation(self):
        with self.assertRaises(ValueError):
            build_segments('总时长：50秒\n' + block(0, 15, '甲。') + block(20, 50, '乙。'), CFG)
        with self.assertRaises(ValueError):
            build_segments('总时长：50秒\n' + block(0, 45, '甲。'), CFG)

    def test_untimed_legacy_narration_stays_estimated_and_has_continuous_offsets(self):
        script = '口播文案：' + '这是一段保留原有口播切分方式的产品介绍。' * 15
        self.assertIsNone(detect_total_seconds(script))
        segments = build_segments(script, CFG)
        self.assertGreater(len(segments), 1)
        self.assertTrue(all(segment['est_seconds'] <= 13.5 for segment in segments))
        self.assertEqual(segments[0]['start_seconds'], 0)
        for previous, current in zip(segments, segments[1:]):
            self.assertEqual(previous['end_seconds'], current['start_seconds'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
