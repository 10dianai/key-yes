"""面板密码新机制测试：配置明文密码、默认密码强制改、改密后配置清空。"""

import json

import pytest

from conftest import make_settings
from app.server import create_app


def make_password_config(tmp_path, panel_password="admin123"):
    """写一个含 panel_password 的配置文件，返回 Settings。"""
    config_file = tmp_path / "key_pool_config.json"
    config_file.write_text(json.dumps({
        "pool_api_keys": ["sk-pool-test"],
        "admin_key": "admin-test",
        "panel_password": panel_password,
    }), encoding="utf-8")
    from app.config import load_config
    return load_config(config_file)


class TestConfigPassword:
    def test_panel_password_loaded(self, tmp_path):
        settings = make_password_config(tmp_path, "my-plain-pass")
        assert settings.panel_password == "my-plain-pass"
        assert settings.password_config_source.name == "key_pool_config.json"

    def test_local_config_source_tracked(self, tmp_path):
        """panel_password 写在 local 覆盖文件里时，清空也清 local。"""
        (tmp_path / "key_pool_config.json").write_text(
            json.dumps({"panel_password": "from-main"}), encoding="utf-8")
        (tmp_path / "key_pool_config.local.json").write_text(
            json.dumps({"panel_password": "from-local"}), encoding="utf-8")
        from app.config import load_config
        settings = load_config(tmp_path / "key_pool_config.json")
        assert settings.panel_password == "from-local"
        assert settings.password_config_source.name == "key_pool_config.local.json"

    def test_password_type_validated(self, tmp_path):
        config_file = tmp_path / "key_pool_config.json"
        config_file.write_text(json.dumps({"panel_password": 123}), encoding="utf-8")
        from app.config import ConfigError, load_config
        with pytest.raises(ConfigError, match="panel_password"):
            load_config(config_file)


class TestStartupSync:
    def test_config_password_becomes_panel_password(self, tmp_path):
        """配置明文在启动时自动转哈希，登录即用该明文。"""
        settings = make_password_config(tmp_path, "admin123")
        app = create_app(settings)
        assert app.state.panel_auth.has_password()
        assert app.state.panel_auth.check_password("admin123")

    def test_config_password_overrides_existing(self, tmp_path):
        """已有密码的实例：配置明文与现密码不同时，重启以配置为准。"""
        settings = make_password_config(tmp_path, "new-config-pass")
        app1 = create_app(settings)
        app1.state.panel_auth.set_password("old-pass-123")
        assert not app1.state.panel_auth.check_password("new-config-pass")
        # 模拟重启
        app2 = create_app(settings)
        assert app2.state.panel_auth.check_password("new-config-pass")

    def test_no_config_password_keeps_existing(self, tmp_path):
        """配置 panel_password 为空：沿用面板里改过的密码。"""
        settings = make_password_config(tmp_path, "")
        app1 = create_app(settings)
        app1.state.panel_auth.set_password("user-set-pass")
        app2 = create_app(settings)  # 重启
        assert app2.state.panel_auth.check_password("user-set-pass")


class TestLoginAndChange:
    @pytest.fixture
    def password_app(self, tmp_path):
        settings = make_password_config(tmp_path, "admin123")
        return create_app(settings)

    def test_login_with_config_password_must_change(self, password_app, tmp_path):
        from fastapi.testclient import TestClient
        with TestClient(password_app) as client:
            # 错误密码
            r = client.post("/admin/panel/login", json={"password": "wrong"})
            assert r.status_code == 401
            # 正确密码（配置明文）→ must_change=True
            r = client.post("/admin/panel/login", json={"password": "admin123"})
            assert r.status_code == 200
            assert r.json()["must_change"] is True

    def test_change_password_full_flow(self, password_app, tmp_path):
        from fastapi.testclient import TestClient
        with TestClient(password_app) as client:
            token = client.post("/admin/panel/login",
                                json={"password": "admin123"}).json()["token"]
            # 旧密码错 → 401
            r = client.post("/admin/panel/change-password",
                            headers={"X-Panel-Token": token},
                            json={"old_password": "wrong", "new_password": "my-own-999"})
            assert r.status_code == 401
            # 新密码太短 → 400
            r = client.post("/admin/panel/change-password",
                            headers={"X-Panel-Token": token},
                            json={"old_password": "admin123", "new_password": "123"})
            assert r.status_code == 400
            # 无 token → 401
            r = client.post("/admin/panel/change-password",
                            json={"old_password": "admin123", "new_password": "my-own-999"})
            assert r.status_code == 401
            # 正常改密
            r = client.post("/admin/panel/change-password",
                            headers={"X-Panel-Token": token},
                            json={"old_password": "admin123", "new_password": "my-own-999"})
            assert r.status_code == 200
            new_token = r.json()["token"]

            # 新密码登录 → must_change=False（配置已清空）
            r = client.post("/admin/panel/login", json={"password": "my-own-999"})
            assert r.status_code == 200
            assert r.json()["must_change"] is False

            # 配置文件里的 panel_password 已被清空
            cfg = json.loads(
                (tmp_path / "key_pool_config.json").read_text(encoding="utf-8"))
            assert cfg["panel_password"] == ""

            # 旧 token 已失效
            r = client.get("/admin/stats", headers={"X-Panel-Token": token})
            assert r.status_code == 401
            # 新 token 可用
            r = client.get("/admin/stats", headers={"X-Panel-Token": new_token})
            assert r.status_code == 200

            # 重启后（配置已空）新密码仍有效
            settings2 = make_password_config(tmp_path, "")
            app2 = create_app(settings2)
            assert app2.state.panel_auth.check_password("my-own-999")
