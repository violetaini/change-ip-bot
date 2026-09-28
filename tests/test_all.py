import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

# Ensure src is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

# Mock external packages if not installed in current environment
for mod in ["loguru", "telegram", "telegram.ext"]:
    if mod not in sys.modules:
        try:
            __import__(mod)
        except ImportError:
            sys.modules[mod] = MagicMock()

from utils.network import (
    ChangeIPTimeoutError,
    call_boil_change_ip,
    call_boil_get_ip,
    call_change_ip_api,
    is_valid_ipv4,
    parse_change_ip_result,
)
from utils.redact import redact_text
from utils.state import (
    load_state,
    save_state,
    update_change_state,
    update_state_keys,
)
from services.ip_change_service import (
    ChangeResult,
    get_ip_change_provider_name,
    perform_ip_change,
)
from config import DEFAULT_CONFIG, load_config


class TestConfigAndRedact(unittest.TestCase):
    def test_default_config_fields(self):
        self.assertIn("ip_change_provider", DEFAULT_CONFIG)
        self.assertIn("boil_api_base_url", DEFAULT_CONFIG)
        self.assertIn("boil_api_token", DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["ip_change_provider"], "classic")

    def test_redact_sensitive_tokens(self):
        text = "Request with Bearer my_secret_token_123456789 and telegram_bot_token=123456:abcdefghijklmnopqrstuvwxyz"
        redacted = redact_text(text)
        self.assertNotIn("my_secret_token_123456789", redacted)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", redacted)
        self.assertIn("<redacted>", redacted)


class TestStateManagement(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmp_dir, "state.json")
        os.environ["VPS_IP_BOT_STATE_FILE"] = self.state_file

    def tearDown(self):
        if os.path.exists(self.state_file):
            os.remove(self.state_file)
        if os.path.isdir(self.tmp_dir):
            os.rmdir(self.tmp_dir)

    def test_default_state_fields(self):
        st = load_state()
        self.assertEqual(st.get("boil_next_allowed_at"), 0)
        self.assertEqual(st.get("boil_uses_left"), -1)

    def test_update_state_keys_atomic(self):
        update_state_keys({"boil_next_allowed_at": 1782732942, "boil_uses_left": 3})
        st = load_state()
        self.assertEqual(st.get("boil_next_allowed_at"), 1782732942)
        self.assertEqual(st.get("boil_uses_left"), 3)


