import json
import logging
import os
import time
from typing import Optional
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
import requests

from .config import FetcherConfig
from .redact import redact_text, redact_url


def build_driver(config: FetcherConfig):
    chrome_options = webdriver.ChromeOptions()

    browser_lang = os.getenv("BROWSER_LANGUAGE", "zh-CN,zh,en-US,en")
    browser_ua = os.getenv("BROWSER_USER_AGENT", "")
    device_scale = os.getenv("BROWSER_DEVICE_SCALE_FACTOR", "2")
    window_size = os.getenv("BROWSER_WINDOW_SIZE", "1280,900")
    profile_dir = os.getenv("SGCC_BROWSER_PROFILE", "/data/chrome-profile")
    browser_mode = _normalize_browser_mode(os.getenv("SGCC_BROWSER_MODE", "local"))
    browser_service_mode = browser_mode in {
        "browser-service",
        "browser_service",
        "sidecar",
        "container-google-cdp",
        "container_google_cdp",
    }
    cdp_mode = browser_service_mode or browser_mode in {
        "cdp",
        "cdp_attach",
        "host_cdp",
        "host-cdp",
        "remote_debugging",
        "remote-debugging",
    }

    service = _chrome_service(cdp_mode=cdp_mode)

    if cdp_mode:
        cdp_address = _cdp_address_from_env()
        if browser_service_mode:
            _browser_service_start()
            logging.info(f"使用官方 Google Chrome browser-service CDP: {cdp_address}")
        else:
            logging.info(f"使用外部 Google Chrome CDP: {cdp_address}")

        # 连接到已经启动的官方 Google Chrome。这里不再传
        # user-data-dir / binary_location / 启动参数，避免 ChromeDriver
        # 新开容器内 Debian Chromium。
        chrome_options.add_experimental_option("debuggerAddress", cdp_address)
    else:
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--start-maximized")
        chrome_options.add_argument(f"--user-data-dir={profile_dir}")

        # 反检测核心参数（参考 ha-95598）
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option("useAutomationExtension", False)

        chrome_options.add_argument(f"--lang={browser_lang}")
        chrome_options.add_argument(f"--window-size={window_size}")
        chrome_options.add_argument(f"--force-device-scale-factor={device_scale}")
        chrome_options.add_argument("--high-dpi-support=1")
        if browser_ua:
            chrome_options.add_argument(f"user-agent={browser_ua}")

        chrome_options.add_experimental_option("prefs", {
            "intl.accept_languages": browser_lang,
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        })

        if 'PYTHON_IN_DOCKER' in os.environ:
            chrome_options.binary_location = "/usr/bin/chromium"

        logging.info(f"使用 Chromium profile: {profile_dir}")

    def _setting_driver(driver):
        if cdp_mode and os.getenv("SGCC_CDP_SET_WINDOW_SIZE", "").strip().lower() not in {"1", "true", "yes"}:
            return
        width, height = map(int, window_size.split(','))
        try:
            driver.set_window_size(width, height)
        except Exception as e:
            logging.warning(f"设置窗口大小失败: {e}")
        try:
            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                "width": width,
                "height": height,
                "deviceScaleFactor": int(device_scale),
                "mobile": False,
                "dontSetVisibleSize": False
            })
        except Exception as e:
            logging.warning(f"CDP 设置 viewport 失败: {e}")

    driver = webdriver.Chrome(options=chrome_options, service=service)
    driver._sgcc_attached_browser = cdp_mode
    driver._sgcc_browser_service_mode = browser_service_mode
    driver.implicitly_wait(config.DRIVER_IMPLICITY_WAIT_TIME)
    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
    try:
        driver.set_script_timeout(int(os.getenv("BROWSER_SCRIPT_TIMEOUT", "20")))
    except Exception as e:
        logging.warning(f"设置脚本超时失败: {e}")


    _apply_browser_consistency(driver, browser_lang)

    _setting_driver(driver)
    _log_browser_runtime(driver)

    return driver


