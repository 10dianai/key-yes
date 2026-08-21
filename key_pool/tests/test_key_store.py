"""KeyStore 单元测试：去重、轮询、失败禁用、备份恢复、防写放大。"""

import json

from core.key_store import (
    KeyStore, STATUS_ACTIVE, STATUS_DISABLED, STATUS_INVALID,
    looks_like_mistral_key,
)


class TestKeyFormat:
    # 测试假数据在运行时拼接构造，源码中不出现完整 Key 形态的字符串，
    # 避免被 GitHub 密钥扫描误报。
    FAKE_KEY = "Ma2" * 10 + "Xy"   # 32 位字母数字

    def test_valid_alnum32(self):
        assert looks_like_mistral_key(self.FAKE_KEY)

    def test_hex_is_subset(self):
        assert looks_like_mistral_key("a1b2c3d4" * 4)

    def test_reject_short(self):
        assert not looks_like_mistral_key("short")

    def test_reject_prefix(self):
        assert not looks_like_mistral_key("sk-abc123")

    def test_reject_chinese(self):
        assert not looks_like_mistral_key("纯中文没有key哦哦哦哦哦哦哦哦哦哦哦哦哦哦")


class TestAddAndDedup:
    def test_add_and_duplicate(self, tmp_path):
        store = KeyStore(tmp_path / "p.json", start_flusher=False)
        entry, status = store.add("A" * 32, label="t")
        assert status == "added" and entry["id"] == "k_0001"
        _, status = store.add("A" * 32)
        assert status == "duplicate"

    def test_strict_rejects_invalid(self, tmp_path):
        store = KeyStore(tmp_path / "p.json", start_flusher=False)
        _, status = store.add("zzz")
        assert status == "invalid-format"

    def test_add_many_report(self, tmp_path):
        store = KeyStore(tmp_path / "p.json", start_flusher=False)
        report = store.add_many([("A" * 32, "", ""), ("A" * 32, "", ""), ("bad", "", "")])
        assert report["added"] == 1 and report["duplicate"] == 1
        assert report["invalid"] == 1


class TestRoundRobin:
    def test_pick_cycles(self, tmp_path):
        store = KeyStore(tmp_path / "p.json", start_flusher=False)
        store.add("A" * 32)
        store.add("B" * 32)
        picked = [store.pick()["key"] for _ in range(4)]
        assert picked[0] != picked[1]
        assert picked[0] == picked[2] and picked[1] == picked[3]

    def test_pick_empty_pool(self, tmp_path):
        store = KeyStore(tmp_path / "p.json", start_flusher=False)
        assert store.pick() is None

    def test_pick_skips_disabled(self, tmp_path):
        store = KeyStore(tmp_path / "p.json", start_flusher=False)
        entry = store.add("A" * 32)[0]
        store.add("B" * 32)
        store.set_status(entry["id"], STATUS_DISABLED)
        picked = {store.pick()["key"] for _ in range(10)}
        assert picked == {"B" * 32}


class TestFailureHandling:
    def test_three_fails_marks_invalid(self, tmp_path):
        store = KeyStore(tmp_path / "p.json", start_flusher=False)
        entry = store.add("A" * 32)[0]
        for _ in range(3):
            store.mark_result(entry, ok=False, error="401")
        assert entry["status"] == STATUS_INVALID
        assert "401" in entry["last_error"]

    def test_success_resets_fail_count(self, tmp_path):
        store = KeyStore(tmp_path / "p.json", start_flusher=False)
        entry = store.add("A" * 32)[0]
        store.mark_result(entry, ok=False, error="429")
        store.mark_result(entry, ok=True)
        assert entry["fail_count"] == 0 and entry["status"] == STATUS_ACTIVE


class TestPersistence:
    def test_reload(self, tmp_path):
        path = tmp_path / "p.json"
        store = KeyStore(path, start_flusher=False)
        store.add("A" * 32)
        store.add("B" * 32)
        reloaded = KeyStore(path, start_flusher=False)
        assert reloaded.stats()["total"] == 2

    def test_flush_picks_up_statistics(self, tmp_path):
        path = tmp_path / "p.json"
        store = KeyStore(path, start_flusher=False)
        store.add("A" * 32)
        store.pick()
        store.flush()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["keys"][0]["use_count"] == 1

    def test_no_write_amplification_on_pick(self, tmp_path):
        path = tmp_path / "p.json"
        store = KeyStore(path, start_flusher=False)
        store.add("A" * 32)
        counter = {"n": 0}
        original = store._save_locked

        def counting(original=original, counter=counter):
            counter["n"] += 1
            original()

        store._save_locked = counting
        for _ in range(100):
            store.pick()
        assert counter["n"] == 0, "pick 不应立即落盘"
        store.flush()
        assert counter["n"] == 1

    def test_corrupt_file_with_backup_recovers(self, tmp_path):
        path = tmp_path / "p.json"
        entry = {"id": "k_0001", "key": "C" * 32, "label": "from-backup",
                 "source": "", "status": "active", "fail_count": 0,
                 "use_count": 5, "last_used": None, "last_error": None,
                 "created_at": "x"}
        (tmp_path / "p.json.bak1").write_text(
            json.dumps({"keys": [entry], "rr_index": 0}), encoding="utf-8")
        path.write_text("garbage", encoding="utf-8")
        store = KeyStore(path, start_flusher=False)
        assert store.stats()["total"] == 1
        assert store.list_keys()[0]["label"] == "from-backup"

    def test_corrupt_file_without_backup_starts_empty(self, tmp_path):
        path = tmp_path / "p.json"
        path.write_text("{corrupted", encoding="utf-8")
        store = KeyStore(path, start_flusher=False)
        assert store.stats()["total"] == 0


class TestManagement:
    def test_set_status_and_enable(self, tmp_path):
        store = KeyStore(tmp_path / "p.json", start_flusher=False)
        entry = store.add("A" * 32)[0]
        store.set_status(entry["id"], STATUS_DISABLED)
        assert store.stats()["disabled"] == 1
        store.set_status(entry["id"], STATUS_ACTIVE)
        assert store.stats()["active"] == 1

    def test_delete(self, tmp_path):
        store = KeyStore(tmp_path / "p.json", start_flusher=False)
        entry = store.add("A" * 32)[0]
        assert store.delete(entry["id"])["key"] == "A" * 32
        assert store.delete(entry["id"]) is None

    def test_clear_by_status(self, tmp_path):
        store = KeyStore(tmp_path / "p.json", start_flusher=False)
        entry = store.add("A" * 32)[0]
        store.add("B" * 32)
        for _ in range(3):
            store.mark_result(entry, ok=False)
        assert store.clear(status=STATUS_INVALID) == 1
        assert store.stats()["total"] == 1

    def test_list_keys_filter(self, tmp_path):
        store = KeyStore(tmp_path / "p.json", start_flusher=False)
        store.add("A" * 32)
        assert len(store.list_keys(status=STATUS_DISABLED)) == 0
        assert len(store.list_keys(status=STATUS_ACTIVE)) == 1
