import asyncio
import datetime as dt
import time
from dataclasses import dataclass
from typing import Optional

from config import config
from services.dns_update_service import update_dns_if_enabled
from utils.logger import logger
from utils.network import (
    ChangeIPTimeoutError,
    call_boil_change_ip,
    call_boil_get_ip,
    call_change_ip_api,
    get_current_ip,
    parse_change_ip_result,
    verify_public_ip_matches,
)
from utils.redact import redact_text
from utils.state import (
    get_last_change_time,
    load_state,
    update_change_state,
    update_state_keys,
)


@dataclass
class ChangeResult:
    success: bool
    status: str
    message: str
    old_ip: str = ""
    new_ip: str = ""
    dns_message: str = ""
    trigger: str = "manual"
    raw: Optional[dict] = None


def _check_interval() -> Optional[str]:
    interval = int(config.get("ip_change_interval", 2))
    last_change = get_last_change_time()
    if not last_change:
        return None

    diff_minutes = (time.time() - last_change) / 60
    if diff_minutes < interval:
        remaining = max(1, int(interval - diff_minutes))
        return f"距离上次更换IP不足 {interval} 分钟，请等待约 {remaining} 分钟后再试"
    return None


def build_result_message(result: ChangeResult) -> str:
    if result.success:
        return (
            "IP更换成功\n"
            f"状态: {result.status}\n"
            f"说明: {result.message}\n"
            f"旧IP: {result.old_ip or '未知'}\n"
            f"新IP: {result.new_ip or '未知'}\n"
            f"DNS结果: {result.dns_message or '未执行'}"
        )
    return (
        "IP更换失败\n"
        f"状态: {result.status}\n"
        f"说明: {result.message}\n"
        f"旧IP: {result.old_ip or '未知'}\n"
        f"新IP: {result.new_ip or '未知'}"
    )


async def _wait_for_public_ip_change(old_ip: str, retries: int = 12, delay: int = 10) -> str:
    if not old_ip:
        return ""
    for idx in range(retries):
        await asyncio.sleep(delay)
        try:
            current_ip = await asyncio.to_thread(get_current_ip)
            logger.info(f"超时兜底校验第 {idx + 1}/{retries} 次，当前公网IP: {current_ip}")
            if current_ip and current_ip != old_ip:
                return current_ip
        except Exception as e:
            logger.warning(f"超时兜底校验第 {idx + 1}/{retries} 次获取公网IP失败: {e}")
    return ""


async def _wait_for_boil_ip_change(old_ip: str, base_url: str, token: str, retries: int = 18, delay: int = 5) -> str:
    for idx in range(retries):
        await asyncio.sleep(delay)
        try:
            current_ip = await asyncio.to_thread(call_boil_get_ip, base_url, token, 15)
            logger.info(f"Boil API 轮询第 {idx + 1}/{retries} 次，当前返回IP: {current_ip}")
            if current_ip and current_ip != old_ip:
                return current_ip
        except Exception as e:
            logger.warning(f"Boil API 轮询第 {idx + 1}/{retries} 次获取IP失败: {e}")
    return ""


async def _verify_changed_ip(target_ip: str) -> bool:
    if not config.get("ip_change_verify_public_ip", True):
        return True

    await asyncio.sleep(int(config.get("ip_change_verify_delay", 5)))
    retry_count = int(config.get("ip_change_retry_verify_count", 3))
    for _ in range(retry_count):
        if await asyncio.to_thread(verify_public_ip_matches, target_ip):
            return True
        await asyncio.sleep(3)
    return False


def _update_dns_safely(new_ip: str) -> str:
    # 换IP成功并获得新IP后，立即更新本地内存DNS映射，防止SmartDNS/本地DNS缓存滞后
    try:
        from utils.remote_ssh import set_cached_host_ip
        for host_key in ("dns_record_name", "huawei_dns_record_name", "remote_ssh_host"):
            hostname = str(config.get(host_key) or "").strip().rstrip(".")
            if hostname:
                set_cached_host_ip(hostname, new_ip)
    except Exception as ex:
        logger.debug(f"更新本地DNS映射异常: {ex}")

    try:
        return update_dns_if_enabled(new_ip)
    except Exception as dns_error:
        dns_message = f"DNS更新失败: {dns_error}"
        logger.error(dns_message)
        return dns_message


