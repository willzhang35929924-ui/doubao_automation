"""飞书开放平台客户端：tenant_access_token、多维表格记录、附件下载、固定指令读取。

需要在飞书开放平台创建自建应用，开通权限（并创建/发布版本）：
  - bitable:app:readonly （查看多维表格）
  - drive:drive:readonly （下载云空间文件/附件）
然后把应用添加为目标 Base 的协作者（可阅读即可）。
"""
import re
import time
from pathlib import Path

import requests

from .config import get
from .paths import PRODUCTS_DIR, ROOT, SOURCES_DIR, resolve, safe_filename


class FeishuError(Exception):
    pass


class FeishuClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg.get("feishu", {}) or {}
        self.base = str(self.cfg.get("base_url", "https://open.feishu.cn")).rstrip("/")
        self.app_id = str(self.cfg.get("app_id") or "").strip()
        self.app_secret = str(self.cfg.get("app_secret") or "").strip()
        self.app_token = str(self.cfg.get("app_token") or "").strip()
        self._token = ""
        self._token_expire = 0.0
        if not self.app_id or not self.app_secret:
            raise FeishuError(
                "缺少飞书凭证：请在 config/config.yaml 的 feishu.app_id / feishu.app_secret 填入"
                "自建应用的 App ID 与 App Secret（应用需开通多维表格读取与云空间文件下载权限，"
                "并被添加为该 Base 的协作者）。"
            )
        if not self.app_token:
            raise FeishuError("缺少 feishu.app_token（多维表格链接 /base/ 后面那段）。")

    def validate_sync_config(self):
        """Check local prerequisites before requesting or downloading any data."""
        for key, label in (("products_table", "产品图表"), ("scripts_table", "提示词/视频表")):
            table = self.cfg.get(key) or {}
            if not isinstance(table, dict) or not str(table.get("table_id") or "").strip():
                raise FeishuError(f"未配置{label}的表格 ID：请填写 feishu.{key}.table_id。")
        spec = self.cfg.get("fixed_prompt") or {}
        if not isinstance(spec, dict):
            raise FeishuError("飞书固定提示词配置格式错误：feishu.fixed_prompt 应为配置项。")
        source = str(spec.get("source", "file"))
        if source == "file":
            p = resolve(spec.get("file", "config/gemini_prompt.txt"))
            if not p.is_file():
                raise FeishuError("未找到固定提示词文件。请在本机创建 config/gemini_prompt.txt，或配置 feishu.fixed_prompt 使用飞书表格/文档。")
        elif source == "bitable":
            if not str(spec.get("table_id") or "").strip():
                raise FeishuError("固定提示词使用飞书表格时，请填写 feishu.fixed_prompt.table_id。")
        elif source == "docx":
            if not str(spec.get("document_id") or "").strip():
                raise FeishuError("固定提示词使用飞书文档时，请填写 feishu.fixed_prompt.document_id。")
        else:
            raise FeishuError("未知的固定提示词来源，请使用 file、bitable 或 docx。")

    # ---- 认证 ----
    def token(self) -> str:
        if self._token and time.time() < self._token_expire - 60:
            return self._token
        r = requests.post(
            f"{self.base}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=30,
        )
        data = r.json()
        if data.get("code") != 0:
            raise FeishuError(f"获取 tenant_access_token 失败: {data}")
        self._token = data["tenant_access_token"]
        self._token_expire = time.time() + float(data.get("expire", 7200))
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}

    # ---- 多维表格 ----
    def list_records(self, table_id: str, page_size: int = 100) -> list:
        records, page_token = [], ""
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            r = requests.get(
                f"{self.base}/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/records",
                headers=self._headers(), params=params, timeout=60,
            )
            data = r.json()
            if data.get("code") != 0:
                raise FeishuError(f"读取表 {table_id} 记录失败: {data}")
            payload = data.get("data", {})
            records.extend(payload.get("items") or [])
            if not payload.get("has_more"):
                return records
            page_token = payload.get("page_token", "")

    @staticmethod
    def field_text(value) -> str:
        """多维表格文本列可能是 str 或分段 list[dict]，统一转纯文本。"""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for seg in value:
                if isinstance(seg, dict):
                    parts.append(str(seg.get("text", "")))
                else:
                    parts.append(str(seg))
            return "".join(parts)
        if isinstance(value, dict):
            return str(value.get("text", value))
        return str(value)

    @staticmethod
    def field_attachments(value) -> list:
        """附件列 → [{'file_token','name','size',...}]。"""
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict) and x.get("file_token")]
        return []

    def download_media(self, file_token: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(
            f"{self.base}/open-apis/drive/v1/medias/{file_token}/download",
            headers=self._headers(), stream=True, timeout=300,
        ) as r:
            ctype = r.headers.get("Content-Type", "")
            if "application/json" in ctype:
                raise FeishuError(f"下载附件 {file_token} 失败: {r.text[:500]}")
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
        if dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise FeishuError(f"下载附件 {file_token} 得到空文件")
        return dest

    def _require_field(self, table_id: str, field: str, what: str) -> str:
        """校验列名存在；不存在时抛出带可用列清单的错误，便于快速改配置。"""
        names = [f.get("field_name", "") for f in self.list_fields(table_id)]
        if field not in names:
            raise FeishuError(f"表 {table_id} 找不到{what}列「{field}」，可用列: {names}")
        return field

    # ---- 业务封装 ----
    def fetch_products(self) -> list:
        """产品图表 → [{'record_id','name','images':[Path]}]"""
        tcfg = self.cfg.get("products_table", {}) or {}
        table_id = tcfg.get("table_id", "")
        image_field = self._require_field(table_id, tcfg.get("image_field", "产品图"), "产品图")
        name_field = (tcfg.get("name_field") or "").strip()
        keyword_field = self._require_field(table_id, tcfg.get("keyword_field", "keyword"), "keyword")
        out = []
        for rec in self.list_records(table_id):
            rid = rec.get("record_id", "")
            fields = rec.get("fields", {}) or {}
            keyword = self.field_text(fields.get(keyword_field)).strip()
            atts = self.field_attachments(fields.get(image_field))
            if not atts:
                print(f"[飞书] 产品表记录 {rid} 的「{image_field}」列没有附件，跳过")
                continue
            name = self.field_text(fields.get(name_field)) if name_field else ""
            rec_dir = PRODUCTS_DIR / safe_filename(name or rid)
            images = []
            for att in atts:
                fname = safe_filename(att.get("name") or att["file_token"])
                images.append(self.download_media(att["file_token"], rec_dir / fname))
            out.append({"record_id": rid, "name": name or rid, "keyword": keyword, "images": [str(p) for p in images]})
            print(f"[飞书] 产品「{name or rid}」下载 {len(images)} 张图片")
        return out

    def fetch_sources(self) -> list:
        """提示词/视频表 → [{'record_id','prompt_text','videos':[Path]}]"""
        tcfg = self.cfg.get("scripts_table", {}) or {}
        table_id = tcfg.get("table_id", "")
        prompt_field = self._require_field(table_id, tcfg.get("prompt_field", "提示词"), "提示词")
        video_field = self._require_field(table_id, tcfg.get("video_field", "视频"), "视频")
        max_mb = float(self.cfg.get("max_video_size_mb", 200))
        out = []
        for rec in self.list_records(table_id):
            rid = rec.get("record_id", "")
            fields = rec.get("fields", {}) or {}
            prompt_text = self.field_text(fields.get(prompt_field)).strip()
            atts = self.field_attachments(fields.get(video_field))
            videos = []
            for att in atts:
                size_mb = float(att.get("size", 0)) / 1048576
                if max_mb and size_mb > max_mb:
                    print(f"[飞书] ⚠ 视频 {att.get('name')} 有 {size_mb:.0f}MB，超过上限 {max_mb:.0f}MB，跳过")
                    continue
                fname = safe_filename(att.get("name") or att["file_token"])
                videos.append(str(self.download_media(att["file_token"], SOURCES_DIR / rid / fname)))
            if not prompt_text and not videos:
                print(f"[飞书] 记录 {rid} 既无提示词也无视频，跳过")
                continue
            keyword_field = self.cfg.get("scripts_table", {}).get("keyword_field", "keyword")
            keyword = self.field_text(fields.get(keyword_field)).strip()
            out.append({"record_id": rid, "keyword": keyword, "prompt_text": prompt_text, "videos": videos})
            print(f"[飞书] 源记录 {rid}: 提示词 {len(prompt_text)} 字, 视频 {len(videos)} 个")
        return out

    def list_fields(self, table_id: str) -> list:
        """列出表的字段（顺序与多维表格列顺序一致）。"""
        r = requests.get(
            f"{self.base}/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields",
            headers=self._headers(), params={"page_size": 100}, timeout=60,
        )
        data = r.json()
        if data.get("code") != 0:
            raise FeishuError(f"读取表 {table_id} 字段失败: {data}")
        return data.get("data", {}).get("items") or []

    def load_fixed_prompt(self) -> str:
        """读取固定提示词指令，来源：file | bitable | docx。
        bitable 模式：field 留空时按 field_index（0 起算）自动解析列名；
        record_index=-1 时扫描所有行取该列第一个非空单元格。"""
        spec = self.cfg.get("fixed_prompt", {}) or {}
        source = str(spec.get("source", "file"))
        if source == "file":
            p = resolve(spec.get("file", "config/gemini_prompt.txt"))
            if not p.exists():
                raise FeishuError(f"固定指令文件不存在: {p}")
            return p.read_text(encoding="utf-8").strip()
        if source == "bitable":
            table_id = str(spec.get("table_id", ""))
            field = str(spec.get("field", "") or "").strip()
            if not field:
                idx = int(spec.get("field_index", 0) or 0)
                fields = self.list_fields(table_id)
                names = [f.get("field_name", "") for f in fields]
                if idx >= len(names):
                    raise FeishuError(f"表 {table_id} 只有 {len(names)} 列（{names}），取不到第 {idx + 1} 列")
                field = names[idx]
                print(f"[飞书] 固定指令列（第 {idx + 1} 列）解析为: 「{field}」")
            recs = self.list_records(table_id)
            rec_idx = int(spec.get("record_index", -1))
            if rec_idx >= 0:
                if rec_idx >= len(recs):
                    raise FeishuError(f"固定指令表记录不足（需要第 {rec_idx} 条，共 {len(recs)} 条）")
                text = self.field_text(recs[rec_idx].get("fields", {}).get(field)).strip()
            else:
                text = ""
                for rec in recs:
                    t = self.field_text(rec.get("fields", {}).get(field)).strip()
                    if t:
                        text = t
                        break
            if not text:
                raise FeishuError(f"表 {table_id} 的「{field}」列在所有记录中都为空")
            return text
        if source == "docx":
            doc_id = str(spec.get("document_id", ""))
            r = requests.get(
                f"{self.base}/open-apis/docx/v1/documents/{doc_id}/raw_content",
                headers=self._headers(), timeout=60,
            )
            data = r.json()
            if data.get("code") != 0:
                raise FeishuError(f"读取飞书文档失败: {data}")
            return str(data.get("data", {}).get("content", "")).strip()
        raise FeishuError(f"未知的固定指令来源: {source}")
