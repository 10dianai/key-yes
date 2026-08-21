#!/usr/bin/env python3
"""批量导入 Key：4 种来源格式的解析。

  1. 单个 TXT 文件导入           import_txt_file("keys.txt")
  2. ZIP 压缩包（平铺 TXT）      import_zip("batch.zip")   -> 自动识别
  3. ZIP 压缩包（文件夹装 TXT）  import_zip("batch.zip")   -> 递归解包，与 2 统一处理
  4. 直接文件夹导入              import_directory("mistral_keys/")

每行文本支持的分隔格式（自动识别最像 Mistral Key 的字段）：
  纯 Key                        Ab3xYz...9QmK（32 位字母数字）
  邮箱----密码----Key           a@b.com----pass----Ab3xYz...
  Key----邮箱                   Ab3xYz...----a@b.com
  邮箱:Key / Key:邮箱           a@b.com:Ab3xYz... / Ab3xYz...:a@b.com
  逗号/竖线/空白/制表符分隔      同理取 32 位字母数字字段

特殊规则：若 TXT 文件只有一行且是合法 Key（mistral_keys/<邮箱>.txt 的结构），
文件名（去扩展名）自动作为该 Key 的备注。

安全约束：zip 只在内存读取不落盘；内部文件名清洗路径穿越；单文件与解压
总量双重上限（防解压炸弹）。
"""

import re
import zipfile
from pathlib import Path

from .key_store import looks_like_mistral_key

EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
# 分隔符：四连横线、冒号、竖线、逗号、制表符、连续空白
SPLIT_PATTERN = re.compile(r"----+|\||\t|,|;|\s+|(?<!\S):(?!\S)|(?<=\S):(?=\S)")

MAX_FILE_BYTES = 200 * 1024 * 1024        # 单文件最大 200MB，防误传大文件
MAX_ZIP_TOTAL_BYTES = 500 * 1024 * 1024   # zip 解压后总内容上限 500MB，防解压炸弹


def _safe_zip_name(filename):
    """清洗 zip 内部文件名：去掉路径穿越（../）、盘符、绝对路径，只留相对路径。

    本项目只在内存中读取 zip 内容不落盘，穿越路径无实际危害，
    但文件名会进入 source/备注 展示，必须清洗。
    """
    rel = str(filename or "").replace("\\", "/")
    # 去盘符（C:）与开头的斜杠
    rel = rel.split(":", 1)[-1].lstrip("/")
    # 逐段过滤 ../ 与 . 段
    parts = [seg for seg in rel.split("/") if seg not in ("", ".", "..")]
    return "/".join(parts) if parts else "unnamed"


def _iter_zip_txt_entries(zf):
    """统一遍历 zip 里的 txt 条目：跳过目录/非 txt/超大文件/危险名，
    并对解压总量做上限校验（防解压炸弹）。产出 (info, safe_name)。"""
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        if not info.filename.lower().endswith(".txt"):
            continue
        if info.file_size > MAX_FILE_BYTES:
            continue
        total += info.file_size
        if total > MAX_ZIP_TOTAL_BYTES:
            raise ValueError("压缩包解压总内容超过 500MB，已中止")
        safe_name = _safe_zip_name(info.filename)
        if not safe_name.endswith(".txt"):
            continue
        yield info, safe_name