def _apply_browser_consistency(driver, browser_lang: str) -> None:
    languages = [item.strip() for item in browser_lang.split(",") if item.strip()]
    primary_language = languages[0] if languages else "zh-CN"
    script = f"""
        (() => {{
            const define = (target, key, value) => {{
                try {{
                    Object.defineProperty(target, key, {{
                        get: () => value,
                        configurable: true
                    }});
                }} catch (error) {{}}
            }};
            define(Navigator.prototype, 'webdriver', undefined);
            define(Navigator.prototype, 'languages', {json.dumps(languages, ensure_ascii=False)});
            define(Navigator.prototype, 'language', {json.dumps(primary_language, ensure_ascii=False)});
            window.chrome = window.chrome || {{runtime: {{}}}};
        }})();
    """
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": script},
        )
    except Exception as error:
        logging.warning(f"CDP 注入浏览器一致性脚本失败: {error}")

    try:
        driver.execute_script(script)
    except Exception as error:
        logging.warning(f"当前页面应用浏览器一致性脚本失败: {error}")

    try:
        driver.execute_cdp_cmd(
            "Emulation.setTimezoneOverride",
            {"timezoneId": os.getenv("BROWSER_TIMEZONE", "Asia/Shanghai")},
        )
    except Exception as error:
        logging.warning(f"CDP 设置 timezone 失败: {error}")

    try:
        driver.execute_cdp_cmd(
            "Emulation.setLocaleOverride",
            {"locale": primary_language},
        )
    except Exception as error:
        logging.warning(f"CDP 设置 locale 失败: {error}")


def collect_browser_runtime(driver, stage: str = "runtime") -> dict:
    """Collect a credential-free browser fingerprint snapshot for diagnostics."""
    snapshot = {
        "stage": stage,
        "browser_mode": _normalize_browser_mode(os.getenv("SGCC_BROWSER_MODE", "local")),
        "configured": {
            "language": os.getenv("BROWSER_LANGUAGE", "zh-CN,zh,en-US,en"),
            "timezone": os.getenv("BROWSER_TIMEZONE", "Asia/Shanghai"),
            "window_size": os.getenv("BROWSER_WINDOW_SIZE", "1280,900"),
            "device_scale_factor": os.getenv("BROWSER_DEVICE_SCALE_FACTOR", "2"),
            "cdp_address": _cdp_address_from_env(),
        },
    }
    try:
        snapshot["page"] = driver.execute_script("""
            const webgl = (() => {
                try {
                    const canvas = document.createElement('canvas');
                    const gl = canvas.getContext('webgl') ||
                        canvas.getContext('experimental-webgl');
                    if (!gl) return {available: false};
                    const info = gl.getExtension('WEBGL_debug_renderer_info');
                    return {
                        available: true,
                        vendor: info ? gl.getParameter(info.UNMASKED_VENDOR_WEBGL) : null,
                        renderer: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : null,
                        version: gl.getParameter(gl.VERSION),
                        shadingLanguageVersion: gl.getParameter(gl.SHADING_LANGUAGE_VERSION)
                    };
                } catch (error) {
                    return {available: false, error: error.name};
                }
            })();
            return {
                userAgent: navigator.userAgent,
                webdriver: navigator.webdriver,
                platform: navigator.platform,
                vendor: navigator.vendor,
                language: navigator.language,
                languages: Array.from(navigator.languages || []),
                hardwareConcurrency: navigator.hardwareConcurrency,
                deviceMemory: navigator.deviceMemory || null,
                maxTouchPoints: navigator.maxTouchPoints,
                cookieEnabled: navigator.cookieEnabled,
                doNotTrack: navigator.doNotTrack,
                pdfViewerEnabled: navigator.pdfViewerEnabled,
                screen: {
                    width: screen.width,
                    height: screen.height,
                    availWidth: screen.availWidth,
                    availHeight: screen.availHeight,
                    colorDepth: screen.colorDepth,
                    pixelDepth: screen.pixelDepth
                },
                window: {
                    innerWidth: window.innerWidth,
                    innerHeight: window.innerHeight,
                    outerWidth: window.outerWidth,
                    outerHeight: window.outerHeight,
                    devicePixelRatio: window.devicePixelRatio
                },
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                locale: Intl.DateTimeFormat().resolvedOptions().locale,
                pluginNames: Array.from(navigator.plugins || []).map((item) => item.name).slice(0, 20),
                mimeTypeCount: (navigator.mimeTypes || []).length,
                automationGlobals: Object.keys(window).filter((key) =>
                    key.startsWith('cdc_') ||
                    key.startsWith('__webdriver') ||
                    key.startsWith('__selenium')
                ).slice(0, 30),
                webgl
            };
        """)
        snapshot["current_url"] = redact_url(str(getattr(driver, "current_url", "") or ""))
    except Exception as error:
        snapshot["page_error"] = redact_text(f"{type(error).__name__}: {error}")

    capabilities = getattr(driver, "capabilities", {}) or {}
    chrome = capabilities.get("chrome") or {}
    snapshot["webdriver"] = {
        "browser_name": capabilities.get("browserName"),
        "browser_version": capabilities.get("browserVersion"),
        "platform_name": capabilities.get("platformName"),
        "page_load_strategy": capabilities.get("pageLoadStrategy"),
        "chromedriver_version": str(chrome.get("chromedriverVersion") or "").split(" ", 1)[0],
    }

    try:
        version = driver.execute_cdp_cmd("Browser.getVersion", {})
        snapshot["cdp_version"] = {
            key: version.get(key)
            for key in (
                "protocolVersion",
                "product",
                "revision",
                "userAgent",
                "jsVersion",
            )
        }
    except Exception as error:
        snapshot["cdp_version_error"] = redact_text(
            f"{type(error).__name__}: {error}"
        )

    try:
        system_info = driver.execute_cdp_cmd("SystemInfo.getInfo", {})
        gpu = system_info.get("gpu") or {}
        snapshot["system_info"] = {
            "model_name": system_info.get("modelName"),
            "model_version": system_info.get("modelVersion"),
            "gpu_devices": [
                {
                    key: device.get(key)
                    for key in (
                        "vendorId",
                        "deviceId",
                        "subSysId",
                        "revision",
                        "vendorString",
                        "deviceString",
                        "driverVendor",
                        "driverVersion",
                    )
                }
                for device in (gpu.get("devices") or [])[:8]
            ],
            "aux_attributes": {
                key: value
                for key, value in (gpu.get("auxAttributes") or {}).items()
                if key in {
                    "displayType",
                    "glImplementationParts",
                    "glRenderer",
                    "glVendor",
                    "glVersion",
                    "passthroughCmdDecoder",
                    "sandboxed",
                }
            },
            "feature_status": gpu.get("featureStatus") or {},
        }
    except Exception as error:
        snapshot["system_info_error"] = redact_text(
            f"{type(error).__name__}: {error}"
        )

    return snapshot


