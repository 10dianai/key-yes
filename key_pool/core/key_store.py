#!/usr/bin/env python3
"""Key 池存储与调度：轮询取 Key、失败计数、自动禁用、持久化与滚动备份。

存储文件 pool_data.json 结构：
{
  "keys": [
    {
      "id": "k_0001",
      "key": "32位字母数字",
      "label": "备注（通常是邮箱）",
      "source": "来源文件（batch1.zip / mistral_keys/）",
      "status": "active | disabled | invalid",
      "fail_count": 0,
      "use_count": 0,
      "last_used": "2026-08-21T12:00:00",
      "last_error": null,
      "created_at": "..."
    }
  ],
  "rr_index": 0
}

持久化策略：
- 池组成变化（增删/启停/清空）立即落盘，落盘前滚动备份（10 分钟节流）
- 仅统计变化（pick/mark_result）只标脏，由后台线程每 FLUSH_INTERVAL 秒落盘，
  避免每个请求都全量写盘（写放大）
- 主文件损坏时自动从最近备份恢复
"""

import json
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

MISTRAL_KEY_PATTERN = re.compile(r"^[A-Za-z0-9]{32}$")
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUS_INVALID = "invalid"

BACKUP_COUNT = 3          # 保留 pool_data.json.bak1 ~ bak3
BACKUP_MIN_INTERVAL = 600  # 两次备份间隔至少 10 分钟（节流）
FLUSH_INTERVAL = 5.0       # 脏数据后台落盘周期（秒）
MARK_INVALID_AFTER_FAILS = 3  # 连续失败阈值


def now_str():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def looks_like_mistral_key(token):
    """Mistral API Key 是 32 位字母数字字符串（大小写混合）。"""
    return bool(MISTRAL_KEY_PATTERN.fullmatch(token))


