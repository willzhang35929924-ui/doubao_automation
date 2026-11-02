"""ffmpeg 工具：定位可执行文件、提取尾帧、测时长、拼接分段视频。"""
import re
import shutil
import subprocess
import sys
import json
import math
import os
import tempfile
import uuid
from fractions import Fraction
from pathlib import Path

_FFMPEG_CACHED = None
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def find_ffmpeg(cfg_value: str = "auto") -> str:
    """Find the system executable on PATH on Windows, macOS and Linux."""
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("找不到 ffmpeg：请安装 FFmpeg 并将其加入 PATH（macOS 可用 brew install ffmpeg）")
    return executable


def find_ffprobe() -> str:
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("找不到 ffprobe：请安装包含 ffprobe 的 FFmpeg 并将其加入 PATH")
    return executable

def _run(args: list) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=1800,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('本地视频处理超过30分钟，已停止本次处理子进程') from exc


def probe_duration(video: Path, ffmpeg: str = None) -> float:
    """用 ffmpeg -i 的 stderr 解析时长（秒）；失败返回 0。"""
    ffmpeg = ffmpeg or find_ffmpeg()
    r = _run([ffmpeg, "-hide_banner", "-i", str(video)])
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
    if not m:
        return 0.0
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def extract_last_frame(video: Path, out_png: Path, ffmpeg: str = None,
                       offset_seconds: float = 0.3) -> Path:
    """提取视频最后附近的 1 帧作为 PNG。
    优先 -sseof（从文件尾回退），失败则用 总时长-offset 精确定位。"""
    ffmpeg = ffmpeg or find_ffmpeg()
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    r = _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
              "-sseof", f"-{max(offset_seconds, 0.1)}", "-i", str(video),
              "-frames:v", "1", "-update", "1", str(out_png)])
    if r.returncode == 0 and out_png.exists() and out_png.stat().st_size > 0:
        return out_png
    dur = probe_duration(video, ffmpeg)
    if dur > 0:
        pos = max(dur - max(offset_seconds, 0.1), 0)
        r = _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                  "-ss", f"{pos:.3f}", "-i", str(video),
                  "-frames:v", "1", "-update", "1", str(out_png)])
        if r.returncode == 0 and out_png.exists():
            return out_png
    raise RuntimeError(f"提取尾帧失败: {video}\n{r.stderr}")


def _assert_cfr(points,fps):
    if len(points)<3:return
    step=1/float(fps)
    differences=[b-a for a,b in zip(points,points[1:])]
    if (any(d<=0 or abs(d-step)>max(.002,step*.05) for d in differences)
            or abs((points[-1]-points[0])-(len(points)-1)*step)>step*.5):
        raise RuntimeError('检测到变帧率或异常时间戳；不能按标称帧率裁剪以免改变播放速度')


def _probe_video(video, ffmpeg, allow_vfr=False):
    """Decode/count frames; use frame cadence rather than rounded container duration."""
    video=Path(video)
    if not video.is_file() or not video.stat().st_size:
        raise RuntimeError(f'视频文件不存在或为空: {video.name}')
    ffprobe=find_ffprobe()
    result=_run([ffprobe,'-v','error','-count_frames','-show_streams','-show_format',
                 '-show_frames','-of','json',str(video)])
    if result.returncode:raise RuntimeError('读取视频失败: '+result.stderr[-500:])
    payload=json.loads(result.stdout)
    stream=next((s for s in payload.get('streams',[]) if s.get('codec_type')=='video'),None)
    if not stream:raise RuntimeError('文件没有视频流')
    rate=stream.get('r_frame_rate') or stream.get('avg_frame_rate') or '0'
    fps=Fraction(rate)
    frames=int(stream.get('nb_read_frames') or stream.get('nb_frames') or 0)
    audio=next((s for s in payload.get('streams',[]) if s.get('codec_type')=='audio'),None)
    duration=float(payload.get('format',{}).get('duration') or stream.get('duration') or 0)
    codec=stream.get('codec_name','');width=int(stream['width']);height=int(stream['height'])
    audio_codec=audio.get('codec_name','') if audio else ''
    video_seconds=float(stream.get('duration') or 0)
    timestamps=[float(f['best_effort_timestamp_time']) for f in payload.get('frames',[])
                if f.get('media_type')=='video' and 'best_effort_timestamp_time' in f]
    if fps<=0 or frames<=0:raise RuntimeError('视频帧数或帧率无效')
    if not allow_vfr:_assert_cfr(timestamps,fps)
    if allow_vfr and video_seconds>0:
        fps=(Fraction(frames,1)/Fraction(str(video_seconds))).limit_denominator(1000000)
    else:
        video_seconds=float(Fraction(frames,1)/fps)
    return dict(fps=fps,frames=frames,width=width,height=height,codec=codec,
                audio=bool(audio),audio_codec=audio_codec,duration=duration,
                video_seconds=video_seconds)


