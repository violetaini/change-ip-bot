import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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
        self.assertIn("remote_ssh_enabled", DEFAULT_CONFIG)
        self.assertIn("remote_ssh_host", DEFAULT_CONFIG)
        self.assertIn("remote_ssh_port", DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["ip_change_provider"], "generic")
        self.assertFalse(DEFAULT_CONFIG["remote_ssh_enabled"])

    def test_redact_sensitive_tokens(self):
        text = "Request with Bearer my_secret_token_123456789 and telegram_bot_token=123456:abcdefghijklmnopqrstuvwxyz and remote_ssh_password=super_secret_ssh_pass"
        redacted = redact_text(text)
        self.assertNotIn("my_secret_token_123456789", redacted)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", redacted)
        self.assertNotIn("super_secret_ssh_pass", redacted)
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
        from config import config
        self._orig_config = config.copy()
        self.tmp_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmp_dir, "state.json")
        os.environ["VPS_IP_BOT_STATE_FILE"] = self.state_file

    def tearDown(self):
        from config import config
        config.clear()
        config.update(self._orig_config)
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

    @patch("requests.get")
    @patch("services.ip_change_service.get_active_public_ipv4")
    @patch("services.ip_change_service._update_dns_safely")
    async def test_generic_mode_success_flow(self, mock_dns, mock_get_ip, mock_get_req):
        from config import config
        config["ip_change_provider"] = "generic"
        config["remote_ssh_enabled"] = False
        config["ip_change_api"] = "https://api.example.com/reconnect"
        config["ip_change_interval"] = 0
        config["ip_change_poll_retries"] = 3
        config["ip_change_poll_delay"] = 0

        # First call is pre-check (1.1.1.1), second call is polling (2.2.2.2)
        mock_get_ip.side_effect = ["1.1.1.1", "2.2.2.2"]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "OK"
        mock_get_req.return_value = mock_resp
        mock_dns.return_value = "DNS OK"

        result = await perform_ip_change(trigger="test")
        self.assertTrue(result.success)
        self.assertEqual(result.status, "IP_CHANGED")
        self.assertEqual(result.old_ip, "1.1.1.1")
        self.assertEqual(result.new_ip, "2.2.2.2")
        self.assertIn("curl -4 ip.sb", result.message)

    @patch("services.ip_change_service.call_change_ip_api")
    @patch("services.ip_change_service._verify_changed_ip")
    @patch("services.ip_change_service._update_dns_safely")
    async def test_fachost_mode_success_flow(self, mock_dns, mock_verify, mock_api):
        from config import config
        config["ip_change_provider"] = "fachost"
        config["remote_ssh_enabled"] = False
        config["ip_change_api"] = "https://panel.fachost.com/change_ip"
        config["ip_change_interval"] = 0

        mock_api.return_value = {
            "status": "IP changed",
            "old_ip": "1.1.1.1",
            "new_ip": "2.2.2.2",
        }
        mock_verify.return_value = True
        mock_dns.return_value = "DNS OK"

        result = await perform_ip_change(trigger="test")
        self.assertTrue(result.success)
        self.assertEqual(result.status, "IP changed")
        self.assertEqual(result.new_ip, "2.2.2.2")
        self.assertEqual(result.old_ip, "1.1.1.1")

    @patch("services.ip_change_service.run_remote_ssh_command")
    @patch("services.ip_change_service.get_active_public_ipv4")
    @patch("services.ip_change_service._update_dns_safely")
    async def test_generic_mode_lan_api_via_ssh(self, mock_dns, mock_get_ip, mock_ssh):
        from config import config
        from services.ip_change_service import is_private_or_local_target

        self.assertTrue(is_private_or_local_target("http://192.168.1.1/reconnect"))
        self.assertTrue(is_private_or_local_target("http://127.0.0.1:8080/reconnect"))
        self.assertTrue(is_private_or_local_target("http://router.local/api"))
        self.assertFalse(is_private_or_local_target("https://api.ipify.org"))
        self.assertFalse(is_private_or_local_target("https://panel.fachost.com/api"))

        config["ip_change_provider"] = "generic"
        config["remote_ssh_enabled"] = True
        config["ip_change_api"] = "http://192.168.1.1/cgi-bin/reconnect"
        config["ip_change_interval"] = 0
        config["ip_change_poll_retries"] = 2
        config["ip_change_poll_delay"] = 0

        mock_get_ip.side_effect = ["1.1.1.1", "2.2.2.2"]
        mock_ssh.return_value = (0, "OK")
        mock_dns.return_value = "DNS OK"

        result = await perform_ip_change(trigger="test")
        self.assertTrue(result.success)
        self.assertEqual(result.new_ip, "2.2.2.2")
        # Verify SSH was called for the LAN API trigger
        ssh_calls = [c[0][0] for c in mock_ssh.call_args_list]
        self.assertTrue(any("192.168.1.1" in c for c in ssh_calls))

    @patch("services.ip_change_service.update_dns_if_enabled")
    def test_ssh_mode_skips_ddns_for_generic_and_fachost(self, mock_update_dns):
        from config import config
        from services.ip_change_service import _update_dns_safely

        # 1. SSH is enabled, provider is generic -> DDNS skipped
        config["remote_ssh_enabled"] = True
        config["ip_change_provider"] = "generic"
        msg = _update_dns_safely("1.2.3.4")
        self.assertIn("远程 SSH 模式下不执行 DDNS 更新", msg)
        mock_update_dns.assert_not_called()

        # 2. SSH is enabled, provider is fachost -> DDNS skipped
        config["ip_change_provider"] = "fachost"
        msg2 = _update_dns_safely("1.2.3.4")
        self.assertIn("远程 SSH 模式下不执行 DDNS 更新", msg2)
        mock_update_dns.assert_not_called()

        # 3. SSH is enabled, provider is boil -> DDNS is allowed
        config["ip_change_provider"] = "boil"
        mock_update_dns.return_value = "DNS updated successfully"
        msg3 = _update_dns_safely("1.2.3.4")
        self.assertEqual(msg3, "DNS updated successfully")
        mock_update_dns.assert_called_once_with("1.2.3.4")

        # 4. SSH is disabled, provider is generic -> DDNS is allowed
        mock_update_dns.reset_mock()
        config["remote_ssh_enabled"] = False
        config["ip_change_provider"] = "generic"
        mock_update_dns.return_value = "DNS updated successfully"
        msg4 = _update_dns_safely("1.2.3.4")
        self.assertEqual(msg4, "DNS updated successfully")
        mock_update_dns.assert_called_once_with("1.2.3.4")


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