class KeyStore:
    """线程安全的 Key 池。

    Args:
        data_file: 池数据文件路径。
        start_flusher: 是否启动后台落盘线程（测试/只读场景可关）。
        logger: 可选日志对象（须有 .warning/.info 方法），缺省时静默。
    """

    def __init__(self, data_file, start_flusher=True, logger=None):
        self.data_file = Path(data_file)
        self._lock = threading.RLock()
        self._keys = []       # list[dict]
        self._by_value = {}   # key字符串 -> entry（去重用）
        self._rr_index = 0
        self._next_id = 1
        self._dirty = False
        self._last_backup = 0.0
        self._logger = logger
        self._load()
        self._stop_event = threading.Event()
        self._flusher = None
        if start_flusher:
            self._flusher = threading.Thread(
                target=self._flush_loop, name="keystore-flush", daemon=True
            )
            self._flusher.start()

    # ---- 持久化 ----

    def _backup_locked(self):
        """滚动备份：bak2->bak3, bak1->bak2, 当前->bak1。带节流。"""
        if not self.data_file.exists():
            return
        if time.monotonic() - self._last_backup < BACKUP_MIN_INTERVAL:
            return
        try:
            for index in range(BACKUP_COUNT - 1, 0, -1):
                src = self.data_file.with_suffix(f".json.bak{index}")
                dst = self.data_file.with_suffix(f".json.bak{index + 1}")
                if src.exists():
                    shutil.copy2(src, dst)
            shutil.copy2(
                self.data_file, self.data_file.with_suffix(".json.bak1")
            )
            self._last_backup = time.monotonic()
        except OSError as exc:
            if self._logger:
                self._logger.warning("备份池数据失败: %s", exc)

    def _save_locked(self):
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"keys": self._keys, "rr_index": self._rr_index}
        temp = self.data_file.with_suffix(".json.tmp")
        try:
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._backup_locked()
            temp.replace(self.data_file)
        except OSError:
            raise
        self._dirty = False

    def _load(self):
        if not self.data_file.exists():
            return
        try:
            payload = json.loads(self.data_file.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as exc:
            recovered = self._recover_from_backup()
            if recovered is None:
                if self._logger:
                    self._logger.warning(
                        "池数据损坏且无可用备份，从空池启动: %s", exc
                    )
                return
            payload = recovered
        self._keys = list(payload.get("keys") or [])
        self._rr_index = int(payload.get("rr_index") or 0)
        for entry in self._keys:
            self._by_value[entry["key"]] = entry
            numeric = re.search(r"(\d+)$", entry.get("id") or "")
            if numeric:
                self._next_id = max(self._next_id, int(numeric.group(1)) + 1)

    def _recover_from_backup(self):
        """主文件损坏时依次尝试 bak1~bak3，返回可用的 payload 或 None。"""
        for index in range(1, BACKUP_COUNT + 1):
            candidate = self.data_file.with_suffix(f".json.bak{index}")
            if not candidate.exists():
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
                if isinstance(payload.get("keys"), list):
                    if self._logger:
                        self._logger.warning(
                            "主数据文件损坏，已从备份恢复: %s（%d 个 Key）",
                            candidate.name, len(payload["keys"]),
                        )
                    return payload
            except (json.JSONDecodeError, OSError):
                continue
        return None

    # ---- 后台落盘 ----

    def _flush_loop(self):
        while not self._stop_event.wait(FLUSH_INTERVAL):
            self.flush()

    def flush(self):
        """把脏数据落盘（组成变化已在调用处即时保存，这里兜底统计变化）。"""
        with self._lock:
            if not self._dirty:
                return
            self._save_locked()

    def close(self):
        """停止后台线程并做最后一次落盘。"""
        self._stop_event.set()
        if self._flusher and self._flusher.is_alive():
            self._flusher.join(timeout=FLUSH_INTERVAL + 1)
        self.flush()

    # ---- 导入 ----

    def add(self, key, label="", source="", strict=True):
        """新增单个 Key。返回 (entry, 状态)。

        状态: "added" 新增 | "duplicate" 已存在 | "invalid-format"/"empty" 拒绝
        strict=True 时非 32 位字母数字的 Key 直接拒绝。
        """
        key = str(key or "").strip()
        if not key:
            return None, "empty"
        if strict and not looks_like_mistral_key(key):
            return None, "invalid-format"
        with self._lock:
            if key in self._by_value:
                return self._by_value[key], "duplicate"
            entry = {
                "id": f"k_{self._next_id:04d}",
                "key": key,
                "label": label or "",
                "source": source or "",
                "status": STATUS_ACTIVE,
                "fail_count": 0,
                "use_count": 0,
                "last_used": None,
                "last_error": None,
                "created_at": now_str(),
            }
            self._next_id += 1
            self._keys.append(entry)
            self._by_value[key] = entry
            self._save_locked()  # 池组成变化：立即落盘
            return entry, "added"

    def add_many(self, items, strict=True):
        """批量导入。items 是 [(key, label, source), ...]，返回统计报告。"""
        report = {"added": 0, "duplicate": 0, "invalid": 0, "invalid_samples": []}
        for key, label, source in items:
            _, status = self.add(key, label=label, source=source, strict=strict)
            if status == "added":
                report["added"] += 1
            elif status == "duplicate":
                report["duplicate"] += 1
            else:
                report["invalid"] += 1
                if len(report["invalid_samples"]) < 10:
                    report["invalid_samples"].append(key[:40])
        return report

    # ---- 调度 ----

    def pick(self):
        """轮询返回下一个可用 Key 的 entry；池子空返回 None。"""
        with self._lock:
            active = [e for e in self._keys if e["status"] == STATUS_ACTIVE]
            if not active:
                return None
            if self._rr_index >= len(active):
                self._rr_index = 0
            entry = active[self._rr_index]
            self._rr_index = (self._rr_index + 1) % max(len(active), 1)
            entry["use_count"] = int(entry.get("use_count") or 0) + 1
            entry["last_used"] = now_str()
            self._dirty = True  # 统计变化：标脏，后台落盘
            return entry

    def mark_result(self, entry, ok, error=None):
        """记录调用结果。连续失败达到阈值自动禁用（标脏，后台落盘）。"""
        with self._lock:
            if ok:
                entry["fail_count"] = 0
                entry["last_error"] = None
            else:
                entry["fail_count"] = int(entry.get("fail_count") or 0) + 1
                entry["last_error"] = str(error or "")[:200]
                if entry["fail_count"] >= MARK_INVALID_AFTER_FAILS:
                    entry["status"] = STATUS_INVALID
            self._dirty = True

    # ---- 管理 ----

    def list_keys(self, status=None):
        with self._lock:
            keys = [dict(e) for e in self._keys]
        if status:
            keys = [e for e in keys if e["status"] == status]
        return keys

    def stats(self):
        with self._lock:
            total = len(self._keys)
            by_status = {STATUS_ACTIVE: 0, STATUS_DISABLED: 0, STATUS_INVALID: 0}
            for entry in self._keys:
                by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
            total_calls = sum(int(e.get("use_count") or 0) for e in self._keys)
        return {
            "total": total,
            "active": by_status[STATUS_ACTIVE],
            "disabled": by_status[STATUS_DISABLED],
            "invalid": by_status[STATUS_INVALID],
            "total_calls": total_calls,
            "updated_at": now_str(),
        }

    def set_status(self, key_id, status):
        with self._lock:
            for entry in self._keys:
                if entry["id"] == key_id:
                    entry["status"] = status
                    if status == STATUS_ACTIVE:
                        entry["fail_count"] = 0
                        entry["last_error"] = None
                    self._save_locked()  # 组成变化：立即落盘
                    return dict(entry)
        return None

    def delete(self, key_id):
        with self._lock:
            for index, entry in enumerate(self._keys):
                if entry["id"] == key_id:
                    self._keys.pop(index)
                    self._by_value.pop(entry["key"], None)
                    self._save_locked()
                    return dict(entry)
        return None

    def clear(self, status=None):
        """清空（可只清某种状态）。返回删除数量。"""
        with self._lock:
            if status is None:
                removed = len(self._keys)
                self._keys = []
                self._by_value = {}
            else:
                keep = [e for e in self._keys if e["status"] != status]
                removed = len(self._keys) - len(keep)
                self._keys = keep
                self._by_value = {e["key"]: e for e in keep}
            self._rr_index = 0
            self._save_locked()
            return removed
