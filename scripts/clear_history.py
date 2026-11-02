"""只读历史清理预览；当前任务账本不支持安全删除。"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from doubao_automation.paths import STATE_DIR

TERMINAL = frozenset(('done', 'failed', 'cancelled'))


def preview_history():
    """读取当前流水线账本，保守列出候选；不创建文件、目录或锁。"""
    path = STATE_DIR / 'pipeline_state.json'
    try:
        with path.open(encoding='utf-8') as handle:
            state = json.load(handle)
    except FileNotFoundError:
        state = {}
    if not isinstance(state, dict):
        raise ValueError('任务状态格式无效，已停止预览')
    tasks = state.get('tasks', [])
    assemblies = state.get('assemblies', {})
    if not isinstance(tasks, list) or not isinstance(assemblies, dict):
        raise ValueError('任务状态格式无效，已停止预览')

    candidates, active, paths = [], 0, set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError('任务记录格式无效，已停止预览')
        if task.get('status') in TERMINAL:
            candidates.append(str(task.get('id', '未知任务')))
        else:
            active += 1
        for key in ('video', 'raw_video'):
            value = task.get(key)
            if isinstance(value, str) and value:
                paths.add(value)
    for assembly in assemblies.values():
        if isinstance(assembly, dict):
            value = assembly.get('output')
            if isinstance(value, str) and value:
                paths.add(value)

    reason = ('当前账本中的分段、拼接、远端请求和账号额度可能互相引用，'
              '尚不能安全核实全部恢复依赖，因此暂不执行删除。')
    return {
        'supported': False,
        'reason': reason,
        'delete_records': 0,
        'delete_files': 0,
        'estimated_bytes': 0,
        'directories': [],
        'candidate_records': len(candidates),
        'active_or_uncertain_records': active,
        'skipped_file_count': len(paths),
        'skipped_files': [{'path': path, 'reason': '未能安全确认全部引用'} for path in sorted(paths)],
        'note': '仅统计账本中明确记录的文件路径；没有扫描磁盘或更改状态。',
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='历史清理预览（删除功能尚不可用）')
    parser.add_argument('action', choices=('preview', 'clear'))
    args = parser.parse_args(argv)
    try:
        result = preview_history()
    except (OSError, ValueError) as exc:
        print(json.dumps({'error': str(exc), 'deleted': False}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.action == 'clear':
        print('未删除任何记录或文件：历史清理暂不可用。')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