class TestSystemDeps(unittest.TestCase):
    def test_get_system_dependency_status(self):
        from utils.system_deps import get_system_dependency_status
        status = get_system_dependency_status()
        self.assertIn("cairo", status)
        self.assertIn("font", status)
        self.assertIn("curl", status)
        self.assertIn("speedtest", status)
        self.assertIn("is_root", status)

    @patch("utils.system_deps.platform.system")
    def test_ensure_system_dependencies_non_linux(self, mock_platform):
        mock_platform.return_value = "Windows"
        import utils.system_deps as sd
        sd._AUTO_INSTALL_ATTEMPTED = False
        res = sd.ensure_system_dependencies()
        self.assertEqual(res.get("status"), "skipped")
        self.assertEqual(res.get("reason"), "non-linux")

    @patch("utils.system_deps.platform.system")
    @patch("utils.system_deps.is_root_user")
    @patch("utils.system_deps.get_system_dependency_status")
    def test_ensure_system_dependencies_not_root(self, mock_status, mock_root, mock_platform):
        mock_platform.return_value = "Linux"
        mock_root.return_value = False
        mock_status.return_value = {
            "cairo": {"ok": False, "detail": "missing"},
            "font": {"ok": False, "detail": "missing"},
            "curl": {"ok": False, "detail": "missing"},
        }
        import utils.system_deps as sd
        sd._AUTO_INSTALL_ATTEMPTED = False
        res = sd.ensure_system_dependencies()
        self.assertEqual(res.get("status"), "skipped")
        self.assertEqual(res.get("reason"), "not_root")


class TestQualityDegradation(unittest.IsolatedAsyncioTestCase):
    @patch("handlers.ip_quality.check_user_permission")
    @patch("handlers.ip_quality.run_quality_command")
    @patch("handlers.ip_quality.render_svg_url_to_png")
    async def test_ip_quality_render_fail_graceful_degradation(self, mock_render, mock_cmd, mock_perm):
        mock_perm.return_value = True
        mock_cmd.return_value = (0, "Check passed: https://ip.check.place/report.svg")
        mock_render.side_effect = RuntimeError("libcairo.so.2 not found")

        from handlers.ip_quality import ip_quality_handler

        update = MagicMock()
        update.effective_user.id = 123456
        update.effective_user.username = "test"
        update.effective_user.full_name = "Test User"
        update.message.reply_text = MagicMock()
        # Async mock for reply_text
        fut = asyncio.Future()
        fut.set_result(None)
        update.message.reply_text.return_value = fut

        context = MagicMock()

        await ip_quality_handler(update, context)

        # Ensure reply_text was called with the fallback link
        found_link = False
        for call in update.message.reply_text.call_args_list:
            arg = str(call[0][0]) if call[0] else ""
            if "https://ip.check.place/report.svg" in arg and "自动降级为报告链接" in arg:
                found_link = True
                break
        self.assertTrue(found_link, "Fallback message should contain SVG link and downgrade notice")

    def test_patch_svg_cjk_font(self):
        from handlers.ip_quality import patch_svg_cjk_font
        raw_svg = b'<svg><style>* { font-family: SimHei, Consolas, DejaVu Sans Mono, monospace; }</style></svg>'
        patched = patch_svg_cjk_font(raw_svg)
        self.assertIn(b"WenQuanYi Zen Hei Mono", patched)
        self.assertIn(b"DejaVu Sans Mono", patched)

    def test_extract_svg_urls_dual_stack(self):
        from handlers.ip_quality import extract_svg_url, extract_svg_urls
        sample_output = (
            "Testing IPv4...\n"
            "Report: https://Report.Check.Place/ip/ABC123V4.svg\n"
            "Testing IPv6...\n"
            "Report: https://Report.Check.Place/ip/XYZ789V6.svg\n"
        )
        urls = extract_svg_urls(sample_output)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0], "https://Report.Check.Place/ip/ABC123V4.svg")
        self.assertEqual(urls[1], "https://Report.Check.Place/ip/XYZ789V6.svg")
        self.assertEqual(extract_svg_url(sample_output), "https://Report.Check.Place/ip/ABC123V4.svg")

    @patch("handlers.ip_quality.check_user_permission")
    @patch("handlers.ip_quality.run_quality_command")
    @patch("handlers.ip_quality.render_svg_url_to_png")
    @patch("handlers.ip_quality.crop_report_area")
    async def test_ip_quality_dual_stack_send_media_group(self, mock_crop, mock_render, mock_cmd, mock_perm):
        mock_perm.return_value = True
        mock_cmd.return_value = (
            0,
            "IPv4: https://Report.Check.Place/ip/V4.svg\nIPv6: https://Report.Check.Place/ip/V6.svg"
        )
        def create_dummy_jpg(png_p, jpg_p):
            with open(jpg_p, "wb") as f:
                f.write(b"dummy_jpg_bytes")
        mock_crop.side_effect = create_dummy_jpg

        from handlers.ip_quality import ip_quality_handler

        update = MagicMock()
        update.effective_user.id = 123456
        update.effective_user.username = "test"
        update.effective_user.full_name = "Test User"
        update.message.reply_text = MagicMock()
        fut_text = asyncio.Future()
        fut_text.set_result(None)
        update.message.reply_text.return_value = fut_text

        update.message.reply_media_group = MagicMock()
        fut_mg = asyncio.Future()
        fut_mg.set_result(None)
        update.message.reply_media_group.return_value = fut_mg

        context = MagicMock()
        context.args = []

        await ip_quality_handler(update, context)

        update.message.reply_media_group.assert_called_once()
        called_media = update.message.reply_media_group.call_args[1]["media"]
        self.assertEqual(len(called_media), 2)
        from telegram import InputMediaPhoto
        if hasattr(InputMediaPhoto, "call_args_list") and InputMediaPhoto.call_args_list:
            c0 = InputMediaPhoto.call_args_list[-2][1].get("caption", "")
            c1 = InputMediaPhoto.call_args_list[-1][1].get("caption", "")
            self.assertIn("IPv4", c0)
            self.assertIn("IPv6", c1)
        else:
            self.assertIn("IPv4", str(called_media[0].caption))
            self.assertIn("IPv6", str(called_media[1].caption))