def get_ip_change_provider_name() -> str:
    return str(config.get("ip_change_provider") or "classic").strip().lower()


async def _perform_classic_ip_change(trigger: str = "manual") -> ChangeResult:
    interval_error = _check_interval()
    if interval_error:
        return ChangeResult(
            success=False,
            status="RATE_LIMITED",
            message=interval_error,
            trigger=trigger,
        )

    api_url = str(config.get("ip_change_api", "")).strip()
    if not api_url:
        return ChangeResult(
            success=False,
            status="CONFIG_ERROR",
            message="未配置 ip_change_api",
            trigger=trigger,
        )

    public_old_ip = ""
    try:
        public_old_ip = await asyncio.to_thread(get_current_ip)
    except Exception as e:
        logger.warning(f"更换前获取公网IP失败，将尽量按接口响应判断: {e}")

    try:
        api_data = await asyncio.to_thread(
            call_change_ip_api,
            api_url,
            int(config.get("ip_change_timeout", 600)),
        )

        parsed = parse_change_ip_result(api_data)
        status = parsed["status"]
        old_ip = parsed["old_ip"] or public_old_ip
        new_ip = parsed["new_ip"]

        if status == "IP unchanged":
            return ChangeResult(
                success=False,
                status=status,
                message="换IP接口返回 IP unchanged，IP未变化",
                old_ip=old_ip,
                new_ip=new_ip,
                trigger=trigger,
                raw=api_data,
            )

        if status != "IP changed":
            return ChangeResult(
                success=False,
                status=status or "UNKNOWN",
                message=f"换IP接口返回未知状态: {status or '空'}",
                old_ip=old_ip,
                new_ip=new_ip,
                trigger=trigger,
                raw=api_data,
            )

        verified = await _verify_changed_ip(new_ip)
        if not verified:
            logger.warning(f"API返回已更换，但公网IP暂未校验通过: {new_ip}")

        dns_message = await asyncio.to_thread(_update_dns_safely, new_ip)

        return ChangeResult(
            success=True,
            status=status,
            message="接口返回 IP changed",
            old_ip=old_ip,
            new_ip=new_ip,
            dns_message=dns_message,
            trigger=trigger,
            raw=api_data,
        )

    except ChangeIPTimeoutError as e:
        logger.warning(f"换IP接口超时，转入公网IP兜底判断: {e}")
        fallback_new_ip = await _wait_for_public_ip_change(public_old_ip)
        if fallback_new_ip:
            dns_message = await asyncio.to_thread(_update_dns_safely, fallback_new_ip)
            return ChangeResult(
                success=True,
                status="TIMEOUT_BUT_IP_CHANGED",
                message="换IP接口超时，但公网IP已变化，按成功处理",
                old_ip=public_old_ip,
                new_ip=fallback_new_ip,
                dns_message=dns_message,
                trigger=trigger,
            )

        return ChangeResult(
            success=False,
            status="TIMEOUT",
            message=f"换IP接口超时，且在兜底校验期间未发现公网IP变化: {e}",
            old_ip=public_old_ip,
            new_ip="",
            trigger=trigger,
        )

    except Exception as e:
        logger.exception(f"执行IP更换失败: {e}")
        return ChangeResult(
            success=False,
            status="EXCEPTION",
            message=f"更换IP时出错: {redact_text(str(e))}",
            old_ip=public_old_ip,
            new_ip="",
            trigger=trigger,
        )


