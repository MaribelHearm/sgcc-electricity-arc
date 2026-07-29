import logging
import os
import random
import threading
import time
from typing import Optional

from .browser import build_driver, collect_browser_runtime, release_driver
from .cache_validity import has_useful_account_data
from .config import FetcherConfig
from .const import LOGIN_URL
from .diag import DiagnosticCollector, debug_enabled
from .entity_identity import (
    legacy_alias_policy,
    mqtt_legacy_action,
    mqtt_remove_legacy_on_cleanup,
)
from .error_watcher import ErrorWatcher
from .ha_mapping import account_data_summary, account_data_to_update_args, with_history_daily_if_empty
from .login import SgccLogin
from .login_guard import (
    LoginFailure,
    NonRetryableFetchError,
    clear_login_cooldown,
    env_bool,
    get_login_cooldown,
    set_login_cooldown,
    should_retry_login_failure,
)
from .mqtt_publisher import MqttPublisher
from .model import FetchRun, mask_account_no
from .network_capture import NetworkRecorder
from .redact import install_account_log_redaction, mask_secret, now_iso, redact_text
from .scraper import Scraper, redact_account_data
from .sensor_updator import SensorUpdator
from .session import check_session
from .store import Store

_FETCH_LOCK = threading.Lock()
_PATH_B_SESSION_ATTEMPTS = 2


class SessionExpiredFetchError(Exception):
    """Path B could not continue because the authenticated session expired."""