class TestRemoteSSH(unittest.TestCase):
    def setUp(self):
        from config import config
        self._orig_config = config.copy()
        config["ip_change_provider"] = "generic"
        from utils.remote_ssh import _DNS_LOCAL_CACHE
        _DNS_LOCAL_CACHE.clear()

    def tearDown(self):
        from config import config
        config.clear()
        config.update(self._orig_config)
        from utils.remote_ssh import _DNS_LOCAL_CACHE
        _DNS_LOCAL_CACHE.clear()

    def test_dns_local_cache(self):
        from utils.remote_ssh import get_cached_host_ip, set_cached_host_ip
        self.assertEqual(get_cached_host_ip("residential.example.com"), "")
        set_cached_host_ip("RESIDENTIAL.example.com.", "198.51.100.1")
        self.assertEqual(get_cached_host_ip("residential.example.com"), "198.51.100.1")

    @patch("requests.get")
    def test_resolve_via_cloudflare_doh_success(self, mock_get):
        from utils.remote_ssh import resolve_via_cloudflare_doh
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "Status": 0,
            "Answer": [
                {"name": "residential.example.com.", "type": 1, "TTL": 60, "data": "198.51.100.1"}
            ]
        }
        mock_get.return_value = mock_resp

        ip = resolve_via_cloudflare_doh("residential.example.com")
        self.assertEqual(ip, "198.51.100.1")
        mock_get.assert_called_once_with(
            "https://1.1.1.1/dns-query?name=residential.example.com&type=A",
            headers={"Accept": "application/dns-json"},
            timeout=5,
        )

    @patch("requests.get")
    def test_resolve_via_cloudflare_doh_failure(self, mock_get):
        from utils.remote_ssh import resolve_via_cloudflare_doh
        mock_get.side_effect = Exception("network timeout")
        ip = resolve_via_cloudflare_doh("residential.example.com")
        self.assertEqual(ip, "")

    @patch("utils.remote_ssh.resolve_via_cloudflare_doh")
    def test_resolve_target_host(self, mock_doh):
        from utils.remote_ssh import get_cached_host_ip, resolve_target_host, set_cached_host_ip
        # 1. Direct IP
        self.assertEqual(resolve_target_host("1.2.3.4"), "1.2.3.4")

        # 2. Local cache hit
        set_cached_host_ip("mytest.com", "9.9.9.9")
        self.assertEqual(resolve_target_host("mytest.com"), "9.9.9.9")
        mock_doh.assert_not_called()

        # 3. DoH resolution fallback
        mock_doh.return_value = "8.8.8.8"
        self.assertEqual(resolve_target_host("newhost.com"), "8.8.8.8")
        self.assertEqual(get_cached_host_ip("newhost.com"), "8.8.8.8")

    def test_build_ssh_command_prefix(self):
        from utils.remote_ssh import build_ssh_command_prefix
        cfg = {
            "host": "1.2.3.4",
            "port": 2222,
            "user": "root",
            "key_path": "",
        }
        cmd = build_ssh_command_prefix(cfg)
        self.assertIn("ssh", cmd)
        self.assertIn("-p", cmd)
        self.assertIn("2222", cmd)
        self.assertIn("StrictHostKeyChecking=no", cmd)
        self.assertIn("UserKnownHostsFile=/dev/null", cmd)
        self.assertIn("root@1.2.3.4", cmd)

    @patch("subprocess.run")
    def test_run_remote_ssh_command(self, mock_run):
        from config import config
        from utils.remote_ssh import run_remote_ssh_command
        config["remote_ssh_host"] = "1.2.3.4"
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "output text"
        mock_res.stderr = ""
        mock_run.return_value = mock_res

        code, out = run_remote_ssh_command("uname -a")
        self.assertEqual(code, 0)
        self.assertEqual(out, "output text")

    def test_dns_local_cache_ttl(self):
        from utils.remote_ssh import get_cached_host_ip, set_cached_host_ip
        set_cached_host_ip("node.example.com", "1.1.1.1")
        # Within TTL
        self.assertEqual(get_cached_host_ip("node.example.com", max_age=10), "1.1.1.1")
        # Expired TTL
        self.assertEqual(get_cached_host_ip("node.example.com", max_age=-1), "")

    def test_invalidate_cached_host_ip(self):
        from utils.remote_ssh import get_cached_host_ip, invalidate_cached_host_ip, set_cached_host_ip
        set_cached_host_ip("node.example.com", "1.1.1.1")
        invalidate_cached_host_ip("node.example.com")
        self.assertEqual(get_cached_host_ip("node.example.com"), "")

    @patch("utils.remote_ssh.resolve_via_cloudflare_doh")
    def test_resolve_target_host_force_refresh(self, mock_doh):
        from utils.remote_ssh import get_cached_host_ip, resolve_target_host, set_cached_host_ip
        set_cached_host_ip("fresh.example.com", "10.0.0.1")
        mock_doh.return_value = "10.0.0.2"

        # Without force_refresh, returns cached
        self.assertEqual(resolve_target_host("fresh.example.com", force_refresh=False), "10.0.0.1")
        mock_doh.assert_not_called()

        # With force_refresh, calls resolver and updates cache
        self.assertEqual(resolve_target_host("fresh.example.com", force_refresh=True), "10.0.0.2")
        self.assertEqual(get_cached_host_ip("fresh.example.com"), "10.0.0.2")

    @patch("utils.network.call_boil_get_ip")
    def test_resolve_target_host_boil_mode(self, mock_boil_get):
        from config import config
        from utils.remote_ssh import resolve_target_host
        config["ip_change_provider"] = "boil"
        config["boil_api_token"] = "fake_token"
        mock_boil_get.return_value = "103.150.12.34"

        ip = resolve_target_host("dynamic.boil.net", force_refresh=True)
        self.assertEqual(ip, "103.150.12.34")
        mock_boil_get.assert_called_once()

    @patch("subprocess.run")
    @patch("utils.remote_ssh.resolve_target_host")
    def test_run_remote_ssh_command_self_healing(self, mock_resolve, mock_run):
        from config import config
        from utils.remote_ssh import run_remote_ssh_command
        config["remote_ssh_host"] = "dynamic.example.com"

        # 1st call: initial resolve -> old IP
        # 2nd call: force_refresh on failure -> new IP
        # 3rd call: initial resolve on retry -> new IP
        mock_resolve.side_effect = ["192.168.1.1", "192.168.1.2", "192.168.1.2"]

        # First subprocess run returns code 255 (SSH failure), second run returns code 0 (success)
        fail_res = MagicMock(returncode=255, stdout="", stderr="Connection refused")
        succ_res = MagicMock(returncode=0, stdout="success output", stderr="")
        mock_run.side_effect = [fail_res, succ_res]

        code, output = run_remote_ssh_command("uname -a")
        self.assertEqual(code, 0)
        self.assertEqual(output, "success output")
        self.assertEqual(mock_run.call_count, 2)