class TestBoilNetworkUtils(unittest.TestCase):
    def test_is_valid_ipv4(self):
        self.assertTrue(is_valid_ipv4("1.1.1.1"))
        self.assertTrue(is_valid_ipv4("192.168.1.100"))
        self.assertFalse(is_valid_ipv4("256.0.0.1"))
        self.assertFalse(is_valid_ipv4("abc"))
        self.assertFalse(is_valid_ipv4(""))

    @patch("requests.post")
    def test_call_boil_change_ip_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "ok": True,
            "message": "正在執行更換IP",
            "uses_left": 2,
            "next_allowed_at": 1782732942,
        }
        mock_post.return_value = mock_resp

        res = call_boil_change_ip("https://ippanel.boil.network", "test_token")
        self.assertTrue(res["ok"])
        self.assertEqual(res["uses_left"], 2)
        self.assertEqual(res["next_allowed_at"], 1782732942)

        # Check headers
        called_headers = mock_post.call_args[1]["headers"]
        self.assertEqual(called_headers["Authorization"], "Bearer test_token")

    @patch("requests.post")
    def test_call_boil_change_ip_error_400(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.ok = False
        mock_resp.json.return_value = {
            "error": "當日更換IP次數已用完：5/5次"
        }
        mock_post.return_value = mock_resp

        with self.assertRaises(RuntimeError) as ctx:
            call_boil_change_ip("https://ippanel.boil.network", "test_token")
        self.assertIn("當日更換IP次數已用完", str(ctx.exception))

    @patch("requests.post")
    def test_call_boil_change_ip_error_405(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 405
        mock_resp.ok = False
        mock_resp.json.return_value = {
            "error": "僅允許POST"
        }
        mock_post.return_value = mock_resp

        with self.assertRaises(RuntimeError) as ctx:
            call_boil_change_ip("https://ippanel.boil.network", "test_token")
        self.assertIn("僅允許POST", str(ctx.exception))

    @patch("requests.post")
    def test_call_boil_get_ip_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.json.return_value = {"ok": True, "ip": "103.150.12.34"}
        mock_post.return_value = mock_resp

        ip = call_boil_get_ip("https://ippanel.boil.network", "test_token")
        self.assertEqual(ip, "103.150.12.34")


class TestIPChangeService(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmp_dir, "state.json")
        os.environ["VPS_IP_BOT_STATE_FILE"] = self.state_file

    def tearDown(self):
        if os.path.exists(self.state_file):
            os.remove(self.state_file)
        if os.path.isdir(self.tmp_dir):
            os.rmdir(self.tmp_dir)

    async def test_boil_cooldown_interception(self):
        from config import config
        config["ip_change_provider"] = "boil"
        config["boil_api_token"] = "valid_token"

        future_timestamp = time.time() + 120
        update_state_keys({"boil_next_allowed_at": future_timestamp})

        result = await perform_ip_change(trigger="test")
        self.assertFalse(result.success)
        self.assertEqual(result.status, "COOLDOWN_PROTECTION")
        self.assertIn("频率限制冷却中", result.message)

    async def test_boil_missing_token(self):
        from config import config
        config["ip_change_provider"] = "boil"
        config["boil_api_token"] = ""
        update_state_keys({"boil_next_allowed_at": 0})

        result = await perform_ip_change(trigger="test")
        self.assertFalse(result.success)
        self.assertEqual(result.status, "CONFIG_ERROR")
        self.assertIn("boil_api_token", result.message)

    @patch("services.ip_change_service.call_boil_change_ip")
    @patch("services.ip_change_service.call_boil_get_ip")
    @patch("services.ip_change_service._wait_for_boil_ip_change")
    @patch("services.ip_change_service._update_dns_safely")
    async def test_boil_success_flow(self, mock_dns, mock_wait, mock_get_ip, mock_api):
        from config import config
        config["ip_change_provider"] = "boil"
        config["boil_api_token"] = "valid_token"
        update_state_keys({"boil_next_allowed_at": 0})

        mock_get_ip.return_value = "1.1.1.1"
        mock_api.return_value = {
            "ok": True,
            "message": "正在執行更換IP",
            "uses_left": 3,
            "next_allowed_at": time.time() + 300,
        }
        mock_wait.return_value = "2.2.2.2"
        mock_dns.return_value = "DNS OK"

        result = await perform_ip_change(trigger="test")
        self.assertTrue(result.success)
        self.assertEqual(result.status, "BOIL_SUCCESS")
        self.assertEqual(result.old_ip, "1.1.1.1")
        self.assertEqual(result.new_ip, "2.2.2.2")
        self.assertIn("今日剩余配额: 3次", result.message)

        st = load_state()
        self.assertEqual(st.get("boil_uses_left"), 3)


class TestBotHelpers(unittest.TestCase):
    def test_admin_id_parsing(self):
        from bot import _get_admin_id_list, _get_super_admin_id_list
        from config import config

        # Test when None
        config["telegram_admin_user_ids"] = None
        config["telegram_super_admin_user_ids"] = None
        self.assertEqual(_get_admin_id_list(), [])
        self.assertEqual(_get_super_admin_id_list(), [])

        # Test when empty string
        config["telegram_admin_user_ids"] = ""
        self.assertEqual(_get_admin_id_list(), [])

        # Test when normal IDs
        config["telegram_admin_user_ids"] = " 123456 , 789012 "
        self.assertEqual(_get_admin_id_list(), ["123456", "789012"])

    def test_persist_config_value(self):
        from bot import persist_config_value
        from config import config

        tmp_yaml = tempfile.NamedTemporaryFile("w+", delete=False, suffix=".yaml", encoding="utf-8")
        tmp_yaml.write("ip_change_provider: classic\nboil_api_token: \"\"\n# comments\n")
        tmp_yaml.close()

        try:
            config["_loaded_from"] = tmp_yaml.name
            persist_config_value("ip_change_provider", "boil")
            persist_config_value("boil_api_token", "my_new_token")

            with open(tmp_yaml.name, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertIn('ip_change_provider: "boil"', content)
            self.assertIn('boil_api_token: "my_new_token"', content)
            self.assertIn("# comments", content)

            # Test that YAML parses it accurately back to strings
            import yaml
            parsed = yaml.safe_load(content)
            self.assertEqual(parsed["ip_change_provider"], "boil")
            self.assertEqual(parsed["boil_api_token"], "my_new_token")
        finally:
            if os.path.exists(tmp_yaml.name):
                os.remove(tmp_yaml.name)



if __name__ == "__main__":
    unittest.main()
