import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from config import config
from services.huawei_dns import update_huawei_dns_if_enabled
from utils.logger import logger
from utils.network import (
    ChangeIPTimeoutError,
    call_change_ip_api,
    get_current_ip,
    parse_change_ip_result,
    verify_public_ip_matches,
)
from utils.state import get_last_change_time, update_change_state


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
    try:
        return update_huawei_dns_if_enabled(new_ip)
    except Exception as dns_error:
        dns_message = f"华为云DNS更新失败: {dns_error}"
        logger.error(dns_message)
        return dns_message


async def perform_ip_change(trigger: str = "manual") -> ChangeResult:
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
            message=f"更换IP时出错: {e}",
            old_ip=public_old_ip,
            new_ip="",
            trigger=trigger,
        )


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