class TestStreamCheckDualStack(unittest.TestCase):
    def test_split_sections_and_summary_dual_stack(self):
        from handlers.stream_check import build_stream_summary, split_sections_by_ip_version
        sample_output = (
            " ** 正在测试IPv4解锁情况 \n"
            "--------------------------------\n"
            " ** 您的网络为: AS4760 HKTIMS-AP (218.103.206.0/24)\n"
            " Netflix: Yes (Region: HK)\n"
            " Disney+: Yes (Region: HK)\n"
            " YouTube Premium: Yes (Region: HK)\n"
            " ** 正在测试IPv6解锁情况 \n"
            "--------------------------------\n"
            " ** 您的网络为: AS4760 HKTIMS-AP (2404:c804::/32)\n"
            " Netflix: Yes (Region: HK)\n"
            " Disney+: Yes (Region: HK)\n"
            " YouTube Premium: Yes (Region: HK)\n"
            "本次测试已结束\n"
        )
        summary = build_stream_summary(0, sample_output, 18.5)
        self.assertIn("双栈", summary)
        self.assertIn("【IPv4 解锁结果】", summary)
        self.assertIn("218.103.206.0/24", summary)
        self.assertIn("【IPv6 解锁结果】", summary)
        self.assertIn("2404:c804::/32", summary)
        self.assertIn("Netflix: Yes (Region: HK)", summary)

    def test_single_stack_v4_summary(self):
        from handlers.stream_check import build_stream_summary
        sample_output = (
            " ** 正在测试IPv4解锁情况 \n"
            " ** 您的网络为: AS12345 TestISP (1.2.3.4)\n"
            " Netflix: Yes (Region: US)\n"
        )
        summary = build_stream_summary(0, sample_output, 10.0)
        self.assertIn("单栈 IPv4", summary)
        self.assertIn("【IPv4 解锁结果】", summary)
        self.assertNotIn("【IPv6 解锁结果】", summary)