def _check_timeline(path, ffmpeg, target, expected_frames=None, expected_fps=None, require_audio=False,allow_vfr=False):
    info=_probe_video(path,ffmpeg,allow_vfr=allow_vfr)
    tolerance=max(1/float(info['fps']),1/float(expected_fps or info['fps']))+0.000001
    if expected_frames is not None and info['frames']!=expected_frames:
        raise RuntimeError(f"视频帧数异常：实际{info['frames']}，预期{expected_frames}")
    if expected_fps is not None and info['fps']!=expected_fps:
        raise RuntimeError('视频帧率发生意外变化')
    if abs(info['video_seconds']-float(target))>tolerance or abs(info['duration']-float(target))>tolerance:
        raise RuntimeError(f"视频时长不符：实际{info['video_seconds']:.6f}s，目标{float(target):.6f}s")
    if require_audio and not info['audio']:raise RuntimeError('视频缺少原有音轨')
    return info


def trim_video(video, out_mp4, seconds, ffmpeg=None):
    """Keep raw input; publish a verified clip no longer than its planned frame window."""
    ffmpeg=ffmpeg or find_ffmpeg()
    video,out_mp4=Path(video),Path(out_mp4)
    seconds=float(seconds)
    if not math.isfinite(seconds) or seconds<=0:raise ValueError('裁剪目标秒数必须大于0')
    if video.resolve()==out_mp4.resolve():raise ValueError('原始下载与裁剪输出必须使用不同路径')
    source=_probe_video(video,ffmpeg);fps=source['fps'];frame_seconds=1/float(fps)
    if source['video_seconds']+frame_seconds+0.000001<seconds:
        raise RuntimeError(f"源视频不足目标时长：{source['video_seconds']:.6f}s < {seconds:.6f}s；不补帧")
    wanted=max(1,math.floor(seconds*float(fps)+0.000001))
    frames=min(wanted,source['frames'])
    out_mp4.parent.mkdir(parents=True,exist_ok=True)
    temp=out_mp4.with_name(out_mp4.stem+'.rendering_'+uuid.uuid4().hex[:10]+'.mp4')
    args=[ffmpeg,'-y','-nostdin','-hide_banner','-loglevel','error','-i',str(video),
          '-map','0:v:0','-map','0:a?','-map_metadata','0',
          '-vf',f'trim=end_frame={frames},setpts=N/({fps}*TB)',
          '-frames:v',str(frames),'-r',str(fps),'-fps_mode','cfr','-t',f'{seconds:.9f}',
          '-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p',
          '-video_track_timescale','90000']
    if source['audio']:
        args+=['-af',f'atrim=duration={seconds:.9f},asetpts=PTS-STARTPTS',
               '-c:a','aac','-b:a','192k']
    args+=['-movflags','+faststart+use_metadata_tags',str(temp)]
    try:
        result=_run(args)
        if result.returncode:raise RuntimeError('裁剪视频失败: '+result.stderr[-600:])
        _check_timeline(temp,ffmpeg,seconds,frames,fps,source['audio'])
        os.replace(temp,out_mp4)
    finally:
        if temp.exists():temp.unlink()
    return out_mp4


