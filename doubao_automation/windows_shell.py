"""将固定视频目录的打开请求交给常驻桌面助手，并等待实际窗口确认。"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from .paths import TEMP_DIR, VIDEOS_DIR

_lock = threading.Lock()


def open_video_folder(path):
    path = Path(path).resolve()
    if not path.is_file() or VIDEOS_DIR.resolve() not in path.parents:
        raise FileNotFoundError(f'视频文件不存在或不在下载目录：{path}')
    if sys.platform == 'darwin':
        try:
            result = subprocess.run(['/usr/bin/open', '-R', str(path)],
                                    capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OSError('无法请求 Finder 显示文件，请检查系统权限') from exc
        if result.returncode:
            raise OSError('Finder 打开请求失败：' + (result.stderr.strip() or str(result.returncode)))
        return
    if os.name != 'nt':
        raise OSError('当前平台暂不支持打开本地文件夹')
    status = TEMP_DIR / 'desktop_bridge_status.json'
    if not status.exists() or time.time() - status.stat().st_mtime > 8:
        raise OSError('桌面助手未运行，请重新双击 start_dashboard.bat；已有后台会保持运行')
    with _lock:
        request_id = uuid.uuid4().hex
        request = TEMP_DIR / 'desktop_folder_request.json'
        tmp = request.with_suffix('.json.tmp')
        tmp.write_text(json.dumps({'id':request_id}),encoding='utf-8')
        os.replace(tmp,request)
        response = TEMP_DIR / 'desktop_folder_response.json'
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                data = json.loads(response.read_text(encoding='utf-8-sig'))
                if data.get('id') == request_id:
                    if not data.get('ok'):
                        raise OSError('Windows 未能打开目录：' + data.get('error','未知错误'))
                    return
            except (FileNotFoundError,json.JSONDecodeError):
                pass
            time.sleep(.1)
        raise OSError('桌面助手响应超时，请检查 Windows 提示或重新打开启动脚本')