class TestPingDualStack(unittest.TestCase):
    def test_parse_ping_params_defaults(self):
        from handlers.ping import parse_ping_params
        target, count, ip_version, warning = parse_ping_params([])
        self.assertEqual(target, "1.1.1.1")
        self.assertEqual(count, 10)
        self.assertEqual(ip_version, 4)
        self.assertEqual(warning, "")

    def test_parse_ping_params_v6_flag(self):
        from handlers.ping import parse_ping_params
        target, count, ip_version, warning = parse_ping_params(["-6"])
        self.assertEqual(target, "2606:4700:4700::1111")
        self.assertEqual(count, 10)
        self.assertEqual(ip_version, 6)

        target, count, ip_version, warning = parse_ping_params(["-6", "2400:3200::1"])
        self.assertEqual(target, "2400:3200::1")
        self.assertEqual(ip_version, 6)

    def test_parse_ping_params_auto_detect_colon(self):
        from handlers.ping import parse_ping_params
        target, count, ip_version, warning = parse_ping_params(["2001:4860:4860::8888"])
        self.assertEqual(target, "2001:4860:4860::8888")
        self.assertEqual(ip_version, 6)

    def test_parse_ping_params_count_and_custom(self):
        from handlers.ping import parse_ping_params
        target, count, ip_version, warning = parse_ping_params(["-c", "5", "-6", "2400:3200::1"])
        self.assertEqual(target, "2400:3200::1")
        self.assertEqual(count, 5)
        self.assertEqual(ip_version, 6)

        # Count clamp
        target, count, ip_version, warning = parse_ping_params(["-c", "150"])
        self.assertEqual(count, 100)
        self.assertIn("最大值 100", warning)

    def test_format_ping_result_linux(self):
        from handlers.ping import format_ping_result
        sample_output = (
            "PING 2606:4700:4700::1111 (2606:4700:4700::1111) 56 data bytes\n"
            "64 bytes from 2606:4700:4700::1111: icmp_seq=1 ttl=58 time=3.46 ms\n"
            "--- 2606:4700:4700::1111 ping statistics ---\n"
            "3 packets transmitted, 3 received, 0% packet loss, time 2003ms\n"
            "rtt min/avg/max/mdev = 3.459/3.733/4.185/0.321 ms\n"
        )
        msg = format_ping_result("2606:4700:4700::1111", 6, sample_output)
        self.assertIn("Ping 结果 (2606:4700:4700::1111 [IPv6])", msg)
        self.assertIn("发送: 3", msg)
        self.assertIn("接收: 3", msg)
        self.assertIn("丢包率: 0%", msg)
        self.assertIn("平均: 3.733 ms", msg)

    def test_format_ping_result_fallback_unreachable(self):
        from handlers.ping import format_ping_result
        err_output = "ping: connect: Network is unreachable"
        msg = format_ping_result("2606:4700:4700::1111", 6, err_output)
        self.assertEqual(msg, "ping: connect: Network is unreachable")


class TestSpeedtestFormatting(unittest.TestCase):
    def test_format_speedtest_result_ipv6(self):
        from handlers.speedtest import format_speedtest_result
        data = {
            "server": {"name": "Misaka", "location": "Hong Kong", "country": "Hong Kong"},
            "download": {"bandwidth": 50000000},
            "upload": {"bandwidth": 25000000},
            "ping": {"latency": 4.5},
            "result": {"url": "https://speedtest.net/result/123"},
            "interface": {"externalIp": "2001:db8::1"},
        }
        res = format_speedtest_result(data)
        self.assertIn("客户端出口: 2001:db8::1 (IPv6)", res)
        self.assertIn("400.00 Mbps", res)
        self.assertIn("200.00 Mbps", res)
        self.assertIn("4.50 ms", res)
        self.assertIn("https://speedtest.net/result/123", res)

    def test_format_speedtest_result_ipv4(self):
        from handlers.speedtest import format_speedtest_result
        data = {
            "server": {"name": "STC", "location": "Hong Kong", "country": "Hong Kong"},
            "download": {"bandwidth": 12500000},
            "upload": {"bandwidth": 12500000},
            "ping": {"latency": 2.1},
            "result": {"url": "https://speedtest.net/result/456"},
            "interface": {"externalIp": "198.51.100.1"},
        }
        res = format_speedtest_result(data)
        self.assertIn("客户端出口: 198.51.100.1 (IPv4)", res)
        self.assertIn("100.00 Mbps", res)
        self.assertIn("100.00 Mbps", res)

    def test_format_speedtest_result_missing_interface(self):
        from handlers.speedtest import format_speedtest_result
        data = {}
        res = format_speedtest_result(data)
        self.assertIn("测速结果:", res)
        self.assertNotIn("客户端出口:", res)
        self.assertIn("0.00 Mbps", res)


