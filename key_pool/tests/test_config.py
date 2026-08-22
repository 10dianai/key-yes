"""配置加载与校验测试。"""

import json

import pytest

from app.config import ConfigError, load_config


class TestLoadConfig:
    def test_minimal_config(self, tmp_path):
        path = tmp_path / "key_pool_config.json"
        path.write_text("{}", encoding="utf-8")
        settings = load_config(path)
        assert settings.host == "127.0.0.1"
        assert settings.port == 8787
        assert settings.upstream_base_url == "https://api.mistral.ai"
        assert settings.data_file == tmp_path / "pool_data.json"

    def test_full_config(self, tmp_path):
        path = tmp_path / "key_pool_config.json"
        path.write_text(json.dumps({
            "host": "0.0.0.0", "port": 9000,
            "upstream_base_url": "https://api.mistral.ai",
            "upstream_proxy": "http://127.0.0.1:7897",
            "pool_api_keys": ["sk-a", " "],
            "admin_key": "admin-1",
            "data_file": "custom.json",
        }), encoding="utf-8")
        settings = load_config(path)
        assert settings.port == 9000
        assert settings.pool_api_keys == ("sk-a",)  # 空白被过滤
        assert settings.data_file == tmp_path / "custom.json"
        assert settings.upstream_proxy == "http://127.0.0.1:7897"

    def test_local_override(self, tmp_path):
        (tmp_path / "key_pool_config.json").write_text(
            json.dumps({"port": 8787, "admin_key": "base"}),
            encoding="utf-8")
        (tmp_path / "key_pool_config.local.json").write_text(
            json.dumps({"port": 9000}), encoding="utf-8")
        settings = load_config(tmp_path / "key_pool_config.json")
        assert settings.port == 9000            # 被覆盖
        assert settings.admin_key == "base"     # 未覆盖的保留

    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="不存在"):
            load_config(tmp_path / "nope.json")

    def test_broken_json(self, tmp_path):
        path = tmp_path / "key_pool_config.json"
        path.write_text("{broken", encoding="utf-8")
        with pytest.raises(ConfigError, match="JSON"):
            load_config(path)


class TestValidation:
    def _with(self, tmp_path, **overrides):
        base = {"host": "127.0.0.1"}
        base.update(overrides)
        path = tmp_path / "key_pool_config.json"
        path.write_text(json.dumps(base), encoding="utf-8")
        return load_config(path)

    def test_port_string_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="port"):
            self._with(tmp_path, port="9000")

    def test_port_out_of_range(self, tmp_path):
        with pytest.raises(ConfigError, match="port"):
            self._with(tmp_path, port=99999)

    def test_bad_url(self, tmp_path):
        with pytest.raises(ConfigError, match="upstream_base_url"):
            self._with(tmp_path, upstream_base_url="ftp://x")

    def test_pool_keys_wrong_type(self, tmp_path):
        with pytest.raises(ConfigError, match="pool_api_keys"):
            self._with(tmp_path, pool_api_keys="not-a-list")

    def test_retry_wrong_type(self, tmp_path):
        with pytest.raises(ConfigError, match="key_retry_on_rate_limit"):
            self._with(tmp_path, key_retry_on_rate_limit="3")

    def test_boundary_port_accepted(self, tmp_path):
        assert self._with(tmp_path, port=65535).port == 65535
        assert self._with(tmp_path, port=1).port == 1


class TestPanelAuthUnit:
    def test_password_roundtrip(self, tmp_path):
        from app.panel_auth import PanelAuth
        auth = PanelAuth(tmp_path / "panel_auth.json")
        assert not auth.has_password()
        auth.set_password("secret-123")
        assert auth.has_password()
        assert auth.check_password("secret-123")
        assert not auth.check_password("wrong")

    def test_short_password_rejected(self, tmp_path):
        from app.panel_auth import PanelAuth
        auth = PanelAuth(tmp_path / "panel_auth.json")
        with pytest.raises(ValueError, match="至少 6 位"):
            auth.set_password("123")

    def test_token_lifecycle(self, tmp_path):
        from app.panel_auth import PanelAuth
        auth = PanelAuth(tmp_path / "panel_auth.json")
        token = auth.create_token()
        assert auth.token_valid(token)
        auth.revoke_token(token)
        assert not auth.token_valid(token)

    def test_corrupt_auth_file_rejects_all(self, tmp_path):
        from app.panel_auth import PanelAuth
        path = tmp_path / "panel_auth.json"
        path.write_text("garbage", encoding="utf-8")
        auth = PanelAuth(path)
        # 损坏文件视为未初始化：首屏会重新弹"初始化面板"而不是死锁在登录框
        assert not auth.has_password()
        assert not auth.check_password("anything")

    def test_empty_auth_file_is_uninitialized(self, tmp_path):
        """部署时 touch 出的空文件不算已初始化（修复的 bug 场景）。"""
        from app.panel_auth import PanelAuth
        path = tmp_path / "panel_auth.json"
        path.write_text("", encoding="utf-8")
        auth = PanelAuth(path)
        assert not auth.has_password()
        # 可以直接走 setup 流程设置新密码
        auth.set_password("new-pass-123")
        assert auth.has_password()
        assert auth.check_password("new-pass-123")

    def test_fail_lockout(self, tmp_path):
        from app.panel_auth import PanelAuth
        auth = PanelAuth(tmp_path / "panel_auth.json")
        for _ in range(5):
            assert auth.login_allowed()
            auth.record_fail()
        assert not auth.login_allowed()
