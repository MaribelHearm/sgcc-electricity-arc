"""Single-switch diagnostic collection for SGCC fetch runs.

``SGCC_DIAG=true`` enables a run-level diagnostic package and a concise log
summary.  The collector is observational: it records already parsed models and
already captured Vue/Vuex snapshots, applies redaction, and never changes parser
or publisher output.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import sys
import zipfile
from dataclasses import asdict, is_dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Optional

from .ha_mapping import account_data_summary
from .model import AccountData, mask_account_no
from .redact import now_iso, redact_text, redact_url
from .observation import (
    Observation,
    ParserDecision,
    collect_generic_candidates,
    shape_fingerprint,
)


SUMMARY_START = "========== SGCC DIAG SUMMARY START =========="
SUMMARY_END = "========== SGCC DIAG SUMMARY END =========="

DEFAULT_DIAG_DIR = "/data/debug"
LEGACY_DIAG_DIR = "/data/diag"
MAX_FIELD_VALUES_PER_PAGE = 600
MAX_SHAPES_PER_PAGE = 260
MAX_LIST_ITEMS_PER_ARRAY = 20
MAX_DICT_ITEMS_PER_OBJECT = 120
MAX_FIELD_DEPTH = 10

_TRUTHY = {"1", "true", "yes", "on"}
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|pwd|token|secret|cookie|authorization|credential|api[_-]?key|access[_-]?key|"
    r"session[_-]?id|verification[_-]?code|captcha(?:[_-]?(?:ticket|token))?|sms[_-]?code)$",
    re.IGNORECASE,
)
_PII_KEY_RE = re.compile(
    r"(phone|mobile|tel|address|addr|elecaddr|custname|consname|display[_-]?name|realname|"
    r"id[_-]?card|identity|cert(?:ificate)?[_-]?(?:no|num|number)|email|"
    r"姓名|地址|手机号|电话|身份证|证件|邮箱)",
    re.IGNORECASE,
)
_ACCOUNT_KEY_RE = re.compile(
    r"^(consNo|consNo_dst|accountNo|acctNo|user_id|userId|selectValue|account_no)$",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_ACCOUNT_RE = re.compile(r"(?<!\d)(\d{13})(?!\d)")
_LONG_NUMERIC_ID_RE = re.compile(r"\d{13,}")
_ID_CARD_RE = re.compile(r"(?<![0-9A-Za-z])(?:\d{15}|\d{17}[0-9Xx])(?![0-9A-Za-z])")
_EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SENSITIVE_LABEL_RE = re.compile(
    r"(password|passwd|pwd|token|secret|cookie|authorization|api[_ -]?key|"
    r"phone|mobile|tel|address|addr|cust(?:omer)?name|consname|realname|"
    r"id[_ -]?card|identity|certificate|email|account(?:no|number)?|acctno|"
    r"姓名|地址|手机号|电话|身份证|证件|邮箱|户号|用户编号|客户编号|账号|"
    r"密码|口令|令牌|验证码)",
    re.IGNORECASE,
)
_LABEL_KEYS = {"label", "title", "fieldname", "keyname"}
_LABEL_VALUE_KEYS = {"value", "val", "content", "text", "displayvalue", "fieldvalue"}
_NON_PII_TECHNICAL_KEYS = {"sec-ch-ua-mobile"}


def env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def debug_enabled() -> bool:
    """Canonical debug switch with SGCC_DIAG kept as a compatibility alias."""
    return (
        env_truthy("SGCC_FORCE_DEBUG")
        or env_truthy("SGCC_DEBUG")
        or env_truthy("SGCC_DIAG")
        or env_truthy("DEBUG_MODE")
    )


def diag_enabled() -> bool:
    return debug_enabled()


def diag_output_root() -> Path:
    if (
        env_truthy("SGCC_FORCE_DEBUG")
        or env_truthy("SGCC_DEBUG")
        or env_truthy("DEBUG_MODE")
    ):
        return Path(os.getenv("SGCC_DEBUG_DIR") or DEFAULT_DIAG_DIR)
    if env_truthy("SGCC_DIAG"):
        return Path(os.getenv("SGCC_DIAG_DIR") or LEGACY_DIAG_DIR)
    return Path(os.getenv("SGCC_DEBUG_DIR") or os.getenv("SGCC_DIAG_DIR") or DEFAULT_DIAG_DIR)


class DiagnosticCollector:
    """Collect one fetch run's diagnostic evidence and emit redacted files."""

    def __init__(self, trigger_type: str = "manual", output_dir: Optional[Path | str] = None):
        self.trigger_type = trigger_type
        self.output_root = Path(output_dir) if output_dir is not None else diag_output_root()
        self.started_at = now_iso()
        self.generated_at = ""
        self.status = "running"
        self.run_id: Optional[int] = None
        self.runtime: dict[str, Any] = {}
        self.scrape: dict[str, Any] = {
            "selector_option_count": None,
            "fetched_account_count": None,
            "saved_count": 0,
            "skipped_count": 0,
        }
        self.sessions: list[dict[str, Any]] = []
        self.pages: list[dict[str, Any]] = []
        self.field_pages: list[dict[str, Any]] = []
        self.accounts: list[dict[str, Any]] = []
        self.publish: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.browser_runtime: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.candidates: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.shapes: list[dict[str, Any]] = []
        self.output_paths: dict[str, str] = {}
        self._emitted = False
        self._observation_keys: set[tuple[str, str, str, str]] = set()

    def set_run_id(self, run_id: Optional[int]) -> None:
        self.run_id = run_id

    def record_runtime(self, config: Any = None, publisher: Optional[str] = None, stage: str = "runtime") -> None:
        self.runtime.update({
            "stage": stage,
            "package_version": _package_version(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "trigger_type": self.trigger_type,
            "publisher": publisher or getattr(config, "PUBLISHER", None),
            "browser_mode": os.getenv("SGCC_BROWSER_MODE", "local"),
            "debug_mode": str(debug_enabled()).lower(),
            "env": _safe_env_snapshot(),
        })
        if config is not None:
            self.runtime["config"] = {
                "retry_limit": getattr(config, "RETRY_TIMES_LIMIT", None),
                "page_load_timeout": getattr(config, "PAGE_LOAD_TIMEOUT", None),
                "implicit_wait": getattr(config, "DRIVER_IMPLICITY_WAIT_TIME", None),
                "ignored_user_count": len(getattr(config, "IGNORE_USER_ID", []) or []),
                "mqtt_host": redact_text(getattr(config, "MQTT_HOST", "") or ""),
                "mqtt_port": getattr(config, "MQTT_PORT", None),
                "mqtt_discovery_prefix": getattr(config, "MQTT_DISCOVERY_PREFIX", None),
                "mqtt_legacy_discovery_mode": getattr(
                    config,
                    "MQTT_LEGACY_DISCOVERY_MODE",
                    None,
                ),
            }

    def record_session(self, stage: str, check: Any) -> None:
        self.sessions.append({
            "stage": stage,
            "checked_at": getattr(check, "checked_at", ""),
            "status": getattr(check, "status", "unknown"),
            "current_url": redact_url(str(getattr(check, "current_url", "") or "")),
            "check_method": getattr(check, "check_method", ""),
            "redirected_to_login": bool(getattr(check, "redirected_to_login", False)),
            "evidence_redacted": redact_scalar(getattr(check, "evidence_redacted", "") or "", "evidence"),
        })

    def record_selector_options(self, count: int) -> None:
        self.scrape["selector_option_count"] = count

    def record_timeline(self, event: str, **details: Any) -> None:
        self.timeline.append(redact_structure({
            "at": now_iso(),
            "event": event,
            **details,
        }))

    def record_browser_runtime(self, stage: str, snapshot: dict[str, Any]) -> None:
        self.browser_runtime.append(redact_structure({
            "captured_at": now_iso(),
            "stage": stage,
            "snapshot": snapshot,
        }))

    def record_observations(self, observations: list[Observation]) -> None:
        for observation in observations:
            payload_hash, shape = shape_fingerprint(observation.payload)
            observation_key = (
                observation.source,
                observation.scope_id,
                observation.observed_at,
                payload_hash,
            )
            if observation_key in self._observation_keys:
                continue
            self._observation_keys.add(observation_key)
            base = {
                "source": observation.source,
                "scope_id": observation.scope_id,
                "scope_label": observation.scope_label,
                "account_no": observation.account_no,
                "observed_at": observation.observed_at,
                "metadata": observation.metadata,
                "shape_hash": payload_hash,
                "payload": observation.payload,
            }
            self.observations.append(redact_structure(base))
            self.shapes.append(redact_structure({
                "source": observation.source,
                "scope_id": observation.scope_id,
                "scope_label": observation.scope_label,
                "shape_hash": payload_hash,
                "shape": shape,
            }))
            for candidate in collect_generic_candidates(observation.payload):
                self.candidates.append(redact_structure({
                    "source": observation.source,
                    "scope_id": observation.scope_id,
                    "scope_label": observation.scope_label,
                    "shape_hash": payload_hash,
                    **candidate,
                }))

    def record_decisions(self, decisions: list[ParserDecision]) -> None:
        self.decisions.extend(redact_structure(item.as_dict()) for item in decisions)

    def record_fetched_accounts(self, count: int) -> None:
        self.scrape["fetched_account_count"] = count

    def record_page(self, label: str, snapshot: dict[str, Any], parsed: AccountData) -> None:
        try:
            from .money_candidates import collect_money_candidates

            candidates = collect_money_candidates(
                store=snapshot.get("store"),
                components=snapshot.get("components"),
            )
        except Exception as exc:
            candidates = []
            self.record_error(exc, stage=f"page:{label}:money_candidates")

        parsed_summary = account_data_diag_summary(parsed)
        inventory = collect_snapshot_field_inventory(snapshot)
        page_summary = {
            "label": label,
            "url": redact_url(str(snapshot.get("url") or "")),
            "parsed": parsed_summary,
            "money_candidate_count": len(candidates),
            "field_value_count": inventory["field_value_count"],
            "field_truncated": inventory["truncated"],
        }
        self.pages.append(page_summary)
        self.field_pages.append({
            **page_summary,
            "money_candidates": [_money_candidate_to_dict(item) for item in candidates],
            "source_shapes": inventory["shapes"],
            "field_values": inventory["fields"],
        })

    def record_account_saved(self, account_data: AccountData) -> None:
        self.scrape["saved_count"] = int(self.scrape.get("saved_count") or 0) + 1
        self.accounts.append({
            "event": "saved",
            "summary_text": account_data_summary(account_data),
            **account_data_diag_summary(account_data),
        })

    def record_account_skipped(self, account_data: Optional[AccountData], reason: str) -> None:
        self.scrape["skipped_count"] = int(self.scrape.get("skipped_count") or 0) + 1
        item: dict[str, Any] = {"event": "skipped", "reason": redact_text(reason)}
        if account_data is not None:
            item.update(account_data_diag_summary(account_data))
        self.accounts.append(item)

    def record_publish(
        self,
        account_no: str,
        publisher: str,
        success: bool,
        detail: str = "",
    ) -> None:
        self.publish.append({
            "account": mask_account_no(account_no),
            "publisher": publisher,
            "success": bool(success),
            "detail": redact_scalar(detail, "detail"),
        })

    def record_error(self, error: Any, message: Optional[str] = None, stage: str = "") -> None:
        error_type = error if isinstance(error, str) else type(error).__name__
        error_message = message if message is not None else str(error)
        self.errors.append({
            "stage": stage,
            "type": str(error_type),
            "message": redact_scalar(error_message, "message"),
        })

    def emit(self, status: Optional[str] = None) -> None:
        if self._emitted:
            return
        self._emitted = True
        if status:
            self.status = status
        self.generated_at = now_iso()

        run_dir = self._run_dir()
        latest_dir = self.output_root / "latest"
        self.output_paths = {
            "run_dir": str(run_dir),
            "latest_dir": str(latest_dir),
            "summary_txt": str(latest_dir / "summary.txt"),
            "summary_json": str(latest_dir / "summary.json"),
            "fields_json": str(latest_dir / "fields.redacted.json"),
            "login_network_json": str(latest_dir / "login-network.json"),
            "observations_json": str(latest_dir / "observations.redacted.json"),
            "candidates_json": str(latest_dir / "candidates.redacted.json"),
            "decisions_json": str(latest_dir / "parser-decisions.json"),
            "shapes_json": str(latest_dir / "shapes.json"),
            "timeline_json": str(latest_dir / "timeline.json"),
            "bundle_zip": str(latest_dir / "sgcc-debug-bundle.zip"),
        }

        summary_text = self.summary_text()
        summary_json = self.summary_json()
        fields_json = self.fields_json()
        debug_files = self.debug_files()

        try:
            self._write_outputs(run_dir, latest_dir, summary_text, summary_json, fields_json, debug_files)
        except Exception as exc:
            logging.warning(f"SGCC DIAG 写入诊断包失败: {redact_text(exc)}")
        logging.info("\n%s", summary_text.rstrip())

    def summary_text(self) -> str:
        lines = [
            SUMMARY_START,
            f"status={self.status}",
            f"generated_at={self.generated_at or now_iso()}",
            f"run_id={self.run_id if self.run_id is not None else '-'}",
            f"trigger_type={self.trigger_type}",
            f"publisher={self.runtime.get('publisher') or '-'}",
            f"browser_mode={self.runtime.get('browser_mode') or '-'}",
        ]
        if self.output_paths:
            lines.append(f"diag_latest={self.output_paths.get('latest_dir')}")

        if self.sessions:
            first = self.sessions[0]
            last = self.sessions[-1]
            lines.append(
                "session="
                f"{first.get('stage')}:{first.get('status')} -> "
                f"{last.get('stage')}:{last.get('status')}"
            )

        lines.append(
            "scrape="
            f"selector_options={_dash_none(self.scrape.get('selector_option_count'))}, "
            f"fetched={_dash_none(self.scrape.get('fetched_account_count'))}, "
            f"saved={self.scrape.get('saved_count')}, "
            f"skipped={self.scrape.get('skipped_count')}"
        )
        lines.append(
            "debug="
            f"browser_runtime={len(self.browser_runtime)}, "
            f"login_network={len(self._login_network_observations())}, "
            f"observations={len(self.observations)}, "
            f"candidates={len(self.candidates)}, "
            f"decisions={len(self.decisions)}, "
            f"shapes={len(self.shapes)}, "
            f"timeline={len(self.timeline)}"
        )

        for index, account in enumerate(self.accounts, 1):
            lines.append(
                f"account[{index}]="
                f"event={account.get('event')}, "
                f"account={account.get('account') or '-'}, "
                f"balance={_money_summary(account.get('balance'))}, "
                f"daily={_series_summary(account.get('daily'))}, "
                f"monthly={_series_summary(account.get('monthly'))}, "
                f"yearly={_yearly_summary(account.get('yearly'))}"
            )

        for index, page in enumerate(self.pages, 1):
            parsed = page.get("parsed") or {}
            lines.append(
                f"page[{index}]="
                f"label={page.get('label')}, "
                f"url={page.get('url') or '-'}, "
                f"account={parsed.get('account') or '-'}, "
                f"money_candidates={page.get('money_candidate_count')}, "
                f"fields={page.get('field_value_count')}"
                f"{' truncated' if page.get('field_truncated') else ''}"
            )

        for item in self.publish:
            lines.append(
                "publish="
                f"publisher={item.get('publisher')}, "
                f"account={item.get('account') or '-'}, "
                f"success={str(item.get('success')).lower()}, "
                f"detail={item.get('detail') or '-'}"
            )

        for item in self.errors:
            lines.append(
                "error="
                f"stage={item.get('stage') or '-'}, "
                f"type={item.get('type')}, "
                f"message={item.get('message')}"
            )

        lines.append(SUMMARY_END)
        return "\n".join(lines) + "\n"

    def summary_json(self) -> dict[str, Any]:
        return redact_structure({
            "status": self.status,
            "started_at": self.started_at,
            "generated_at": self.generated_at,
            "run_id": self.run_id,
            "trigger_type": self.trigger_type,
            "runtime": self.runtime,
            "scrape": self.scrape,
            "sessions": self.sessions,
            "accounts": self.accounts,
            "pages": self.pages,
            "publish": self.publish,
            "errors": self.errors,
            "browser_runtime": self.browser_runtime,
            "debug": {
                "browser_runtime_count": len(self.browser_runtime),
                "login_network_count": len(self._login_network_observations()),
                "observation_count": len(self.observations),
                "candidate_count": len(self.candidates),
                "decision_count": len(self.decisions),
                "shape_count": len(self.shapes),
                "timeline_count": len(self.timeline),
            },
            "outputs": self.output_paths,
        })

    def fields_json(self) -> dict[str, Any]:
        return redact_structure({
            "status": self.status,
            "generated_at": self.generated_at,
            "run_id": self.run_id,
            "pages": self.field_pages,
        })

    def debug_files(self) -> dict[str, Any]:
        return {
            "browser-runtime.json": redact_structure({
                "status": self.status,
                "generated_at": self.generated_at,
                "run_id": self.run_id,
                "captures": self.browser_runtime,
            }),
            "login-network.json": redact_structure({
                "status": self.status,
                "generated_at": self.generated_at,
                "run_id": self.run_id,
                "browser_runtime_reference": self._browser_fingerprint_reference(),
                "requests": self._login_network_observations(),
                "comparisons": self._login_network_comparisons(),
            }),
            "observations.redacted.json": redact_structure({
                "status": self.status,
                "generated_at": self.generated_at,
                "run_id": self.run_id,
                "observations": self.observations,
            }),
            "candidates.redacted.json": redact_structure({
                "status": self.status,
                "generated_at": self.generated_at,
                "run_id": self.run_id,
                "candidates": self.candidates,
            }),
            "parser-decisions.json": redact_structure({
                "status": self.status,
                "generated_at": self.generated_at,
                "run_id": self.run_id,
                "decisions": self.decisions,
            }),
            "shapes.json": redact_structure({
                "status": self.status,
                "generated_at": self.generated_at,
                "run_id": self.run_id,
                "shapes": self.shapes,
            }),
            "timeline.json": redact_structure({
                "status": self.status,
                "generated_at": self.generated_at,
                "run_id": self.run_id,
                "timeline": self.timeline,
            }),
        }

    def _login_network_observations(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self.observations
            if item.get("source") == "network_metadata"
            and item.get("scope_id") == "login"
        ]

    def _browser_fingerprint_reference(self) -> dict[str, Any]:
        for capture in reversed(self.browser_runtime):
            snapshot = capture.get("snapshot") or {}
            page = snapshot.get("page") or {}
            webdriver = snapshot.get("webdriver") or {}
            if page:
                return {
                    "stage": capture.get("stage"),
                    "user_agent": page.get("userAgent"),
                    "language": page.get("language"),
                    "languages": page.get("languages") or [],
                    "platform": page.get("platform"),
                    "max_touch_points": page.get("maxTouchPoints"),
                    "browser_version": webdriver.get("browser_version"),
                }
        return {}

    def _login_network_comparisons(self) -> list[dict[str, Any]]:
        browser = self._browser_fingerprint_reference()
        comparisons: list[dict[str, Any]] = []
        for observation in self._login_network_observations():
            payload = observation.get("payload") or {}
            request = payload.get("request") or {}
            headers = request.get("fingerprint_headers") or {}
            comparisons.append({
                "url": (observation.get("metadata") or {}).get("url"),
                "method": request.get("method"),
                "resource_type": request.get("resource_type"),
                "actual_headers": headers,
                "missing_headers": [
                    name
                    for name in (
                        "user-agent",
                        "accept-language",
                        "sec-ch-ua",
                        "sec-ch-ua-platform",
                        "sec-ch-ua-mobile",
                    )
                    if name not in headers
                ],
                "checks": _compare_request_fingerprint(headers, browser),
            })
        return comparisons

    def _run_dir(self) -> Path:
        stamp = re.sub(r"[^0-9T]", "", (self.started_at or now_iso()).split("+", 1)[0].replace("-", "").replace(":", ""))
        run = f"run{self.run_id}" if self.run_id is not None else "run-unknown"
        return self.output_root / f"{stamp}-{run}"

    def _write_outputs(
        self,
        run_dir: Path,
        latest_dir: Path,
        summary_text: str,
        summary_json: dict[str, Any],
        fields_json: dict[str, Any],
        debug_files: dict[str, Any],
    ) -> None:
        _ensure_private_dir(self.output_root)
        _ensure_private_dir(run_dir)
        _write_package(run_dir, summary_text, summary_json, fields_json, debug_files)

        if latest_dir.exists() or latest_dir.is_symlink():
            if latest_dir.is_symlink() or latest_dir.is_file():
                latest_dir.unlink()
            else:
                shutil.rmtree(latest_dir)
        _ensure_private_dir(latest_dir)
        _write_package(latest_dir, summary_text, summary_json, fields_json, debug_files)


def account_data_diag_summary(account_data: Optional[AccountData]) -> dict[str, Any]:
    if account_data is None:
        return {}
    account_no = account_data.account.account_no if account_data.account else ""
    latest_daily = _latest_by(account_data.daily, "date")
    latest_monthly = _latest_by(account_data.monthly, "year_month")
    balance = account_data.balance
    yearly = account_data.yearly
    return {
        "account": mask_account_no(account_no),
        "balance": {
            "present": balance is not None,
            "balance_cny": balance.balance_cny if balance else None,
            "prepay_balance_cny": balance.prepay_balance_cny if balance else None,
            "arrears_cny": balance.arrears_cny if balance else None,
            "observed_at": redact_text(balance.observed_at) if balance else "",
        },
        "daily": {
            "count": len(account_data.daily),
            "range": _range_text([getattr(row, "date", "") for row in account_data.daily]),
            "latest_date": getattr(latest_daily, "date", None) if latest_daily else None,
            "latest_usage_kwh": getattr(latest_daily, "total_usage_kwh", None) if latest_daily else None,
        },
        "monthly": {
            "count": len(account_data.monthly),
            "range": _range_text([getattr(row, "year_month", "") for row in account_data.monthly]),
            "latest_month": getattr(latest_monthly, "year_month", None) if latest_monthly else None,
            "latest_usage_kwh": getattr(latest_monthly, "total_usage_kwh", None) if latest_monthly else None,
            "latest_charge_cny": getattr(latest_monthly, "total_charge_cny", None) if latest_monthly else None,
        },
        "yearly": {
            "present": yearly is not None,
            "year": yearly.year if yearly else None,
            "usage_kwh": yearly.total_usage_kwh if yearly else None,
            "charge_cny": yearly.total_charge_cny if yearly else None,
        },
    }


def collect_snapshot_field_inventory(snapshot: dict[str, Any]) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    shapes: list[dict[str, Any]] = []
    state = {"truncated": False}

    redacted_snapshot = redact_structure(snapshot)
    for root_path, root_value in _iter_snapshot_roots(redacted_snapshot):
        _walk_field_inventory(root_value, root_path, fields, shapes, state, depth=0)
        if len(fields) >= MAX_FIELD_VALUES_PER_PAGE and len(shapes) >= MAX_SHAPES_PER_PAGE:
            state["truncated"] = True
            break

    return {
        "field_value_count": len(fields),
        "shape_count": len(shapes),
        "truncated": state["truncated"],
        "fields": fields,
        "shapes": shapes,
    }


def redact_structure(
    value: Any,
    key: str = "",
    *,
    _depth: int = 0,
    _max_depth: int = 14,
    _max_items: int = 160,
) -> Any:
    if _depth > _max_depth:
        return "<truncated:max-depth>"
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        items = list(value.items())
        sensitive_label = any(
            str(raw_key).casefold() in _LABEL_KEYS
            and _SENSITIVE_LABEL_RE.search(str(child or ""))
            for raw_key, child in items
        )
        for raw_key, child in items[:_max_items]:
            key_text = str(raw_key)
            safe_key = _safe_output_key(key_text)
            if sensitive_label and key_text.casefold() in _LABEL_VALUE_KEYS:
                result[safe_key] = "<redacted>"
            else:
                result[safe_key] = redact_structure(
                    child,
                    key_text,
                    _depth=_depth + 1,
                    _max_depth=_max_depth,
                    _max_items=_max_items,
                )
        if len(items) > _max_items:
            result["<truncated>"] = f"{len(items) - _max_items} more keys"
        return result
    if isinstance(value, list):
        items = [
            redact_structure(
                item,
                key,
                _depth=_depth + 1,
                _max_depth=_max_depth,
                _max_items=_max_items,
            )
            for item in value[:_max_items]
        ]
        if len(value) > _max_items:
            items.append(f"<truncated:{len(value) - _max_items}-items>")
        return items
    if isinstance(value, tuple):
        return redact_structure(
            list(value),
            key,
            _depth=_depth,
            _max_depth=_max_depth,
            _max_items=_max_items,
        )
    return redact_scalar(value, key)


def redact_scalar(value: Any, key: str = "") -> Any:
    if value is None or isinstance(value, bool):
        return value
    key_text = str(key or "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric_text = str(value)
        integer_text = (
            str(int(value))
            if isinstance(value, float) and value.is_integer()
            else numeric_text
        )
        if _LONG_NUMERIC_ID_RE.fullmatch(integer_text):
            return "<redacted-numeric-id>"
        if (
            _is_secret_key(key_text)
            or _is_account_key(key_text)
            or _is_pii_key(key_text)
            or _ACCOUNT_RE.fullmatch(numeric_text)
            or _PHONE_RE.fullmatch(numeric_text)
            or _ID_CARD_RE.fullmatch(numeric_text)
        ):
            return _mask_accounts_and_phones(numeric_text)
        return value
    text = str(value)
    if _is_secret_key(key_text):
        return "<redacted>"
    if _is_account_key(key_text):
        return _mask_accounts_and_phones(text)
    if _is_pii_key(key_text):
        if _ACCOUNT_RE.search(text) or _PHONE_RE.search(text):
            return _mask_accounts_and_phones(text)
        return "<redacted>"
    text = _URL_RE.sub(lambda match: redact_url(match.group(0)), text)
    return _truncate(_mask_accounts_and_phones(redact_text(text)), 240)


def _safe_env_snapshot() -> dict[str, Any]:
    keys = (
        "PYTHON_IN_DOCKER",
        "PUBLISHER",
        "MQTT_HOST",
        "MQTT_PORT",
        "MQTT_DISCOVERY_PREFIX",
        "MQTT_LEGACY_DISCOVERY_MODE",
        "JOB_START_TIME",
        "SGCC_DAILY_JITTER_MINUTES",
        "SGCC_DAILY_RUNS",
        "SGCC_BROWSER_MODE",
        "SGCC_CDP_ADDRESS",
        "SGCC_CDP_HOST",
        "SGCC_CDP_PORT",
        "SGCC_BROWSER_SERVICE_URL",
        "SGCC_BROWSER_SERVICE_STOP_ON_RELEASE",
        "SGCC_DB_PATH",
        "SCRAPER_SETTLE_SECONDS",
        "DEBUG_MODE",
        "SGCC_DEBUG",
        "SGCC_FORCE_DEBUG",
        "SGCC_DEBUG_DIR",
        "SGCC_DIAG",
        "SGCC_DIAG_DIR",
        "SGCC_LOGIN_COOLDOWN_ENABLED",
        "SGCC_LOGIN_FALLBACK_UNATTENDED",
        "SGCC_LOGIN_FALLBACK_METHODS",
        "SGCC_LOGIN_INTERACTION_PROVIDER",
        "SGCC_RISK_FALLBACK_OVERRIDE",
        "SGCC_SMS_CODE_TIMEOUT_SECONDS",
        "SGCC_QRCODE_FALLBACK_UNATTENDED",
    )
    return {key: redact_scalar(os.getenv(key, ""), key) for key in keys if key in os.environ}


def _write_package(
    directory: Path,
    summary_text: str,
    summary_json: dict[str, Any],
    fields_json: dict[str, Any],
    debug_files: Optional[dict[str, Any]] = None,
) -> None:
    _ensure_private_dir(directory)
    _write_private_text(directory / "summary.txt", summary_text)
    _write_private_text(
        directory / "summary.json",
        json.dumps(summary_json, ensure_ascii=False, indent=2, default=str) + "\n",
    )
    _write_private_text(
        directory / "fields.redacted.json",
        json.dumps(fields_json, ensure_ascii=False, indent=2, default=str) + "\n",
    )
    for filename, payload in (debug_files or {}).items():
        _write_private_text(
            directory / filename,
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        )
    bundle_path = directory / "sgcc-debug-bundle.zip"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(directory.iterdir()):
            if path.is_file() and path != bundle_path:
                bundle.write(path, arcname=path.name)
    bundle_path.chmod(0o600)


def _ensure_private_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)


def _write_private_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _compare_request_fingerprint(
    headers: dict[str, Any],
    browser: dict[str, Any],
) -> dict[str, Optional[bool]]:
    user_agent = str(headers.get("user-agent") or "")
    browser_user_agent = str(browser.get("user_agent") or "")
    accept_language = str(headers.get("accept-language") or "")
    browser_language = str(browser.get("language") or "")
    browser_platform = str(browser.get("platform") or "")
    client_hint_platform = str(headers.get("sec-ch-ua-platform") or "").strip('"')
    browser_version = str(browser.get("browser_version") or "")
    sec_ch_ua = str(headers.get("sec-ch-ua") or "")
    sec_ch_ua_mobile = str(headers.get("sec-ch-ua-mobile") or "")

    language_primary = accept_language.split(",", 1)[0].split(";", 1)[0].strip()
    browser_major = browser_version.split(".", 1)[0] if browser_version else ""
    expected_mobile = "?1" if re.search(
        r"(Android|Mobile|iPhone|iPad)",
        browser_user_agent,
        re.IGNORECASE,
    ) else "?0"

    return {
        "user_agent_matches_js": (
            user_agent == browser_user_agent
            if user_agent and browser_user_agent
            else None
        ),
        "accept_language_primary_matches_js": (
            language_primary.lower() == browser_language.lower()
            if language_primary and browser_language
            else None
        ),
        "sec_ch_ua_platform_matches_js": (
            _platform_family(client_hint_platform)
            == _platform_family(browser_platform)
            if client_hint_platform and browser_platform
            else None
        ),
        "sec_ch_ua_contains_browser_major": (
            f'v="{browser_major}"' in sec_ch_ua
            if browser_major and sec_ch_ua
            else None
        ),
        "sec_ch_ua_mobile_matches_user_agent": (
            sec_ch_ua_mobile == expected_mobile
            if sec_ch_ua_mobile and browser_user_agent
            else None
        ),
    }


def _platform_family(value: str) -> str:
    normalized = value.strip().lower()
    if "android" in normalized:
        return "android"
    if any(item in normalized for item in ("iphone", "ipad", "ios")):
        return "ios"
    if "win" in normalized:
        return "windows"
    if any(item in normalized for item in ("mac", "darwin")):
        return "macos"
    if "linux" in normalized:
        return "linux"
    return normalized


def _iter_snapshot_roots(snapshot: dict[str, Any]):
    store = snapshot.get("store") if isinstance(snapshot, dict) else None
    if isinstance(store, dict):
        if isinstance(store.get("state"), dict):
            yield "store.state", store["state"]
        if isinstance(store.get("getters"), dict):
            yield "store.getters", store["getters"]
        if isinstance(store.get("route"), dict):
            yield "store.route", store["route"]
        for index, snap in enumerate(store.get("snapshots") or []):
            if isinstance(snap, dict):
                if isinstance(snap.get("state"), dict):
                    yield f"store.snapshots[{index}].state", snap["state"]
                if isinstance(snap.get("getters"), dict):
                    yield f"store.snapshots[{index}].getters", snap["getters"]
    elif store:
        yield "store", store

    components = snapshot.get("components") if isinstance(snapshot, dict) else None
    if isinstance(components, list):
        for index, component in enumerate(components):
            if not isinstance(component, dict):
                yield f"component[{index}]", component
                continue
            meta = {
                key: component.get(key)
                for key in ("tag", "id", "className")
                if component.get(key) not in (None, "")
            }
            if meta:
                yield f"component[{index}].meta", meta
            if isinstance(component.get("data"), dict):
                yield f"component[{index}].data", component["data"]
    elif isinstance(components, dict):
        if isinstance(components.get("data"), dict):
            yield "component.data", components["data"]
        else:
            yield "components", components


def _walk_field_inventory(
    value: Any,
    path: str,
    fields: list[dict[str, Any]],
    shapes: list[dict[str, Any]],
    state: dict[str, bool],
    depth: int,
) -> None:
    if len(fields) >= MAX_FIELD_VALUES_PER_PAGE:
        state["truncated"] = True
        return
    if depth > MAX_FIELD_DEPTH:
        state["truncated"] = True
        _add_shape(shapes, {"path": path, "type": _type_name(value), "truncated": "max_depth"})
        return

    if isinstance(value, dict):
        keys = list(value.keys())
        _add_shape(shapes, {
            "path": path,
            "type": "dict",
            "size": len(keys),
            "keys": [_safe_output_key(str(key)) for key in keys[:60]],
            "keys_truncated": len(keys) > 60,
        })
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_DICT_ITEMS_PER_OBJECT:
                state["truncated"] = True
                break
            child_path = f"{path}.{_path_key(key)}"
            _walk_field_inventory(child, child_path, fields, shapes, state, depth + 1)
            if len(fields) >= MAX_FIELD_VALUES_PER_PAGE:
                state["truncated"] = True
                break
        return

    if isinstance(value, list):
        _add_shape(shapes, {"path": path, "type": "list", "length": len(value)})
        for index, child in enumerate(value[:MAX_LIST_ITEMS_PER_ARRAY]):
            _walk_field_inventory(child, f"{path}[{index}]", fields, shapes, state, depth + 1)
            if len(fields) >= MAX_FIELD_VALUES_PER_PAGE:
                state["truncated"] = True
                break
        if len(value) > MAX_LIST_ITEMS_PER_ARRAY:
            state["truncated"] = True
        return

    if isinstance(value, tuple):
        _walk_field_inventory(list(value), path, fields, shapes, state, depth)
        return

    fields.append({
        "path": path,
        "key": _path_leaf(path),
        "type": _type_name(value),
        "value": redact_scalar(value, _path_leaf(path)),
    })


def _add_shape(shapes: list[dict[str, Any]], shape: dict[str, Any]) -> None:
    if len(shapes) < MAX_SHAPES_PER_PAGE:
        shapes.append(shape)


def _money_candidate_to_dict(candidate: Any) -> dict[str, Any]:
    return {
        "category": getattr(candidate, "category", ""),
        "source": getattr(candidate, "source", ""),
        "key": getattr(candidate, "key", ""),
        "value": getattr(candidate, "value", None),
        "raw_value": redact_scalar(getattr(candidate, "raw_value", ""), getattr(candidate, "key", "")),
        "label": redact_scalar(getattr(candidate, "label", ""), "label"),
        "account": getattr(candidate, "account", ""),
        "time": redact_text(getattr(candidate, "time", "")),
        "period": redact_text(getattr(candidate, "period", "")),
    }


def _package_version() -> str:
    try:
        return metadata.version("sgcc-home-assistant-bridge")
    except metadata.PackageNotFoundError:
        return ""


def _latest_by(rows: list[Any], attr: str) -> Any:
    values = [row for row in rows if getattr(row, attr, "")]
    return max(values, key=lambda row: getattr(row, attr, "")) if values else None


def _range_text(values: list[str]) -> str:
    clean = sorted(value for value in values if value)
    if not clean:
        return "-"
    if len(clean) == 1:
        return clean[0]
    return f"{clean[0]}..{clean[-1]}"


def _money_summary(balance: Any) -> str:
    if not isinstance(balance, dict) or not balance.get("present"):
        return "no"
    return (
        "yes("
        f"amount={_dash_none(balance.get('balance_cny'))}, "
        f"prepay={_dash_none(balance.get('prepay_balance_cny'))}, "
        f"arrears={_dash_none(balance.get('arrears_cny'))}"
        ")"
    )


def _series_summary(series: Any) -> str:
    if not isinstance(series, dict):
        return "0(-)"
    latest = series.get("latest_date") or series.get("latest_month") or "-"
    return f"{series.get('count', 0)}({series.get('range') or '-'}, latest={latest})"


def _yearly_summary(yearly: Any) -> str:
    if not isinstance(yearly, dict) or not yearly.get("present"):
        return "no"
    return f"{yearly.get('year') or '-'} usage={_dash_none(yearly.get('usage_kwh'))} charge={_dash_none(yearly.get('charge_cny'))}"


def _dash_none(value: Any) -> str:
    return "-" if value is None else str(value)


def _mask_accounts_and_phones(text: str) -> str:
    text = _ACCOUNT_RE.sub(lambda m: mask_account_no(m.group(1)), str(text))
    text = _LONG_NUMERIC_ID_RE.sub("<redacted-numeric-id>", text)
    text = _PHONE_RE.sub(lambda m: mask_account_no(m.group(1), keep_last=2), text)
    text = _ID_CARD_RE.sub("<redacted-id>", text)
    return _EMAIL_RE.sub("<redacted-email>", text)


def _is_secret_key(key: str) -> bool:
    if key == "<sensitive>":
        return True
    return bool(_SECRET_KEY_RE.search(key or ""))


def _is_pii_key(key: str) -> bool:
    if str(key or "").casefold() in _NON_PII_TECHNICAL_KEYS:
        return False
    return bool(_PII_KEY_RE.search(key or ""))


def _is_account_key(key: str) -> bool:
    return bool(_ACCOUNT_KEY_RE.search(key or ""))


def _safe_output_key(key: str) -> str:
    if _is_secret_key(key):
        return "<sensitive>"
    return _truncate(key, 80)


def _path_key(key: Any) -> str:
    text = _safe_output_key(str(key))
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", text):
        return text
    if text == "<sensitive>":
        return text
    return "['" + text.replace("'", "\\'") + "']"


def _path_leaf(path: str) -> str:
    if "." in path:
        return path.rsplit(".", 1)[-1].strip("[]'")
    return path.strip("[]'")


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__


def _truncate(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