class TestMainlandDomesticProbe(unittest.TestCase):
    def test_resolve_mainland_target(self):
        from utils.network import resolve_mainland_target
        v4, v6 = resolve_mainland_target()
        self.assertTrue(bool(v4))
        self.assertTrue(bool(v6))
        self.assertIn(".", v4)
        self.assertIn(":", v6)

    def test_probe_domestic_http_retry_success(self):
        from utils.network import probe_domestic_http
        mock_ssh = MagicMock()
        # 1st attempt fails, 2nd attempt succeeds with HTTP 200 and 150ms latency
        mock_ssh.side_effect = [
            (1, ""),
            (0, "200|0.150234"),
        ]
        ok, desc, ms = probe_domestic_http("198.51.100.1", 4, retries=3, timeout=2, run_ssh_fn=mock_ssh)
        self.assertTrue(ok)
        self.assertEqual(ms, 150)
        self.assertIn("正常 (HTTP 200, 握手 150ms)", desc)
        self.assertEqual(mock_ssh.call_count, 2)

    def test_probe_domestic_http_all_failed(self):
        from utils.network import probe_domestic_http
        mock_ssh = MagicMock()
        mock_ssh.return_value = (1, "")
        ok, desc, ms = probe_domestic_http("198.51.100.1", 4, retries=3, timeout=1, run_ssh_fn=mock_ssh)
        self.assertFalse(ok)
        self.assertEqual(ms, 0)
        self.assertIn("重试3次均失败", desc)
        self.assertEqual(mock_ssh.call_count, 3)

    def test_probe_domestic_http_v6(self):
        from utils.network import probe_domestic_http
        mock_ssh = MagicMock()
        mock_ssh.return_value = (0, "200|0.280123")
        ok, desc, ms = probe_domestic_http("2001:db8::1", 6, retries=2, timeout=2, run_ssh_fn=mock_ssh)
        self.assertTrue(ok)
        self.assertEqual(ms, 280)
        self.assertIn("正常 (HTTP 200, 握手 280ms)", desc)



class TestMultiServerManagement(unittest.TestCase):
    def test_single_server_backward_compatibility(self):
        from config import get_server_config, get_servers, is_multi_server_mode

        cfg = {
            "telegram_bot_token": "token123",
            "telegram_chat_id": "111,222",
            "ip_change_provider": "generic",
            "ip_change_api": "https://example.com/change",
        }
        self.assertFalse(is_multi_server_mode(cfg))
        servers = get_servers(cfg)
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["id"], "default")
        self.assertEqual(servers[0]["name"], "默认服务器")
        self.assertEqual(servers[0]["ip_change_provider"], "generic")
        self.assertEqual(servers[0]["ip_change_api"], "https://example.com/change")

        # get_server_config
        self.assertIsNotNone(get_server_config("default", cfg))
        self.assertIsNone(get_server_config("other", cfg))

    def test_multi_server_configuration_and_overrides(self):
        from config import get_server_config, get_servers, is_multi_server_mode

        cfg = {
            "telegram_bot_token": "token123",
            "telegram_chat_id": "111",
            "ip_change_provider": "generic",
            "auto_change_enabled": True,
            "servers": [
                {
                    "id": "hkt",
                    "name": "香港 HKT 家宽",
                    "ip_change_provider": "generic",
                    "ip_change_api": "http://192.168.1.100/change",
                    "remote_ssh_enabled": True,
                    "remote_ssh_host": "192.168.1.100",
                    "auto_change_time": "03:30",
                },
                {
                    "id": "tokyo_boil",
                    "name": "东京 Boil 住宅",
                    "ip_change_provider": "boil",
                    "boil_api_token": "tokyo_token",
                    "auto_change_enabled": False,
                },
            ],
        }
        self.assertTrue(is_multi_server_mode(cfg))
        servers = get_servers(cfg)
        self.assertEqual(len(servers), 2)

        s1 = servers[0]
        self.assertEqual(s1["id"], "hkt")
        self.assertEqual(s1["name"], "香港 HKT 家宽")
        self.assertEqual(s1["ip_change_api"], "http://192.168.1.100/change")
        self.assertTrue(s1["remote_ssh_enabled"])
        self.assertEqual(s1["auto_change_time"], "03:30")
        self.assertEqual(s1["telegram_bot_token"], "token123")  # Inherited from root

        s2 = servers[1]
        self.assertEqual(s2["id"], "tokyo_boil")
        self.assertEqual(s2["name"], "东京 Boil 住宅")
        self.assertEqual(s2["ip_change_provider"], "boil")
        self.assertEqual(s2["boil_api_token"], "tokyo_token")
        self.assertFalse(s2["auto_change_enabled"])  # Override
        self.assertFalse(s2.get("remote_ssh_enabled", False))

        # Lookup by id (case-insensitive)
        self.assertEqual(get_server_config("HKT", cfg)["id"], "hkt")
        self.assertEqual(get_server_config("tokyo_boil", cfg)["id"], "tokyo_boil")
        self.assertIsNone(get_server_config("unknown", cfg))

    def test_state_partitioning_per_server(self):
        import tempfile
        from utils.state import (
            get_all_pending_notifications,
            get_user_selected_server,
            load_server_state,
            mark_notification_sent,
            mark_sending_notify,
            save_server_state,
            set_user_selected_server,
            update_server_change_state,
        )

        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json") as f:
            temp_state_path = f.name

        from config import config
        orig_state_file = config.get("state_file")
        config["state_file"] = temp_state_path

        try:
            # 1. Test isolated state saves
            save_server_state("srv1", {"custom_prop": 100})
            save_server_state("srv2", {"custom_prop": 200})

            s1 = load_server_state("srv1")
            s2 = load_server_state("srv2")
            self.assertEqual(s1.get("custom_prop"), 100)
            self.assertEqual(s2.get("custom_prop"), 200)

            # 2. Test update_server_change_state
            update_server_change_state(
                server_id="srv1",
                success=True,
                status="成功",
                old_ip="1.1.1.1",
                new_ip="1.1.1.2",
            )
            s1_after = load_server_state("srv1")
            s2_after = load_server_state("srv2")
            self.assertEqual(s1_after.get("last_new_ip"), "1.1.1.2")
            self.assertEqual(s2_after.get("last_new_ip"), "")

            # 3. Test pending notification across servers
            mark_sending_notify(False, server_id="srv1")
            save_server_state("srv1", {"pending_notify": True, "last_message": "Msg srv1"})
            save_server_state("srv2", {"pending_notify": True, "last_message": "Msg srv2"})

            pending_all = get_all_pending_notifications()
            p_sids = {sid for sid, _ in pending_all}
            self.assertIn("srv1", p_sids)
            self.assertIn("srv2", p_sids)

            # Mark one sent
            mark_notification_sent(server_id="srv1")
            pending_after = get_all_pending_notifications()
            p_sids_after = {sid for sid, _ in pending_after}
            self.assertNotIn("srv1", p_sids_after)
            self.assertIn("srv2", p_sids_after)

            # 4. User server selection
            set_user_selected_server(999888, "srv2")
            self.assertEqual(get_user_selected_server(999888), "srv2")
            self.assertIsNone(get_user_selected_server(111111))
        finally:
            config["state_file"] = orig_state_file
            if os.path.exists(temp_state_path):
                os.remove(temp_state_path)

    def test_per_server_change_lock_isolation(self):
        from services.ip_change_service import get_server_change_lock

        lock1 = get_server_change_lock("node_1")
        lock2 = get_server_change_lock("node_2")
        lock1_again = get_server_change_lock("node_1")

        self.assertIsNot(lock1, lock2)
        self.assertIs(lock1, lock1_again)