async def _perform_boil_ip_change(trigger: str = "manual") -> ChangeResult:
    state = load_state()
    now = time.time()
    next_allowed_at = float(state.get("boil_next_allowed_at", 0) or 0)
    if now < next_allowed_at:
        wait_sec = max(1, int(next_allowed_at - now))
        next_dt = dt.datetime.fromtimestamp(next_allowed_at).strftime("%H:%M:%S")
        return ChangeResult(
            success=False,
            status="COOLDOWN_PROTECTION",
            message=f"Boil 频率限制冷却中，还需等待 {wait_sec} 秒（预计可用时间: {next_dt}），已自动拦截以防扣减API配额",
            trigger=trigger,
        )

    token = str(config.get("boil_api_token", "")).strip()
    base_url = str(config.get("boil_api_base_url", "https://ippanel.boil.network")).strip()
    if not token:
        return ChangeResult(
            success=False,
            status="CONFIG_ERROR",
            message="未配置 boil_api_token，请使用 /set_boil_token 设置或在 config.yaml 中配置",
            trigger=trigger,
        )

    boil_old_ip = ""
    try:
        boil_old_ip = await asyncio.to_thread(call_boil_get_ip, base_url, token, 15)
        logger.info(f"Boil 更换前当前IP: {boil_old_ip}")
    except Exception as e:
        logger.warning(f"Boil 更换前通过API获取IP失败，尝试本机公网IP兜底: {e}")
        try:
            boil_old_ip = await asyncio.to_thread(get_current_ip)
        except Exception:
            pass

    try:
        api_data = await asyncio.to_thread(call_boil_change_ip, base_url, token, 30)
    except ChangeIPTimeoutError as e:
        return ChangeResult(
            success=False,
            status="TIMEOUT",
            message=f"Boil API 请求超时: {e}",
            old_ip=boil_old_ip,
            trigger=trigger,
        )
    except Exception as e:
        logger.error(f"调用 Boil 换IP接口失败: {e}")
        return ChangeResult(
            success=False,
            status="BOIL_ERROR",
            message=f"Boil 换IP失败: {redact_text(str(e))}",
            old_ip=boil_old_ip,
            trigger=trigger,
        )

    next_allowed = float(api_data.get("next_allowed_at") or 0)
    uses_left = api_data.get("uses_left")
    update_state_keys({
        "boil_next_allowed_at": next_allowed,
        "boil_uses_left": uses_left if uses_left is not None else -1,
    })

    # 优先通过 Boil API 轮询新IP（每5秒检查一次，最多等待90秒）
    new_ip = await _wait_for_boil_ip_change(boil_old_ip, base_url, token, retries=18, delay=5)
    if not new_ip:
        # 兜底：如果 API 未返回新 IP，尝试检查本机公网 IP（兼容 Bot 部署在目标机自身的情况）
        new_ip = await _wait_for_public_ip_change(boil_old_ip, retries=3, delay=3)

    if not new_ip:
        uses_info = f"，今日剩余配额: {uses_left}次" if uses_left is not None else ""
        return ChangeResult(
            success=False,
            status="WAIT_IP_TIMEOUT",
            message=f"Boil 更换任务已下发{uses_info}，但在规定等待时间内未能通过API获取到新IP",
            old_ip=boil_old_ip,
            trigger=trigger,
            raw=api_data,
        )

    dns_message = await asyncio.to_thread(_update_dns_safely, new_ip)
    uses_info = f"；今日剩余配额: {uses_left}次" if uses_left is not None else ""
    return ChangeResult(
        success=True,
        status="BOIL_SUCCESS",
        message=f"Boil 换IP成功{uses_info}",
        old_ip=boil_old_ip,
        new_ip=new_ip,
        dns_message=dns_message,
        trigger=trigger,
        raw=api_data,
    )


async def perform_ip_change(trigger: str = "manual") -> ChangeResult:
    provider = get_ip_change_provider_name()
    if provider == "boil":
        return await _perform_boil_ip_change(trigger=trigger)
    return await _perform_classic_ip_change(trigger=trigger)



async def persist_result_for_notification(result: ChangeResult, chat_id: str = "") -> str:
    text = build_result_message(result)
    update_change_state(
        old_ip=result.old_ip,
        new_ip=result.new_ip,
        status=result.status,
        trigger=result.trigger,
        dns_result=result.dns_message,
        message=text,
        success=result.success,
        chat_id=str(chat_id) if chat_id else "",
        pending_notify=True,
    )
    return text