class DataFetcher:

    def __init__(self, username: str, password: str):
        self.config = FetcherConfig.from_env()
        self._username = username
        self._password = password

    @staticmethod
    def _mask_secret(value: str, keep_last: int = 2) -> str:
        return mask_secret(value, keep_last=keep_last)

    @staticmethod
    def _redact_text(value) -> str:
        return redact_text(value)

    @staticmethod
    def _allow_login_fallback(trigger_type: str) -> bool:
        return (
            trigger_type == "manual"
            or env_bool("SGCC_LOGIN_FALLBACK_UNATTENDED", False)
            or env_bool("SGCC_QRCODE_FALLBACK_UNATTENDED", False)
        )

    def _random_delay(self, min_seconds=0.5, max_seconds=3.0):
        """添加随机延迟，使自动化操作更难被检测。"""
        delay = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)

    def _mqtt_legacy_policy(
        self,
        account_nos,
        *,
        published_account_nos,
        authoritative: bool,
    ):
        return legacy_alias_policy(
            account_nos,
            published_account_nos=published_account_nos,
            authoritative=authoritative,
        )

    def fetch(self, trigger_type: str = "manual"):

        """主逻辑：登录链路保持原样，数据抓取切换到 Path B + Store。"""

        install_account_log_redaction()
        diag = DiagnosticCollector(trigger_type=trigger_type) if debug_enabled() else None

        if not _FETCH_LOCK.acquire(blocking=False):
            skipped_run_id = self._record_skipped_busy_run(trigger_type)
            logging.info("已有抓取任务正在运行，本次 fetch 标记为 skipped_busy 后跳过。")
            if diag is not None:
                diag.set_run_id(skipped_run_id)
                diag.record_runtime(self.config, stage="skipped_busy")
                diag.record_error("FetchBusy", "已有抓取任务正在运行", stage="lock")
                diag.emit("skipped_busy")
            return "skipped_busy"

        driver = None
        network_recorder = None
        store = None
        run_id = None
        fetch_status = "failed"
        session_status_before = "unknown"
        session_status_after = "unknown"
        try:
            store = Store()
            run_id = store.start_run(FetchRun(
                trigger_type=trigger_type,
                started_at=now_iso(),
            ))
            if diag is not None:
                diag.set_run_id(run_id)
                diag.record_runtime(self.config, stage="start")

            if env_bool("SGCC_LOGIN_COOLDOWN_ENABLED", True) and trigger_type != "manual":
                cooldown = get_login_cooldown()
                if cooldown.active:
                    message = (
                        f"登录风控冷却中，剩余 {cooldown.remaining_seconds // 60} 分钟，"
                        f"跳过本次无人值守登录: {cooldown.reason}"
                    )
                    logging.warning(message)
                    store.finish_run(
                        run_id,
                        "skipped_cooldown",
                        session_status_before=session_status_before,
                        session_status_after=session_status_after,
                        error_type="LoginCooldown",
                        error_message_redacted=redact_text(message),
                    )
                    fetch_status = "skipped_cooldown"
                    if diag is not None:
                        diag.record_error("LoginCooldown", message, stage="cooldown")
                    return "skipped_cooldown"

            driver = build_driver(self.config)
            ErrorWatcher.instance().set_driver(driver)

            self._random_delay(1, 3)
            logging.info("浏览器驱动已初始化。")
            publisher = self.config.PUBLISHER
            if publisher not in {"rest", "mqtt", "both"}:
                logging.warning(f"未知 PUBLISHER={publisher}，回退为 mqtt。")
                publisher = "mqtt"
            if diag is not None:
                diag.record_runtime(self.config, publisher=publisher, stage="browser_ready")
                startup_captures = getattr(driver, "_sgcc_browser_runtime", None) or []
                for snapshot in startup_captures:
                    diag.record_browser_runtime(
                        str(snapshot.get("stage") or "driver_ready"),
                        snapshot,
                    )
                network_recorder = NetworkRecorder(
                    driver,
                    unscoped_metadata_only=True,
                )
                network_started = network_recorder.start()
                diag.record_timeline(
                    "login_network_recorder_started",
                    success=network_started,
                    cdp_address=network_recorder.cdp_address,
                    response_bodies=False,
                )
            updator = SensorUpdator() if publisher in {"rest", "both"} else None
            mqtt_pub = MqttPublisher(self.config) if publisher in {"mqtt", "both"} else None
            mqtt_connected = mqtt_pub.connect() if mqtt_pub is not None else False

            before_check = check_session(driver, "before_login")
            session_status_before = before_check.status
            store.record_session_check(before_check)
            if diag is not None:
                diag.record_session("before_login", before_check)

            try:
                login_client = SgccLogin(
                    driver,
                    self._username,
                    self._password,
                    self.config,
                    diagnostic=diag,
                )
                login_method = os.getenv("SGCC_LOGIN_METHOD", "password").strip().lower()
                allow_fallback = self._allow_login_fallback(trigger_type)
                if login_method in {"phone-code", "phone_code", "sms"}:
                    logged_in = login_client.login(phone_code=True, allow_fallback=False)
                else:
                    logged_in = login_client.login(allow_fallback=allow_fallback)
                if logged_in:
                    logging.info("登录成功!")
                else:
                    logging.info("登录失败!")
                    raise LoginFailure("login_failed", "login unsuccessed")
            except LoginFailure as e:
                if not should_retry_login_failure(e.category):
                    cooldown = set_login_cooldown(f"{e.category}: {e.message}")
                    logging.error(
                        f"登录失败[{e.category}]，判定为不应立即重试；已熔断到 "
                        f"{cooldown.until.isoformat() if cooldown.until else 'unknown'}: {redact_text(e.message)}"
                    )
                    raise NonRetryableFetchError(str(e)) from e
                logging.error(
                    f"登录失败[{e.category}]，原因: {redact_text(e.message)}。"
                    f"还剩 {self.config.RETRY_TIMES_LIMIT} 次重试机会。"
                )
                raise
            except Exception as e:
                logging.error(
                    f"浏览器驱动异常，原因: {redact_text(e)}。还剩 {self.config.RETRY_TIMES_LIMIT} 次重试机会。")
                raise

            logging.info(f"在 {LOGIN_URL} 登录成功")
            after_login_check = check_session(driver, "after_login")
            store.record_session_check(after_login_check)
            session_status_after = after_login_check.status
            if diag is not None:
                diag.record_session("after_login", after_login_check)
                diag.record_browser_runtime(
                    "after_login",
                    collect_browser_runtime(driver, stage="after_login"),
                )

            if after_login_check.status != "authenticated":
                raise Exception(f"session not authenticated after login: {after_login_check.status}")

            if network_recorder is None or not network_recorder.started:
                network_recorder = NetworkRecorder(driver)
                network_started = network_recorder.start()
                if diag is not None:
                    diag.record_timeline(
                        "network_recorder_started",
                        success=network_started,
                        cdp_address=network_recorder.cdp_address,
                    )
            else:
                network_started = network_recorder.started
                if diag is not None:
                    diag.record_timeline(
                        "network_recorder_continued_after_login",
                        success=network_started,
                    )

            self._random_delay(1, 3)
            logging.info("开始使用动态多源 Path B 抓取账户数据。")
            scraper = Scraper(
                driver,
                diagnostic=diag,
                network_recorder=network_recorder if network_started else None,
            )
            account_data_list = self._fetch_path_b_in_session(
                scraper,
                driver,
                store,
                diag,
            )

            discovered_account_nos = {
                account_data.account.account_no
                for account_data in account_data_list
                if account_data.account.account_no
            }
            published_account_nos = {
                account_data.account.account_no
                for account_data in account_data_list
                if (
                    account_data.account.account_no
                    and account_data.account.account_no not in self.config.IGNORE_USER_ID
                    and has_useful_account_data(account_data)
                )
            }
            mqtt_legacy_policy = self._mqtt_legacy_policy(
                discovered_account_nos,
                published_account_nos=published_account_nos,
                authoritative=bool(scraper.account_set_authoritative),
            )
            saved_count = 0
            for account_data in account_data_list:
                user_id = account_data.account.account_no
                masked_user_id = mask_account_no(user_id)
                if not user_id:
                    logging.warning("Path B 返回了缺少户号的账户数据，已跳过。")
                    if diag is not None:
                        diag.record_account_skipped(account_data, "missing_account_no")
                    continue
                if user_id in self.config.IGNORE_USER_ID:
                    logging.info(f"用户 ID {masked_user_id} 将被忽略")
                    if diag is not None:
                        diag.record_account_skipped(account_data, "ignored_by_config")
                    continue

                if not has_useful_account_data(account_data):
                    logging.warning(
                        f"用户 [{masked_user_id}] Path B 只返回户号/元数据，没有任何有效国网业务数据，已跳过。"
                    )
                    if diag is not None:
                        diag.record_account_skipped(account_data, "no_useful_business_data")
                    continue

                store.save_account_data(account_data, run_id)
                saved_count += 1
                if diag is not None:
                    diag.record_account_saved(account_data)
                logging.info(f"用户 [{masked_user_id}] Path B 数据已写入 Store: {account_data_summary(account_data)}")
                logging.debug(f"用户 [{masked_user_id}] Path B 脱敏数据: {redact_account_data(account_data)}")

                publish_account_data = with_history_daily_if_empty(account_data, store, limit=31)
                if publish_account_data is not account_data:
                    logging.info(
                        f"用户 [{masked_user_id}] 本次抓取 daily 为空，发布前从 Store 回填 "
                        f"{len(publish_account_data.daily)} 条历史日用电数据。"
                    )

                update_args = account_data_to_update_args(publish_account_data)
                cache_args = account_data_to_update_args(account_data)
                logging.info(
                    f"用户 [{masked_user_id}] 数据获取完成: 余额={update_args['balance']}元, "
                    f"预付费余额={update_args['prepay_balance']}元, "
                    f"应交金额={update_args['arrears']}元, "
                    f"最近日用电={update_args['last_daily_usage']}度({update_args['last_daily_date']}), "
                    f"年度用电={update_args['yearly_usage']}度, 年度电费={update_args['yearly_charge']}元, "
                    f"月用电={update_args['month_usage']}度, 月电费={update_args['month_charge']}元")
                if updator is not None:
                    try:
                        rest_result = updator.update_one_userid(
                            **update_args,
                            cache_values=cache_args,
                        )
                        rest_success = rest_result is not False
                        rest_detail = (
                            "ok"
                            if rest_success
                            else "update_one_userid returned false"
                        )
                    except Exception as rest_error:
                        rest_success = False
                        rest_detail = f"exception: {redact_text(rest_error)}"
                        logging.warning(
                            f"用户 [{masked_user_id}] Home Assistant REST 发布异常，"
                            "国网抓取和本地 Store 已完成，不触发重新登录: "
                            f"{redact_text(rest_error)}"
                        )
                    if diag is not None:
                        diag.record_publish(
                            user_id,
                            "ha_rest",
                            rest_success,
                            rest_detail,
                        )
                    if not rest_success:
                        logging.warning(
                            f"用户 [{masked_user_id}] Home Assistant REST 发布未完全成功；国网抓取和本地 Store 已完成，不触发重新登录。"
                        )
                if mqtt_pub is not None:
                    try:
                        if not mqtt_connected:
                            logging.warning(f"用户 [{masked_user_id}] MQTT 未连接，跳过发布。")
                            if diag is not None:
                                diag.record_publish(user_id, "mqtt", False, "not_connected")
                        else:
                            mqtt_success = bool(mqtt_pub.publish_account_data(
                                publish_account_data,
                                legacy_action=mqtt_legacy_action(
                                    self.config.MQTT_LEGACY_DISCOVERY_MODE,
                                    user_id,
                                    mqtt_legacy_policy,
                                ),
                            ))
                            if diag is not None:
                                diag.record_publish(
                                    user_id,
                                    "mqtt",
                                    mqtt_success,
                                    "ok" if mqtt_success else "publish_account_data returned false",
                                )
                            if not mqtt_success:
                                logging.warning(f"用户 [{masked_user_id}] MQTT 发布失败，已跳过。")
                    except Exception as mqtt_error:
                        if diag is not None:
                            diag.record_publish(
                                user_id,
                                "mqtt",
                                False,
                                f"exception: {redact_text(mqtt_error)}",
                            )
                        logging.warning(f"用户 [{masked_user_id}] MQTT 发布异常，已忽略: {redact_text(mqtt_error)}")

            if saved_count == 0:
                raise NonRetryableFetchError(
                    "Path B 抓取结果没有可保存账户数据，已停止外层重试以避免重复登录"
                )

            final_check = check_session(driver, "after_fetch")
            store.record_session_check(final_check)
            session_status_after = final_check.status
            if diag is not None:
                diag.record_session("after_fetch", final_check)

            store.finish_run(
                run_id,
                "success",
                session_status_before=session_status_before,
                session_status_after=session_status_after,
            )

            try:
                cleanup_account_nos: set[str] = set(self.config.IGNORE_USER_ID or [])
                if scraper.account_set_authoritative and discovered_account_nos:
                    deactivated_account_nos = store.reconcile_active_accounts(
                        discovered_account_nos,
                        run_id,
                    )
                    cleanup_account_nos.update(deactivated_account_nos)
                    if deactivated_account_nos:
                        logging.info(
                            f"Path B 已将 {len(deactivated_account_nos)} 个本轮未出现的历史户号标记为 inactive。"
                        )
                else:
                    logging.warning(
                        "Path B 本轮账户枚举不完整，跳过历史户号核销，避免部分抓取误删有效账户。"
                    )

                if mqtt_pub is not None and mqtt_connected:
                    for cleanup_account_no in sorted(cleanup_account_nos):
                        cleanup_data = store.get_account_data(cleanup_account_no)
                        if cleanup_data is None:
                            continue
                        cleanup_success = mqtt_pub.remove_account_data(
                            cleanup_data,
                            remove_legacy=mqtt_remove_legacy_on_cleanup(
                                self.config.MQTT_LEGACY_DISCOVERY_MODE,
                                cleanup_account_no,
                                mqtt_legacy_policy,
                            ),
                        )
                        if diag is not None:
                            diag.record_publish(
                                cleanup_account_no,
                                "mqtt_cleanup",
                                cleanup_success,
                                "ok" if cleanup_success else "remove_account_data returned false",
                            )
            except Exception as lifecycle_error:
                if diag is not None:
                    diag.record_error(lifecycle_error, stage="account_lifecycle")
                logging.warning(
                    f"账户 active 核销/MQTT 清理失败，抓取数据仍保留: {redact_text(lifecycle_error)}"
                )

            logging.info(f"抓取运行 {run_id} 完成: success, 账户数={saved_count}, 会话={session_status_after}")
            clear_login_cooldown()
            fetch_status = "success"
            return "success"
        except Exception as e:
            if driver is not None and store is not None:
                try:
                    failed_check = check_session(driver, "failed")
                    store.record_session_check(failed_check)
                    session_status_after = failed_check.status
                    if diag is not None:
                        diag.record_session("failed", failed_check)
                except Exception:
                    pass
            if store is not None and run_id is not None:
                try:
                    store.finish_run(
                        run_id,
                        "failed",
                        session_status_before=session_status_before,
                        session_status_after=session_status_after,
                        error_type=type(e).__name__,
                        error_message_redacted=redact_text(e),
                    )
                except Exception as finish_error:
                    logging.warning(f"记录 fetch run 失败状态失败: {redact_text(finish_error)}")
            if diag is not None:
                diag.record_error(e, stage="fetch")
                if driver is not None:
                    diag.record_browser_runtime(
                        "fetch_failed",
                        collect_browser_runtime(driver, stage="fetch_failed"),
                    )
            raise
        finally:
            if network_recorder is not None:
                network_recorder.flush()
                network_recorder.stop()
                if diag is not None:
                    diag.record_observations(network_recorder.observations())
                    for error in network_recorder.errors:
                        diag.record_error("NetworkRecorder", error, stage="network")
                    diag.record_timeline(
                        "network_recorder_stopped",
                        observation_count=len(network_recorder.observations()),
                        error_count=len(network_recorder.errors),
                    )
            if driver is not None:
                if diag is not None:
                    diag.record_browser_runtime(
                        "before_release",
                        collect_browser_runtime(driver, stage="before_release"),
                    )
                release_driver(driver)
            if "mqtt_pub" in locals() and mqtt_pub is not None:
                mqtt_pub.disconnect()
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass
            if diag is not None:
                try:
                    diag.emit(fetch_status)
                except Exception as diag_error:
                    logging.warning(f"SGCC DIAG 输出失败，已忽略: {redact_text(diag_error)}")
            _FETCH_LOCK.release()

    def _fetch_path_b_in_session(self, scraper, driver, store, diag=None):
        """Retry transient empty Path B results without creating another login session."""
        last_status = "unknown"
        for attempt in range(1, _PATH_B_SESSION_ATTEMPTS + 1):
            account_data_list = scraper.fetch_all()
            if diag is not None:
                diag.record_fetched_accounts(len(account_data_list))

            eligible_accounts = [
                account_data
                for account_data in account_data_list
                if account_data.account.account_no not in self.config.IGNORE_USER_ID
            ]
            useful_accounts = [
                account_data
                for account_data in eligible_accounts
                if has_useful_account_data(account_data)
            ]
            if useful_accounts:
                return account_data_list

            if account_data_list and not eligible_accounts:
                raise NonRetryableFetchError(
                    "Path B 返回的账户均被 IGNORE_USER_ID 忽略，已停止本轮抓取"
                )

            session_check = check_session(driver, f"path_b_empty_attempt_{attempt}")
            store.record_session_check(session_check)
            last_status = session_check.status
            if diag is not None:
                diag.record_session(f"path_b_empty_attempt_{attempt}", session_check)
                diag.record_timeline(
                    "path_b_empty",
                    attempt=attempt,
                    account_count=len(account_data_list),
                    eligible_count=len(eligible_accounts),
                    session_status=session_check.status,
                )

            if session_check.status == "expired":
                raise SessionExpiredFetchError(
                    f"Path B 第 {attempt} 次未获得业务数据，且当前 session 已过期"
                )

            if attempt < _PATH_B_SESSION_ATTEMPTS:
                logging.warning(
                    f"Path B 第 {attempt} 次未获得有效业务数据，session={session_check.status}；"
                    "将在当前已认证浏览器会话内再尝试一次，不重新登录。"
                )
                self._random_delay(1, 3)

        raise NonRetryableFetchError(
            f"Path B 在当前会话内连续 {_PATH_B_SESSION_ATTEMPTS} 次未获得有效业务数据，"
            f"session={last_status}；已停止外层重试以避免重复登录"
        )

    def _record_skipped_busy_run(self, trigger_type: str) -> Optional[int]:
        try:
            with Store() as store:
                now = now_iso()
                return store.start_run(FetchRun(
                    trigger_type=trigger_type,
                    status="skipped_busy",
                    started_at=now,
                    finished_at=now,
                    session_status_before="unknown",
                    session_status_after="unknown",
                ))
        except Exception as e:
            logging.warning(f"记录 skipped_busy fetch run 失败: {redact_text(e)}")
        return None