def parse_line(line):
    """解析单行，返回 (key, label)。解析不出 Key 返回 (None, "")。"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None, ""

    parts = [p for p in SPLIT_PATTERN.split(line) if p]
    if not parts:
        return None, ""

    # 单字段：整行就是一个 Key（格式校验交给上层 strict 逻辑）
    if len(parts) == 1:
        token = parts[0].strip()
        if "@" in token:
            # 整行是邮箱，不是 Key
            return None, ""
        # 行内可能带 "key: xxx" / "xxx: key" 标注，取冒号后 32 位字母数字的写法
        if ":" in token:
            tail = token.rsplit(":", 1)[-1].strip()
            if looks_like_mistral_key(tail):
                return tail, ""
        return token, ""

    # 多字段：优先取 32 位字母数字字段，其次取最长的 token；备注优先取邮箱字段
    key = next((p for p in parts if looks_like_mistral_key(p)), None)
    if key is None:
        key = max(parts, key=len)
    label = next((p for p in parts if EMAIL_PATTERN.match(p)), "")
    if not label:
        others = [p for p in parts if p != key]
        label = others[0] if len(others) == 1 else ""
    return key, label


def parse_line_strict(line):
    """严格版单行解析：整行只有一个字段且不是合法 Key 时直接拒绝。

    纯文本行（如说明、中文句子）不会像分隔格式那样有旁证字段，
    只有 32 位字母数字才认定为 Key，避免把说明文字当 Key 导入。
    """
    key, label = parse_line(line)
    if key is None:
        return None, ""
    if looks_like_mistral_key(key):
        return key, label
    # 非字母数字 32 位的候选 key：若同行还有其他字段（分隔格式）说明是数据行，放行交给上层判；
    # 只有单字段时必须严格。
    parts = [p for p in SPLIT_PATTERN.split(line.strip()) if p]
    if len(parts) > 1:
        return key, label
    return None, ""


def _parse_text(content, source, default_label=None):
    """解析一段文本内容，返回 [(key, label, source), ...]。

    单字段行只有 32 位字母数字才认定为 Key（防说明文字误入池）；
    带分隔符的行仍按多字段规则解析，交给池的 strict 校验把关。
    """
    items = []
    lines = content.splitlines()
    single = [l for l in lines if l.strip() and not l.strip().startswith("#")]
    single_key_file = len(single) == 1 and looks_like_mistral_key(single[0].strip())
    for line in lines:
        key, label = parse_line_strict(line)
        if not key:
            continue
        if single_key_file and default_label:
            label = default_label
        items.append((key, label, source))
    return items


def _read_text(path, in_zip=False):
    """读取文本，自动尝试 utf-8 / gbk（Windows 导出的 zip 常见 GBK）。"""
    if in_zip:
        raw = path
    else:
        raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# ---- 4 种导入入口 ----

def import_txt_file(path):
    """格式 1：单个 TXT 文件导入。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"不是文件或不存在: {path}")
    if path.suffix.lower() != ".txt":
        raise ValueError(f"只支持 .txt 文件: {path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"文件超过 200MB: {path}")
    content = _read_text(path)
    return _parse_text(content, source=path.name, default_label=path.stem), path.name


def import_zip(path):
    """格式 2 / 3：ZIP 导入。

    压缩包里无论是平铺 TXT 还是文件夹嵌套 TXT，都递归收集：
      batch.zip
        ├── a.txt              <- 格式 2
        └── pool1/
            ├── b.txt          <- 格式 3
            └── sub/
                └── c.txt      <- 格式 3（任意深度）
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"不是文件或不存在: {path}")
    if path.suffix.lower() != ".zip":
        raise ValueError(f"只支持 .zip 压缩包: {path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"压缩包超过 200MB: {path}")

    items = []
    layout = {"root_files": 0, "nested_files": 0}  # 用于导入报告区分格式 2/3
    with zipfile.ZipFile(path) as zf:
        for info, safe_name in _iter_zip_txt_entries(zf):
            with zf.open(info) as handle:
                content = _read_text(handle.read(), in_zip=True)
            stem = Path(safe_name).stem
            file_items = _parse_text(content, source=f"{path.name}/{safe_name}", default_label=stem)
            items.extend(file_items)
            if "/" in safe_name:
                layout["nested_files"] += 1
            else:
                layout["root_files"] += 1
    return items, {"zip": path.name, **layout}


def import_directory(path):
    """格式 4：直接文件夹导入（递归收集所有 .txt）。"""
    path = Path(path)
    if not path.is_dir():
        raise NotADirectoryError(f"不是文件夹或不存在: {path}")
    items = []
    files = sorted(p for p in path.rglob("*.txt") if p.is_file() and p.stat().st_size <= MAX_FILE_BYTES)
    for file_path in files:
        content = _read_text(file_path)
        rel = file_path.relative_to(path)
        items.extend(
            _parse_text(content, source=str(rel), default_label=file_path.stem)
        )
    return items, {"directory": str(path), "txt_files": len(files)}


def import_auto(path):
    """自动识别来源类型：TXT / ZIP / 文件夹。返回 (items, 来源描述)。"""
    path = Path(path)
    if path.is_dir():
        return import_directory(path)
    if not path.exists():
        raise FileNotFoundError(f"路径不存在: {path}")
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return import_txt_file(path)
    if suffix == ".zip":
        return import_zip(path)
    raise ValueError(f"不支持的文件类型 {suffix}，仅支持 .txt / .zip / 文件夹")


def _read_bytes(payload):
    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def import_bytes(filename, payload):
    """管理界面上传导入：根据文件名后缀解析内存中的文件内容。"""
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError("上传文件超过 200MB")
    name = filename or ""
    suffix = Path(name).suffix.lower()
    if suffix == ".zip":
        import io
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            items = []
            for info, safe_name in _iter_zip_txt_entries(zf):
                with zf.open(info) as handle:
                    content = _read_text(handle.read(), in_zip=True)
                items.extend(
                    _parse_text(content, source=f"{name}/{safe_name}",
                                default_label=Path(safe_name).stem)
                )
        return items, {"upload_zip": name}
    if suffix == ".txt" or not suffix:
        content = _read_bytes(payload)
        return _parse_text(content, source=name, default_label=Path(name).stem), {"upload_txt": name}
    raise ValueError(f"不支持的上传类型 {suffix}，仅支持 .txt / .zip")