def _debug_browser_runtime_enabled() -> bool:
    return any(
        os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}
        for name in ("SGCC_FORCE_DEBUG", "SGCC_DEBUG", "SGCC_DIAG", "DEBUG_MODE")
    )


def _log_browser_runtime(driver, stage: str = "driver_ready") -> Optional[dict]:
    if not _debug_browser_runtime_enabled():
        return None
    try:
        snapshot = collect_browser_runtime(driver, stage=stage)
        history = getattr(driver, "_sgcc_browser_runtime", None)
        if not isinstance(history, list):
            history = []
            driver._sgcc_browser_runtime = history
        history.append(snapshot)
        logging.info(
            "浏览器运行态（不含凭证）: %s",
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
        )
        return snapshot
    except Exception as error:
        logging.warning(f"读取浏览器运行态失败: {error}")
        return None


def _normalize_browser_mode(value: str) -> str:
    return (value or "local").strip().lower()


def _cdp_address_from_env() -> str:
    raw_address = os.getenv("SGCC_CDP_ADDRESS", "").strip()
    if raw_address:
        return raw_address

    raw_url = os.getenv("SGCC_CDP_URL", "").strip()
    if raw_url:
        parsed = urlparse(raw_url)
        if parsed.netloc:
            return parsed.netloc
        if parsed.path:
            return parsed.path

    host = os.getenv("SGCC_CDP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.getenv("SGCC_CDP_PORT", "9222").strip() or "9222"
    return f"{host}:{port}"


def _browser_service_url() -> str:
    return os.getenv("SGCC_BROWSER_SERVICE_URL", "http://127.0.0.1:39222").strip().rstrip("/")


def _browser_service_start() -> None:
    """Ask the browser-service manager to start official Google Chrome on demand."""
    url = _browser_service_url()
    timeout = float(os.getenv("SGCC_BROWSER_SERVICE_TIMEOUT", "90"))
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            response = requests.post(f"{url}/start", timeout=10)
            if response.status_code < 400:
                try:
                    status = response.json()
                except ValueError:
                    status = {}
                app_revision = os.getenv("VERSION", "").strip() or "unknown"
                browser_revision = str(status.get("revision") or "unknown")
                logging.info(
                    "browser-service 已就绪: app_revision=%s, browser_revision=%s, ready=%s",
                    app_revision,
                    browser_revision,
                    status.get("ready"),
                )
                if (
                    app_revision != "unknown"
                    and browser_revision != "unknown"
                    and app_revision != browser_revision
                ):
                    logging.warning(
                        "app/browser 镜像 revision 不一致: app=%s, browser=%s；"
                        "请将两个镜像固定到同一个 tag 或 SHA。",
                        app_revision,
                        browser_revision,
                    )
                return
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
        except Exception as e:
            last_error = e
        time.sleep(1)
    raise RuntimeError(f"启动 SGCC Chrome browser-service 失败: {redact_text(last_error)}")


def _browser_service_stop() -> None:
    url = _browser_service_url()
    try:
        response = requests.post(f"{url}/stop", timeout=10)
        if response.status_code >= 400:
            logging.warning(f"停止 SGCC Chrome browser-service 失败: HTTP {response.status_code}: {redact_text(response.text[:300])}")
    except Exception as e:
        logging.warning(f"停止 SGCC Chrome browser-service 异常: {redact_text(e)}")


def _chrome_service(cdp_mode: bool = False) -> ChromeService:
    configured = os.getenv("SGCC_CHROMEDRIVER_PATH", "").strip()
    if configured:
        return ChromeService(executable_path=configured)

    if 'PYTHON_IN_DOCKER' in os.environ:
        # local mode drives Debian Chromium, so keep Debian chromedriver first.
        # browser-service/CDP mode attaches to official Google Chrome; prefer the
        # Chrome-for-Testing driver installed beside google-chrome-stable.
        candidates = (
            ["/usr/local/bin/chromedriver", "/usr/bin/chromedriver"]
            if cdp_mode
            else ["/usr/bin/chromedriver", "/usr/local/bin/chromedriver"]
        )
        for path in candidates:
            if os.path.exists(path):
                return ChromeService(executable_path=path)
        return ChromeService(executable_path="/usr/bin/chromedriver")
    return _find_chromedriver()


def _find_chromedriver() -> ChromeService:
    """在非 Docker 环境中查找可用的 ChromeDriver。"""
    import shutil

    # 1) 尝试系统 PATH
    path = shutil.which("chromedriver") or shutil.which("chromedriver.exe")
    if path:
        return ChromeService(executable_path=path)

    # 2) 尝试 CloakBrowser 缓存的 chromedriver（如果有）
    for base in [
        os.path.expanduser("~/.cloakbrowser"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), ".cloakbrowser"),
    ]:
        try:
            for root, dirs, files in os.walk(base):
                if "chromedriver.exe" in files or "chromedriver" in files:
                    fname = "chromedriver.exe" if "chromedriver.exe" in files else "chromedriver"
                    path = os.path.join(root, fname)
                    if os.path.isfile(path):
                        return ChromeService(executable_path=path)
                # 最多扫描两级目录
                if len(root) - len(base) > 200:
                    dirs.clear()
        except Exception:
            pass

    # 3) 尝试 Selenium Manager 自动下载
    try:
        return ChromeService()
    except Exception:
        pass

    raise RuntimeError(
        "ChromeDriver 未找到。请安装 ChromeDriver 或运行: pip install chromedriver-binary-auto"
    )


def release_driver(driver) -> None:
    attached_browser = bool(getattr(driver, "_sgcc_attached_browser", False))
    browser_service_mode = bool(getattr(driver, "_sgcc_browser_service_mode", False))
    try:
        if attached_browser:
            try:
                driver.command_executor.close()
            except Exception:
                pass
            try:
                driver.service.stop()
            except Exception:
                pass
            if browser_service_mode and os.getenv("SGCC_BROWSER_SERVICE_STOP_ON_RELEASE", "true").strip().lower() not in {"0", "false", "no"}:
                _browser_service_stop()
                logging.info("浏览器驱动已断开；browser-service Chrome 已按需关闭。")
            else:
                logging.info("浏览器驱动已断开；外部 Chrome 保持原运行状态。")
            return
        driver.quit()
        logging.info("数据抓取完成后浏览器驱动退出。")
    except Exception as e:
        logging.warning(f"浏览器驱动退出失败: {redact_text(e)}")
