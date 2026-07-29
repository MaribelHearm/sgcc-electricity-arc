from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .redact import redact_text

RISK_CONTROL_KEYWORDS = (
    "RK001",
    "操作过于频繁",
    "请求过于频繁",
    "稍后再试",
    "环境异常",
    "网络环境",
    "可疑",
    "风险",
    "风控",
    "安全策略",
    "异常请求",
    "恶意",
)

NON_RETRYABLE_LOGIN_CATEGORIES = {
    "risk_blocked",
    "captcha_passed_login_failed",
    "captcha_failed",
    "phone_code_timeout",
    "phone_code_request_failed",
    "login_ui_failed",
}


class NonRetryableFetchError(Exception):
    """Fetch failed in a way that should not be retried immediately."""


class LoginFailure(Exception):
    def __init__(self, category: str, message: str = ""):
        self.category = category or "login_failed"
        self.message = message or self.category
        super().__init__(f"{self.category}: {self.message}")


@dataclass(frozen=True)
class CooldownState:
    active: bool
    until: Optional[datetime] = None
    reason: str = ""

    @property
    def remaining_seconds(self) -> int:
        if not self.active or self.until is None:
            return 0
        return max(0, int((self.until - datetime.now(timezone.utc)).total_seconds()))


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def classify_login_failure(message: str | None, *, captcha_passed: bool = False, captcha_failed: bool = False) -> str:
    text = message or ""
    if any(keyword in text for keyword in RISK_CONTROL_KEYWORDS):
        return "risk_blocked"
    if captcha_passed:
        return "captcha_passed_login_failed"
    if captcha_failed:
        return "captcha_failed"
    if "登录页面加载失败" in text or "login_page" in text:
        return "page_load_failed"
    return "login_failed"


def should_retry_login_failure(category: str) -> bool:
    return category not in NON_RETRYABLE_LOGIN_CATEGORIES


def _data_dir() -> str:
    if "PYTHON_IN_DOCKER" in os.environ:
        return "/data"
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _cooldown_file() -> Path:
    return Path(os.getenv("SGCC_LOGIN_COOLDOWN_FILE", os.path.join(_data_dir(), "sgcc_login_cooldown.json")))


def get_login_cooldown() -> CooldownState:
    path = _cooldown_file()
    try:
        if not path.exists():
            return CooldownState(False)
        data = json.loads(path.read_text(encoding="utf-8"))
        until_raw = data.get("until")
        if not until_raw:
            return CooldownState(False)
        until = datetime.fromisoformat(until_raw)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if until <= datetime.now(timezone.utc):
            return CooldownState(False, until=until, reason=data.get("reason", ""))
        return CooldownState(True, until=until, reason=data.get("reason", ""))
    except Exception as e:
        logging.warning(f"读取登录风控冷却状态失败，忽略: {redact_text(e)}")
        return CooldownState(False)


def set_login_cooldown(reason: str, minutes: int | None = None) -> CooldownState:
    if minutes is None:
        minutes = int(os.getenv("RISK_COOLDOWN_MINUTES", "60"))
    minutes = max(1, int(minutes))
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    state = CooldownState(True, until=until, reason=reason)
    path = _cooldown_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "until": until.isoformat(),
            "reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logging.warning(f"写入登录风控冷却状态失败: {redact_text(e)}")
    return state


def clear_login_cooldown() -> bool:
    """Remove persisted login cooldown after a successful authenticated fetch."""
    path = _cooldown_file()
    try:
        if not path.exists():
            return False
        path.unlink()
        logging.info("已清理登录风控冷却状态。")
        return True
    except Exception as e:
        logging.warning(f"清理登录风控冷却状态失败: {redact_text(e)}")
        return False
