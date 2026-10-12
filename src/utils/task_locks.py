import asyncio
from typing import Dict

_TASK_LOCKS: Dict[str, Dict[str, asyncio.Lock]] = {}


def get_task_lock(task_type: str, server_id: str = "default") -> asyncio.Lock:
    """
    获取指定任务类型与指定服务器节点的互斥锁。
    task_type: 任务类别 (如 quality, stream, speedtest, check, ping, health, change)
    server_id: 服务器ID (如 node_1, default, global)
    """
    category = str(task_type or "general").strip().lower()
    sid = str(server_id or "default").strip().lower()
    if category not in _TASK_LOCKS:
        _TASK_LOCKS[category] = {}
    if sid not in _TASK_LOCKS[category]:
        _TASK_LOCKS[category][sid] = asyncio.Lock()
    return _TASK_LOCKS[category][sid]


def is_task_locked(task_type: str, server_id: str = "default") -> bool:
    """检查指定任务类型与指定服务器节点的锁是否被占用"""
    category = str(task_type or "general").strip().lower()
    sid = str(server_id or "default").strip().lower()
    if category in _TASK_LOCKS and sid in _TASK_LOCKS[category]:
        return _TASK_LOCKS[category][sid].locked()
    return False


def reset_task_locks() -> None:
    """重置所有互斥锁（主要供单元测试及状态重置使用）"""
    _TASK_LOCKS.clear()