class TestIPChangeServiceMultiServer(unittest.IsolatedAsyncioTestCase):
    @patch("services.ip_change_service.get_last_change_time", return_value=0)
    @patch("services.ip_change_service.get_active_public_ipv4")
    @patch("services.ip_change_service._update_dns_safely", return_value="DNS 更新成功")
    @patch("services.ip_change_service.get_current_ip", return_value="10.0.0.1")
    @patch("requests.get")
    async def test_perform_ip_change_with_server_config(
        self,
        mock_requests_get,
        mock_get_ip,
        mock_update_dns,
        mock_get_active_ip,
        mock_last_time,
    ):
        from services.ip_change_service import perform_ip_change

        server_cfg = {
            "id": "tokyo_vps",
            "name": "东京 VPS 节点",
            "ip_change_provider": "generic",
            "ip_change_api": "https://example.com/tokyo/change",
            "ip_change_interval": 1,
            "ip_change_poll_delay": 0,
            "dns_update_enabled": True,
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"
        mock_requests_get.return_value = mock_resp
        mock_get_active_ip.side_effect = ["10.0.0.1", "10.0.0.2"]

        result = await perform_ip_change(trigger="manual", server_config=server_cfg)

        self.assertTrue(result.success)
        self.assertEqual(result.server_id, "tokyo_vps")
        self.assertEqual(result.server_name, "东京 VPS 节点")
        self.assertEqual(result.new_ip, "10.0.0.2")
        self.assertEqual(result.old_ip, "10.0.0.1")
        mock_requests_get.assert_called_once_with("https://example.com/tokyo/change", timeout=60)


class TestServerSelection(unittest.IsolatedAsyncioTestCase):
    @patch("config.config", {
        "servers": [
            {"id": "hkt", "name": "香港 HKT"},
            {"id": "tokyo", "name": "东京"},
        ]
    })
    async def test_resolve_target_server_by_arg(self):
        from handlers.server_selection import resolve_target_server

        update = MagicMock()
        update.effective_user.id = 12345
        context = MagicMock()
        context.args = ["hkt"]

        cfg, is_all, prompt = await resolve_target_server(update, context, "check", allow_all=True)
        self.assertFalse(prompt)
        self.assertFalse(is_all)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["id"], "hkt")
        self.assertEqual(context.args, [])  # Arg consumed

    @patch("config.config", {
        "servers": [
            {"id": "hkt", "name": "香港 HKT"},
            {"id": "tokyo", "name": "东京"},
        ]
    })
    async def test_resolve_target_server_all(self):
        from handlers.server_selection import resolve_target_server

        update = MagicMock()
        update.effective_user.id = 12345
        context = MagicMock()
        context.args = ["all"]

        cfg, is_all, prompt = await resolve_target_server(update, context, "change", allow_all=True)
        self.assertFalse(prompt)
        self.assertTrue(is_all)
        self.assertIsNone(cfg)

    @patch("config.config", {
        "servers": [
            {"id": "hkt", "name": "香港 HKT"},
            {"id": "tokyo", "name": "东京"},
        ]
    })
    async def test_resolve_target_server_prompt_when_none(self):
        from handlers.server_selection import resolve_target_server
        from utils.state import set_user_selected_server

        update = MagicMock()
        update.effective_user.id = 777888
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = []

        cfg, is_all, prompt = await resolve_target_server(update, context, "ping", allow_all=False)
        self.assertTrue(prompt)
        self.assertFalse(is_all)
        self.assertIsNone(cfg)
        update.message.reply_text.assert_called_once()

    @patch("config.config", {
        "ip_change_provider": "generic",
        "ip_change_api": "https://example.com/change",
    })
    async def test_resolve_target_server_single_server_no_prompt(self):
        from handlers.server_selection import resolve_target_server

        update = MagicMock()
        update.effective_user.id = 12345
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = []

        cfg, is_all, prompt = await resolve_target_server(update, context, "check", allow_all=True)
        self.assertFalse(prompt)
        self.assertFalse(is_all)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["id"], "default")
        update.message.reply_text.assert_not_called()


