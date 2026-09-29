import asyncio
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
        self.assertIn("remote_ssh_enabled", DEFAULT_CONFIG)
        self.assertIn("remote_ssh_host", DEFAULT_CONFIG)
        self.assertIn("remote_ssh_port", DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["ip_change_provider"], "classic")
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
        from utils.remote_ssh import _DNS_LOCAL_CACHE
        _DNS_LOCAL_CACHE.clear()

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