def _concat_filter(videos, infos, output, ffmpeg):
    args=[ffmpeg,'-y','-nostdin','-hide_banner','-loglevel','error']
    for video in videos:args+=['-i',str(video)]
    width,height=infos[0]['width'],infos[0]['height']
    has_audio=any(i['audio'] for i in infos)
    filters=[];parts=[]
    for index,info in enumerate(infos):
        duration=info['video_seconds']
        filters.append(f'[{index}:v:0]settb=AVTB,setpts=N/({info["fps"]}*TB),'
                       f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                       f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{index}]')
        parts.append(f'[v{index}]')
        if has_audio:
            if info['audio']:
                filters.append(f'[{index}:a:0]atrim=duration={duration:.9f},asetpts=PTS-STARTPTS,'
                               f'aresample=48000[a{index}]')
            else:
                filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={duration:.9f},'
                               f'asetpts=PTS-STARTPTS[a{index}]')
            parts.append(f'[a{index}]')
    filters.append(''.join(parts)+f'concat=n={len(videos)}:v=1:a={int(has_audio)}[v]'+('[a]' if has_audio else ''))
    args+=['-filter_complex',';'.join(filters),'-map','[v]']
    if has_audio:args+=['-map','[a]','-c:a','aac','-b:a','192k']
    args+=['-map_metadata','0','-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p']
    if all(i['fps']==infos[0]['fps'] for i in infos):
        args+=['-r',str(infos[0]['fps']),'-fps_mode','cfr']
    else:
        args+=['-fps_mode','vfr']
    args+=['-video_track_timescale','90000','-movflags','+faststart+use_metadata_tags',str(output)]
    return _run(args)


def concat_videos(videos, out_mp4, ffmpeg=None, target_seconds=None):
    """Join frames in the supplied order; explicit frame durations avoid MP4 tick drift."""
    ffmpeg=ffmpeg or find_ffmpeg();videos=[Path(v) for v in videos];out_mp4=Path(out_mp4)
    if not videos:raise ValueError('没有可以拼接的视频')
    if out_mp4.resolve() in [v.resolve() for v in videos]:raise ValueError('拼接输出不能覆盖原始分段')
    infos=[_probe_video(v,ffmpeg) for v in videos]
    total=sum(i['video_seconds'] for i in infos)
    target=float(total if target_seconds is None else target_seconds)
    tolerance=max(1/float(i['fps']) for i in infos)+0.000001
    if target<=0 or not math.isfinite(target) or abs(total-target)>tolerance:
        raise RuntimeError(f'分段帧时长合计{total:.6f}s与目标{target:.6f}s不符，不会补帧或截掉段落')
    expected_frames=sum(i['frames'] for i in infos)
    same_fps=all(i['fps']==infos[0]['fps'] for i in infos)
    compatible=all((i['width'],i['height'],i['codec'],i['audio'],i['audio_codec'])==
                   (infos[0]['width'],infos[0]['height'],infos[0]['codec'],infos[0]['audio'],infos[0]['audio_codec']) for i in infos)
    out_mp4.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=out_mp4.stem+'_concat_',dir=out_mp4.parent) as directory:
        work=Path(directory)
        if work.resolve().parent!=out_mp4.parent.resolve():raise RuntimeError('临时目录不在预期输出目录中')
        temp=work/'joined.mp4';copy_ok=False
        if same_fps and compatible:
            listing=work/'timeline.ffconcat'
            rows=['ffconcat version 1.0']
            for video,info in zip(videos,infos):
                escaped=str(video.resolve()).replace('\\','/').replace("'","'\\''")
                rows.extend([f"file '{escaped}'",f'duration {info["video_seconds"]:.12f}'])
            listing.write_text('\n'.join(rows)+'\n',encoding='utf-8')
            result=_run([ffmpeg,'-y','-nostdin','-hide_banner','-loglevel','error','-f','concat','-safe','0',
                         '-i',str(listing),'-map','0:v:0','-map','0:a?','-c','copy',
                         '-video_track_timescale','90000','-movflags','+faststart+use_metadata_tags',str(temp)])
            if not result.returncode:
                try:
                    _check_timeline(temp,ffmpeg,target,expected_frames,infos[0]['fps'],any(i['audio'] for i in infos))
                    copy_ok=True
                except RuntimeError:pass
        if not copy_ok:
            if temp.exists():temp.unlink()
            result=_concat_filter(videos,infos,temp,ffmpeg)
            if result.returncode:raise RuntimeError('拼接视频失败: '+result.stderr[-800:])
            _check_timeline(temp,ffmpeg,target,expected_frames,infos[0]['fps'] if same_fps else None,
                            any(i['audio'] for i in infos),allow_vfr=not same_fps)
        os.replace(temp,out_mp4)
    return out_mp4