class TestBugFixesAndEdgeCases(unittest.IsolatedAsyncioTestCase):
    def test_state_concurrency(self):
        import concurrent.futures
        from utils.state import update_server_state_keys, load_server_state

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            temp_state_file = f.name

        try:
            with patch.dict(os.environ, {"VPS_IP_BOT_STATE_FILE": temp_state_file}):
                def worker(idx):
                    sid = f"node_{idx % 3}"
                    update_server_state_keys(sid, {"counter": idx, "worker": True})

                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futures = [executor.submit(worker, i) for i in range(30)]
                    for fut in concurrent.futures.as_completed(futures):
                        fut.result()

                for i in range(3):
                    data = load_server_state(f"node_{i}")
                    self.assertTrue(data.get("worker"))
        finally:
            if os.path.exists(temp_state_file):
                os.unlink(temp_state_file)

    def test_remote_ssh_host_validation(self):
        from utils.remote_ssh import run_remote_ssh_command, build_ssh_command_prefix

        # Empty host raises ValueError
        with self.assertRaises(ValueError):
            run_remote_ssh_command("echo 1", server_config={"remote_ssh_host": ""})

        # BatchMode in build_ssh_command_prefix
        prefix = build_ssh_command_prefix({
            "host": "192.168.1.1",
            "port": 22,
            "user": "root",
            "key_path": "",
            "password": "",
        })
        self.assertIn("BatchMode=yes", prefix)

    def test_persist_config_value_preserves_nested_servers(self):
        from bot import persist_config_value
        import config

        sample_yaml = (
            "ip_change_provider: generic\n"
            "servers:\n"
            "  - id: hkt\n"
            "    ip_change_provider: fachost\n"
            "  - id: tokyo\n"
            "    ip_change_provider: boil\n"
        )
        with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False, encoding="utf-8") as f:
            f.write(sample_yaml)
            tmp_cfg_path = f.name

        try:
            with patch.dict(config.config, {"_loaded_from": tmp_cfg_path}):
                persist_config_value("ip_change_provider", "boil")
                with open(tmp_cfg_path, "r", encoding="utf-8") as rf:
                    content = rf.read()
                # Top level must be updated
                self.assertTrue(content.startswith('ip_change_provider: "boil"\n'))
                # Indented server provider must NOT be changed to boil
                self.assertIn("  - id: hkt\n    ip_change_provider: fachost", content)
        finally:
            if os.path.exists(tmp_cfg_path):
                os.unlink(tmp_cfg_path)

    async def test_ip_quality_callback_support(self):
        from handlers.ip_quality import ip_quality_handler

        update = MagicMock()
        update.message = None
        mock_msg = AsyncMock()
        update.callback_query = MagicMock()
        update.callback_query.message = mock_msg
        context = MagicMock()
        context.args = []

        with patch("handlers.ip_quality.check_user_permission", return_value=True), \
             patch("handlers.ip_quality.resolve_target_server", return_value=({"id": "default"}, False, False)), \
             patch("handlers.ip_quality.run_quality_command", return_value=(0, "No SVG here")):
            await ip_quality_handler(update, context)
            mock_msg.reply_text.assert_called()

    async def test_single_server_locked_message(self):
        from services.ip_change_service import get_server_change_lock, perform_ip_change
        lock = get_server_change_lock("default")
        await lock.acquire()
        try:
            with patch("services.ip_change_service.is_multi_server_mode", return_value=False):
                res = await perform_ip_change(trigger="manual")
                self.assertFalse(res.success)
                self.assertEqual(res.status, "LOCKED")
                self.assertEqual(res.message, "正在执行换IP任务，请勿重复发起")
                self.assertNotIn("[default]", res.message)
        finally:
            lock.release()


if __name__ == "__main__":
    unittest.main()


