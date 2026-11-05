"""Real model timestamp headings retain strict continuity and duration checks."""
import unittest

from doubao_automation.script_splitter import build_segments, parse_timestamp_blocks


def script(prefix='', ranges=((0, 5), (5, 10), (10, 15))):
    return '总时长：15秒\n' + ''.join(
        f'{prefix}00:{start:02d}-00:{end:02d}\n'
        f'摄影与构图：产品近景。\n视觉与动作：展示动作{start}。\n'
        f'音频与口播：测试片段{start}。\n'
        for start, end in ranges)


class TimestampLabelTests(unittest.TestCase):
    def test_gemini_accepts_a_complete_script_with_labeled_timestamp_headings(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from doubao_automation.gemini_web import GeminiWeb

        reply='Script 01：本地测试（总时长15秒）\n'+script('【时间轴】 ')
        agent=GeminiWeb(SimpleNamespace(page=None),{})
        agent.open=Mock();agent.type_prompt=Mock();agent.send_prompt=Mock()
        agent.wait_response=Mock(return_value=reply);agent._capture_diagnostic=Mock()
        agent._response_snapshot=Mock(return_value=dict(model_count=0,user_count=0,
            user_texts=[],texts=[],response_hashes=[],selector=''))
        self.assertEqual(agent.generate([], '写一个15秒脚本'),reply)
        agent.send_prompt.assert_called_once()

    def test_labeled_and_markdown_timestamps_plan_the_full_fifteen_seconds(self):
        for prefix in ('【时间轴】 ', '【时间戳】 ', '[时间轴]: ', '**【时间轴】** ',
                       '### 【时间戳】 ', '【时间线】 ', '【时间段】 ', '[TIMELINE] '):
            with self.subTest(prefix=prefix):
                text=script(prefix)
                blocks=parse_timestamp_blocks(text)
                self.assertEqual([(b['start'], b['end']) for b in blocks], [(0,5),(5,10),(10,15)])
                segments=build_segments(text, {})
                self.assertEqual([s['est_seconds'] for s in segments], [15])
                self.assertEqual((segments[0]['start_seconds'],segments[0]['end_seconds']), (0,15))

    def test_bare_timestamps_retain_the_existing_plan(self):
        self.assertEqual([s['est_seconds'] for s in build_segments(script(), {})], [15])

    def test_reference_text_is_not_treated_as_a_timeline_heading(self):
        self.assertEqual(parse_timestamp_blocks('参考视频【时间轴】00:00-00:05\n一般说明。'), [])

    def test_labeled_timestamps_still_reject_gaps_overlaps_and_invalid_seconds(self):
        for ranges in (((0,5),(6,10),(10,15)), ((0,6),(5,10),(10,15)), ((0,5),(5,10),(10,60))):
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                build_segments(script('【时间轴】 ',ranges), {})


if __name__ == '__main__':
    unittest.main()
