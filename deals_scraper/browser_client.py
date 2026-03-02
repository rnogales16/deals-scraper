"""Cliente Playwright anti-ban: stealth avanzado, delays gaussianos, proxies."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# User agents pool: Chrome 120-125, Firefox 123-126, Safari 17, Edge 124-125
# ---------------------------------------------------------------------------
_USER_AGENTS = [
    # Chrome 120 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Chrome 121 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    # Chrome 122 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome 123 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome 124 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 125 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 120 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Chrome 122 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome 124 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 125 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 123 – Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome 124 – Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 125 – Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Edge 124 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Edge 125 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    # Edge 124 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.2478.80",
    # Firefox 123 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    # Firefox 124 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    # Firefox 125 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Firefox 126 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Firefox 124 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:124.0) Gecko/20100101 Firefox/124.0",
    # Firefox 125 – Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Firefox 126 – Linux
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Safari 17 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    # Safari 17 – macOS (minor variant)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15",
]

# ---------------------------------------------------------------------------
# Viewport pool: 15+ real sizes
# ---------------------------------------------------------------------------
_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1920, "height": 1200},
    {"width": 1680, "height": 1050},
    {"width": 1600, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1440, "height": 810},
    {"width": 1366, "height": 768},
    {"width": 1360, "height": 768},
    {"width": 1280, "height": 1024},
    {"width": 1280, "height": 800},
    {"width": 1280, "height": 720},
    {"width": 1024, "height": 768},
    {"width": 2560, "height": 1440},
    {"width": 2560, "height": 1600},
    {"width": 1920, "height": 1080},  # duplicated intentionally for weight
    {"width": 1366, "height": 768},   # duplicated intentionally for weight
]

# ---------------------------------------------------------------------------
# Spanish timezones
# ---------------------------------------------------------------------------
_TIMEZONES = ["Europe/Madrid", "Atlantic/Canary"]

# ---------------------------------------------------------------------------
# Tracking / analytics domains to block
# ---------------------------------------------------------------------------
_BLOCKED_URL_PATTERNS = [
    "*google-analytics.com*",
    "*googletagmanager.com*",
    "*googletagservices.com*",
    "*doubleclick.net*",
    "*googlesyndication.com*",
    "*facebook.com/tr*",
    "*connect.facebook.net*",
    "*hotjar.com*",
    "*clarity.ms*",
    "*segment.com*",
    "*segment.io*",
    "*mixpanel.com*",
    "*amplitude.com*",
    "*intercom.io*",
    "*intercom.com*",
    "*crisp.chat*",
    "*tawk.to*",
    "*livechatinc.com*",
    "*bing.com/bat*",
    "*ads.twitter.com*",
    "*static.ads-twitter.com*",
    "*snap.licdn.com*",
    "*px.ads.linkedin.com*",
    "*cdn.krxd.net*",
    "*scorecard research.com*",
    "*chartbeat.com*",
    "*newrelic.com*",
    "*nr-data.net*",
    "*sentry.io*",
]

# ---------------------------------------------------------------------------
# WebGL realistic vendor/renderer combos
# ---------------------------------------------------------------------------
_WEBGL_CONFIGS = [
    {"vendor": "Intel Inc.", "renderer": "Intel Iris OpenGL Engine"},
    {"vendor": "Intel Inc.", "renderer": "Intel(R) UHD Graphics 620"},
    {"vendor": "Intel Inc.", "renderer": "Intel(R) Iris(R) Xe Graphics"},
    {"vendor": "NVIDIA Corporation", "renderer": "NVIDIA GeForce GTX 1650/PCIe/SSE2"},
    {"vendor": "NVIDIA Corporation", "renderer": "NVIDIA GeForce RTX 3060/PCIe/SSE2"},
    {"vendor": "NVIDIA Corporation", "renderer": "NVIDIA GeForce RTX 2060/PCIe/SSE2"},
    {"vendor": "AMD", "renderer": "AMD Radeon RX 580 Series"},
    {"vendor": "AMD", "renderer": "AMD Radeon Pro 5300M OpenGL Engine"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (AMD)", "renderer": "ANGLE (AMD, Radeon RX Vega 56 Series Direct3D11 vs_5_0 ps_5_0, D3D11)"},
]

# ---------------------------------------------------------------------------
# Stealth init script template (filled at runtime with random values)
# ---------------------------------------------------------------------------
_STEALTH_SCRIPT_TEMPLATE = """
(function() {{
    // ---- 1. Hide webdriver ----
    try {{
        Object.defineProperty(navigator, 'webdriver', {{
            get: () => undefined,
            configurable: true,
        }});
    }} catch(e) {{}}

    // ---- 2. Realistic plugins array ----
    const makePlugin = (name, filename, desc, mimeTypes) => {{
        const plugin = Object.create(Plugin.prototype);
        Object.defineProperty(plugin, 'name',        {{ get: () => name }});
        Object.defineProperty(plugin, 'filename',    {{ get: () => filename }});
        Object.defineProperty(plugin, 'description', {{ get: () => desc }});
        Object.defineProperty(plugin, 'length',      {{ get: () => mimeTypes.length }});
        mimeTypes.forEach((mt, i) => {{ plugin[i] = mt; }});
        return plugin;
    }};
    const makeMime = (type, desc, suffixes) => {{
        const m = Object.create(MimeType.prototype);
        Object.defineProperty(m, 'type',        {{ get: () => type }});
        Object.defineProperty(m, 'description', {{ get: () => desc }});
        Object.defineProperty(m, 'suffixes',    {{ get: () => suffixes }});
        return m;
    }};
    const pdf1 = makeMime('application/x-google-chrome-pdf',    'Portable Document Format', 'pdf');
    const pdf2 = makeMime('application/pdf',                     'Portable Document Format', 'pdf');
    const nacl = makeMime('application/x-nacl',                  'Native Client Executable',  '');
    const pnacl= makeMime('application/x-pnacl',                 'Portable Native Client Executable', '');
    const pluginsArr = [
        makePlugin('Chrome PDF Plugin',  'internal-pdf-viewer',  'Portable Document Format', [pdf1]),
        makePlugin('Chrome PDF Viewer',  'mhjfbmdgcfjbbpaeojofohoefgiehjai', '', [pdf2]),
        makePlugin('Native Client',      'internal-nacl-plugin',  '', [nacl, pnacl]),
    ];
    const pluginList = Object.create(PluginArray.prototype);
    Object.defineProperty(pluginList, 'length', {{ get: () => pluginsArr.length }});
    pluginsArr.forEach((p, i) => {{
        pluginList[i] = p;
        Object.defineProperty(pluginList, p.name, {{ get: () => p }});
    }});
    pluginList[Symbol.iterator] = function*() {{ for (let i=0;i<this.length;i++) yield this[i]; }};
    try {{
        Object.defineProperty(navigator, 'plugins', {{ get: () => pluginList, configurable: true }});
    }} catch(e) {{}}

    // ---- 3. Languages ----
    try {{
        Object.defineProperty(navigator, 'languages', {{
            get: () => ['es-ES', 'es', 'en-US', 'en'],
            configurable: true,
        }});
        Object.defineProperty(navigator, 'language', {{
            get: () => 'es-ES',
            configurable: true,
        }});
    }} catch(e) {{}}

    // ---- 4. hardwareConcurrency ----
    try {{
        Object.defineProperty(navigator, 'hardwareConcurrency', {{
            get: () => {hw_concurrency},
            configurable: true,
        }});
    }} catch(e) {{}}

    // ---- 5. deviceMemory ----
    try {{
        Object.defineProperty(navigator, 'deviceMemory', {{
            get: () => {device_memory},
            configurable: true,
        }});
    }} catch(e) {{}}

    // ---- 6. platform ----
    try {{
        Object.defineProperty(navigator, 'platform', {{
            get: () => '{platform}',
            configurable: true,
        }});
    }} catch(e) {{}}

    // ---- 7. Permissions API override ----
    try {{
        const origQuery = window.Permissions && window.Permissions.prototype.query;
        if (navigator.permissions && navigator.permissions.query) {{
            const origPermQuery = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.__proto__.query = function(params) {{
                if (params && params.name === 'notifications') {{
                    return Promise.resolve({{ state: 'prompt', onchange: null }});
                }}
                return origPermQuery(params);
            }};
        }}
    }} catch(e) {{}}

    // ---- 8. WebGL vendor/renderer spoof ----
    try {{
        const getParam = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(param) {{
            if (param === 37445) return '{webgl_vendor}';   // UNMASKED_VENDOR_WEBGL
            if (param === 37446) return '{webgl_renderer}'; // UNMASKED_RENDERER_WEBGL
            return getParam.call(this, param);
        }};
        const getParam2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(param) {{
            if (param === 37445) return '{webgl_vendor}';
            if (param === 37446) return '{webgl_renderer}';
            return getParam2.call(this, param);
        }};
    }} catch(e) {{}}

    // ---- 9. Canvas fingerprint noise ----
    try {{
        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type) {{
            const ctx = this.getContext('2d');
            if (ctx) {{
                const imgData = ctx.getImageData(0, 0, this.width || 1, this.height || 1);
                // Flip exactly 3 random low-order bits in the alpha channel
                for (let k = 0; k < 3; k++) {{
                    const idx = Math.floor(Math.random() * imgData.data.length / 4) * 4 + 3;
                    imgData.data[idx] ^= 1;
                }}
                ctx.putImageData(imgData, 0, 0);
            }}
            return origToDataURL.apply(this, arguments);
        }};
        const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {{
            const data = origGetImageData.call(this, x, y, w, h);
            for (let k = 0; k < 3; k++) {{
                const idx = Math.floor(Math.random() * data.data.length / 4) * 4;
                data.data[idx] ^= 1;
            }}
            return data;
        }};
    }} catch(e) {{}}

    // ---- 10. navigator.connection ----
    try {{
        Object.defineProperty(navigator, 'connection', {{
            get: () => ({{
                rtt: {rtt},
                downlink: {downlink},
                effectiveType: '4g',
                saveData: false,
            }}),
            configurable: true,
        }});
    }} catch(e) {{}}

    // ---- 11. window.chrome (for Chrome UAs) ----
    try {{
        if ({is_chrome}) {{
            if (!window.chrome) {{
                window.chrome = {{}};
            }}
            if (!window.chrome.runtime) {{
                window.chrome.runtime = {{
                    PlatformOs: {{ MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' }},
                    PlatformArch: {{ ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64', MIPS: 'mips', MIPS64: 'mips64' }},
                    RequestUpdateCheckStatus: {{ THROTTLED: 'throttled', NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available' }},
                    OnInstalledReason: {{ INSTALL: 'install', UPDATE: 'update', CHROME_UPDATE: 'chrome_update', SHARED_MODULE_UPDATE: 'shared_module_update' }},
                    OnRestartRequiredReason: {{ APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' }},
                    connect: function() {{}},
                    sendMessage: function() {{}},
                }};
            }}
            if (!window.chrome.loadTimes) {{
                window.chrome.loadTimes = function() {{
                    return {{
                        requestTime: Date.now() / 1000 - Math.random() * 2,
                        startLoadTime: Date.now() / 1000 - Math.random() * 2,
                        commitLoadTime: Date.now() / 1000 - Math.random(),
                        finishDocumentLoadTime: Date.now() / 1000 - Math.random() * 0.5,
                        finishLoadTime: Date.now() / 1000 - Math.random() * 0.2,
                        firstPaintTime: Date.now() / 1000 - Math.random() * 0.3,
                        firstPaintAfterLoadTime: 0,
                        navigationType: 'Other',
                        wasFetchedViaSpdy: true,
                        wasNpnNegotiated: true,
                        npnNegotiatedProtocol: 'h2',
                        wasAlternateProtocolAvailable: false,
                        connectionInfo: 'h2',
                    }};
                }};
            }}
            if (!window.chrome.csi) {{
                window.chrome.csi = function() {{
                    return {{
                        startE: Date.now(),
                        onloadT: Date.now() + Math.floor(Math.random() * 300),
                        pageT: Math.random() * 3000,
                        tran: 15,
                    }};
                }};
            }}
            if (!window.chrome.app) {{
                window.chrome.app = {{
                    isInstalled: false,
                    InstallState: {{ DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }},
                    RunningState: {{ CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }},
                    getDetails: function() {{ return null; }},
                    getIsInstalled: function() {{ return false; }},
                    runningState: function() {{ return 'cannot_run'; }},
                }};
            }}
        }}
    }} catch(e) {{}}

    // ---- 12. Remove automation-related properties ----
    try {{
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
        delete window.__webdriver_evaluate;
        delete window.__selenium_evaluate;
        delete window.__webdriver_script_function;
        delete window.__webdriver_script_func;
        delete window.__webdriver_script_fn;
        delete window.__fxdriver_evaluate;
        delete window.__driver_unwrapped;
        delete window.__webdriver_unwrapped;
        delete window.__driver_evaluate;
        delete window.__selenium_unwrapped;
        delete window.__fxdriver_unwrapped;
    }} catch(e) {{}}

    // ---- 13. Realistic timing noise ----
    try {{
        const origNow = Date.now;
        let _offset = Math.floor(Math.random() * 100);
        Date.now = function() {{ return origNow() + _offset; }};
        const origPerfNow = performance.now.bind(performance);
        performance.now = function() {{ return origPerfNow() + (Math.random() - 0.5) * 0.1; }};
    }} catch(e) {{}}
}})();
"""


def _js_string(s: str) -> str:
    """Escape a Python string for safe embedding in JS source code."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def _gaussian_delay(center: float = 3.0, stddev: float = 1.0,
                    lo: float = 1.0, hi: float = 8.0) -> float:
    """Return a gaussian-distributed delay clamped to [lo, hi]."""
    raw = random.gauss(center, stddev)
    return max(lo, min(hi, raw))


def _platform_from_ua(ua: str) -> str:
    """Derive a plausible navigator.platform string from a user agent."""
    ua_lower = ua.lower()
    if "windows" in ua_lower:
        return "Win32"
    if "macintosh" in ua_lower or "mac os x" in ua_lower:
        return "MacIntel"
    if "linux" in ua_lower or "x11" in ua_lower:
        return "Linux x86_64"
    return "Win32"


def _is_chrome_ua(ua: str) -> bool:
    """Return True if the UA string is Chrome or Edge (not Firefox/Safari)."""
    ua_lower = ua.lower()
    return "chrome" in ua_lower or "edg" in ua_lower


def _build_stealth_script(ua: str) -> str:
    """Fill the stealth script template with per-session random values."""
    webgl = random.choice(_WEBGL_CONFIGS)
    return _STEALTH_SCRIPT_TEMPLATE.format(
        hw_concurrency=random.choice([4, 6, 8, 12, 16]),
        device_memory=random.choice([4, 8, 16]),
        platform=_platform_from_ua(ua),
        webgl_vendor=webgl["vendor"],
        webgl_renderer=webgl["renderer"],
        rtt=random.choice([50, 75, 100, 150]),
        downlink=round(random.uniform(5.0, 50.0), 1),
        is_chrome="true" if _is_chrome_ua(ua) else "false",
    )


class BrowserClient:
    """Cliente Playwright async con medidas anti-deteccion avanzadas."""

    def __init__(
        self,
        delay_min: float = 2.0,
        delay_max: float = 5.0,
        max_requests_per_minute: int = 10,
        proxy_url: str | None = None,
        headless: bool = True,
        speed_mode: bool = False,
    ) -> None:
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.max_rpm = max_requests_per_minute
        self.proxy_url = proxy_url
        self.headless = headless
        self.speed_mode = speed_mode

        self._playwright = None
        self._browser = None
        self._request_log: dict[str, list[float]] = defaultdict(list)
        # Cookie persistence: domain -> list[cookie dicts]
        self._cookie_store: dict[str, list[dict]] = {}
        # Persistent contexts per domain (speed mode)
        self._contexts: dict[str, Any] = {}  # domain -> BrowserContext

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Inicia Playwright y lanza el navegador Chromium."""
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()

        launch_opts: dict = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--no-first-run",
                "--no-zygote",
                "--disable-gpu",
                "--lang=es-ES",
                "--disable-features=IsolateOrigins,site-per-process",
                # Memory optimization
                "--js-flags=--max-old-space-size=512",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--single-process",
            ],
        }
        if self.proxy_url:
            launch_opts["proxy"] = {"server": self.proxy_url}

        self._browser = await self._playwright.chromium.launch(**launch_opts)
        logger.info("Navegador Chromium iniciado (headless=%s)", self.headless)

    async def cleanup_contexts(self) -> None:
        """Cierra todos los contextos persistentes para liberar memoria.

        Llamar después de cada ciclo de scraping de una tienda.
        """
        closed = 0
        for domain, ctx in list(self._contexts.items()):
            try:
                await ctx.close()
                closed += 1
            except Exception:
                pass
        self._contexts.clear()
        if closed:
            logger.debug("Contextos cerrados: %d (memoria liberada)", closed)

    async def close(self) -> None:
        """Cierra contextos persistentes, el navegador y Playwright."""
        await self.cleanup_contexts()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ------------------------------------------------------------------
    # Rate limiting y delays gaussianos
    # ------------------------------------------------------------------
    async def _wait_for_rate_limit(self, domain: str) -> None:
        now = time.monotonic()
        window = 60.0
        self._request_log[domain] = [
            t for t in self._request_log[domain] if now - t < window
        ]
        if len(self._request_log[domain]) >= self.max_rpm:
            oldest = self._request_log[domain][0]
            wait = window - (now - oldest) + 0.5
            logger.info(
                "Rate limit alcanzado para %s, esperando %.1fs", domain, wait
            )
            await asyncio.sleep(wait)
        self._request_log[domain].append(time.monotonic())

    async def _random_delay(self) -> None:
        """Gaussian-distributed delay clamped to [delay_min, delay_max]."""
        center = (self.delay_min + self.delay_max) / 2.0
        stddev = (self.delay_max - self.delay_min) / 4.0
        delay = _gaussian_delay(
            center=center,
            stddev=max(stddev, 0.5),
            lo=self.delay_min,
            hi=self.delay_max,
        )
        logger.debug("Delay gaussiano: %.2fs", delay)
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Resource blocking
    # ------------------------------------------------------------------
    async def _setup_route_blocking(self, page) -> None:
        """Block tracking resources, fonts, media and stylesheets."""
        blocked_types = {"font", "media"}

        async def _handle_route(route):
            req = route.request
            url = req.url
            # Check resource type
            if req.resource_type in blocked_types:
                await route.abort()
                return
            # Check against known tracking patterns (simple substring check)
            url_lower = url.lower()
            for pattern in (
                "google-analytics.com",
                "googletagmanager.com",
                "googletagservices.com",
                "doubleclick.net",
                "googlesyndication.com",
                "facebook.com/tr",
                "connect.facebook.net",
                "hotjar.com",
                "clarity.ms",
                "segment.com",
                "segment.io",
                "mixpanel.com",
                "amplitude.com",
                "intercom.io",
                "intercom.com",
                "crisp.chat",
                "tawk.to",
                "livechatinc.com",
                "bing.com/bat",
                "ads.twitter.com",
                "static.ads-twitter.com",
                "snap.licdn.com",
                "px.ads.linkedin.com",
                "chartbeat.com",
                "newrelic.com",
                "nr-data.net",
            ):
                if pattern in url_lower:
                    await route.abort()
                    return
            await route.continue_()

        await page.route("**/*", _handle_route)

    # ------------------------------------------------------------------
    # Cookie persistence
    # ------------------------------------------------------------------
    async def _restore_cookies(self, context, domain: str) -> None:
        cookies = self._cookie_store.get(domain)
        if cookies:
            await context.add_cookies(cookies)
            logger.debug("Cookies restauradas para %s (%d)", domain, len(cookies))

    async def _save_cookies(self, context, domain: str) -> None:
        cookies = await context.cookies()
        if cookies:
            self._cookie_store[domain] = cookies
            logger.debug("Cookies guardadas para %s (%d)", domain, len(cookies))

    # ------------------------------------------------------------------
    # Realistic mouse movements
    # ------------------------------------------------------------------
    async def _random_mouse_movements(self, page, viewport: dict) -> None:
        """Perform a few random bezier-ish mouse moves."""
        w = viewport["width"]
        h = viewport["height"]
        steps = random.randint(3, 7)
        for _ in range(steps):
            x = random.randint(100, w - 100)
            y = random.randint(100, h - 100)
            await page.mouse.move(x, y, steps=random.randint(5, 20))
            await asyncio.sleep(random.uniform(0.05, 0.25))

    # ------------------------------------------------------------------
    # Realistic multi-step scrolling
    # ------------------------------------------------------------------
    async def _realistic_scroll(self, page, viewport: dict) -> None:
        """Scroll down in multiple steps, simulating reading behaviour."""
        try:
            scroll_height: int = await page.evaluate("document.body.scrollHeight")
        except Exception:
            return
        visible_height = viewport["height"]
        current_pos = 0
        max_scrolls = random.randint(4, 9)

        for i in range(max_scrolls):
            # Scroll amount: roughly one viewport height with some variance
            scroll_amount = int(visible_height * random.uniform(0.6, 1.1))
            current_pos = min(current_pos + scroll_amount, scroll_height)
            try:
                # Use document.documentElement.scrollTop to avoid conflicts
                # with jQuery scrollTo plugins on some sites
                await page.evaluate(f"document.documentElement.scrollTop = {current_pos}")
            except Exception:
                break
            # Pause that mimics reading: gaussian around 1.2s
            pause = _gaussian_delay(center=1.2, stddev=0.5, lo=0.4, hi=3.5)
            await asyncio.sleep(pause)
            if current_pos >= scroll_height:
                break

        # Small chance to scroll back up a bit (reader changed mind)
        if random.random() < 0.25:
            back = random.randint(200, 600)
            try:
                await page.evaluate(f"document.documentElement.scrollTop -= {back}")
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.3, 0.9))

    # ------------------------------------------------------------------
    # Fetch dispatcher
    # ------------------------------------------------------------------
    async def fetch(self, url: str, force_stealth: bool = False) -> str:
        """Navega a la URL y devuelve HTML. Despacha según speed_mode."""
        if self.speed_mode and not force_stealth:
            return await self._fetch_fast(url)
        return await self._fetch_stealth(url)

    # ------------------------------------------------------------------
    # Fast fetch: persistent context, minimal delays, no scroll/mouse
    # ------------------------------------------------------------------
    async def _fetch_fast(self, url: str) -> str:
        """Fetch rápido: contexto persistente, sin scroll/mouse/cookies consent."""
        if not self._browser:
            await self.start()

        domain = urlparse(url).netloc

        # Minimal delay: 0.3-1.0s
        delay = random.uniform(0.3, 1.0)
        logger.debug("Fast delay: %.2fs", delay)
        await asyncio.sleep(delay)

        await self._wait_for_rate_limit(domain)

        # Get or create persistent context for this domain
        context = await self._get_or_create_context(domain)
        page = await context.new_page()

        # Block images + fonts + media + stylesheets + trackers
        await self._setup_route_blocking_fast(page)

        try:
            logger.info("FAST navegando a %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Minimal wait: 0.5-1.0s
            wait_ms = int(random.uniform(0.5, 1.0) * 1000)
            await page.wait_for_timeout(wait_ms)

            # Wait for Cloudflare Turnstile challenge to resolve
            await self._wait_for_cloudflare(page)

            # Single fast scroll to trigger lazy-loaded content
            try:
                await page.evaluate(
                    "document.documentElement.scrollTop = "
                    "document.body.scrollHeight * 0.7"
                )
            except Exception:
                pass
            await page.wait_for_timeout(300)

            html = await page.content()

            # Persistir cookies para futuras peticiones en fast mode
            await self._save_cookies(context, domain)

            return html
        finally:
            await page.close()

    _MAX_CONTEXTS = 6  # Máximo de contextos simultáneos para limitar RAM

    async def _get_or_create_context(self, domain: str):
        """Return a persistent BrowserContext for the domain, creating if needed."""
        if domain in self._contexts:
            return self._contexts[domain]

        # Evitar acumulación de contextos: cerrar los más antiguos
        while len(self._contexts) >= self._MAX_CONTEXTS:
            oldest_domain = next(iter(self._contexts))
            old_ctx = self._contexts.pop(oldest_domain)
            try:
                await old_ctx.close()
                logger.debug("Contexto cerrado (límite %d): %s",
                             self._MAX_CONTEXTS, oldest_domain)
            except Exception:
                pass

        ua = random.choice(_USER_AGENTS)
        viewport = random.choice(_VIEWPORTS)
        timezone = random.choice(_TIMEZONES)
        stealth_script = _build_stealth_script(ua)

        context = await self._browser.new_context(
            viewport=viewport,
            user_agent=ua,
            locale="es-ES",
            timezone_id=timezone,
            extra_http_headers={
                "Accept-Language": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Accept-Encoding": "gzip, deflate, br",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            },
            java_script_enabled=True,
            bypass_csp=False,
        )

        # Inject stealth scripts into every new page in this context
        await context.add_init_script(stealth_script)

        # Restore cookies if we have them
        await self._restore_cookies(context, domain)

        self._contexts[domain] = context
        logger.debug("Contexto persistente creado para %s", domain)
        return context

    async def _setup_route_blocking_fast(self, page) -> None:
        """Block images + fonts + media + stylesheets + trackers (fast mode)."""
        blocked_types = {"font", "media", "image"}

        async def _handle_route(route):
            req = route.request
            if req.resource_type in blocked_types:
                await route.abort()
                return
            url_lower = req.url.lower()
            for pattern in (
                "google-analytics.com", "googletagmanager.com",
                "doubleclick.net", "googlesyndication.com",
                "facebook.com/tr", "connect.facebook.net",
                "hotjar.com", "clarity.ms",
            ):
                if pattern in url_lower:
                    await route.abort()
                    return
            await route.continue_()

        await page.route("**/*", _handle_route)

    # ------------------------------------------------------------------
    # Stealth fetch: full anti-detection (original behaviour)
    # ------------------------------------------------------------------
    async def _fetch_stealth(self, url: str) -> str:
        """Navega a la URL con Playwright — modo stealth completo."""
        if not self._browser:
            await self.start()

        domain = urlparse(url).netloc
        await self._random_delay()
        await self._wait_for_rate_limit(domain)

        ua = random.choice(_USER_AGENTS)
        viewport = random.choice(_VIEWPORTS)
        timezone = random.choice(_TIMEZONES)
        stealth_script = _build_stealth_script(ua)

        # Build accept-language header consistent with UA
        accept_lang = "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7"

        context_opts: dict = {
            "viewport": viewport,
            "user_agent": ua,
            "locale": "es-ES",
            "timezone_id": timezone,
            "extra_http_headers": {
                "Accept-Language": accept_lang,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Accept-Encoding": "gzip, deflate, br",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            },
            "java_script_enabled": True,
            "bypass_csp": False,
        }

        context = await self._browser.new_context(**context_opts)

        # Restore persisted cookies for this domain
        await self._restore_cookies(context, domain)

        page = await context.new_page()

        # Inject stealth scripts BEFORE any navigation
        await page.add_init_script(stealth_script)

        # Block unwanted resources
        await self._setup_route_blocking(page)

        try:
            logger.info(
                "Navegando a %s | viewport %dx%d | UA: %.60s...",
                url,
                viewport["width"],
                viewport["height"],
                ua,
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Wait for dynamic content with gaussian timing
            initial_wait = int(_gaussian_delay(center=2.5, stddev=0.8, lo=1.5, hi=5.0) * 1000)
            await page.wait_for_timeout(initial_wait)

            # Wait for Cloudflare Turnstile challenge to resolve
            await self._wait_for_cloudflare(page)

            # Dismiss cookie consent popups (blocks content on many EU sites)
            await self._dismiss_cookie_consent(page)

            # Mouse movements before scrolling
            await self._random_mouse_movements(page, viewport)

            # Multi-step realistic scroll
            await self._realistic_scroll(page, viewport)

            # Final short wait after scrolling
            final_wait = int(_gaussian_delay(center=1.0, stddev=0.4, lo=0.4, hi=2.5) * 1000)
            await page.wait_for_timeout(final_wait)

            html = await page.content()

            # Persist cookies for future requests to this domain
            await self._save_cookies(context, domain)

            return html
        finally:
            await context.close()

    # ------------------------------------------------------------------
    # Fetch with network interception (for SPA stores like Decathlon)
    # ------------------------------------------------------------------
    async def fetch_with_intercept(
        self,
        url: str,
        url_pattern: str,
        *,
        force_stealth: bool = False,
        timeout: int = 10000,
    ) -> tuple[str, list[dict]]:
        """Navigate to *url* and capture JSON responses matching *url_pattern*.

        Returns ``(html, [response_json, ...])``.  Useful for SPA sites that
        load product data via XHR (e.g. Algolia).
        """
        if not self._browser:
            await self.start()

        domain = urlparse(url).netloc
        await self._random_delay()
        await self._wait_for_rate_limit(domain)

        captured: list[dict] = []

        async def _on_response(response):
            try:
                if url_pattern in response.url and "json" in (
                    response.headers.get("content-type", "")
                ):
                    body = await response.json()
                    captured.append(body)
            except Exception:
                pass

        ua = random.choice(_USER_AGENTS)
        viewport = random.choice(_VIEWPORTS)
        timezone = random.choice(_TIMEZONES)
        stealth_script = _build_stealth_script(ua)

        context = await self._browser.new_context(
            viewport=viewport,
            user_agent=ua,
            locale="es-ES",
            timezone_id=timezone,
            java_script_enabled=True,
        )
        await self._restore_cookies(context, domain)
        page = await context.new_page()
        await page.add_init_script(stealth_script)

        page.on("response", _on_response)

        try:
            logger.info("INTERCEPT navegando a %s (pattern=%s)", url, url_pattern)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Wait for XHR responses to arrive
            wait_ms = timeout
            await page.wait_for_timeout(wait_ms)

            html = await page.content()
            await self._save_cookies(context, domain)
            logger.info("INTERCEPT capturó %d respuestas JSON", len(captured))
            return html, captured
        finally:
            await context.close()

    # ------------------------------------------------------------------
    # Cloudflare Turnstile challenge wait
    # ------------------------------------------------------------------
    async def _wait_for_cloudflare(self, page, max_wait: float = 8.0) -> None:
        """Detect and wait for Cloudflare Turnstile challenge to resolve.

        Checks if the page title indicates a Cloudflare challenge page
        ("Un momento", "Just a moment", etc.), tries to click the Turnstile
        checkbox, and polls until the challenge resolves or max_wait elapses.
        """
        try:
            title = await page.title()
        except Exception:
            return

        cf_titles = ("un momento", "just a moment", "checking your browser")
        if not any(t in title.lower() for t in cf_titles):
            return

        logger.info("Cloudflare challenge detectado, intentando resolver...")
        start = time.monotonic()

        # Try to click the Turnstile checkbox inside its iframe
        clicked = False
        for attempt in range(3):
            try:
                # Turnstile renders inside an iframe from challenges.cloudflare.com
                cf_frame = None
                for frame in page.frames:
                    if "challenges.cloudflare.com" in frame.url:
                        cf_frame = frame
                        break

                if cf_frame:
                    # The checkbox is typically an input or div inside the iframe
                    checkbox = cf_frame.locator("input[type='checkbox']").first
                    if await checkbox.is_visible(timeout=2000):
                        await checkbox.click(timeout=3000)
                        clicked = True
                        logger.info("Turnstile checkbox clicked (attempt %d)", attempt + 1)
                        break
                    # Alternative: click the body/label of the challenge
                    label = cf_frame.locator("label").first
                    if await label.is_visible(timeout=1000):
                        await label.click(timeout=2000)
                        clicked = True
                        logger.info("Turnstile label clicked (attempt %d)", attempt + 1)
                        break
            except Exception:
                pass
            await page.wait_for_timeout(2000)

        if not clicked:
            # Try clicking at the approximate location of the Turnstile widget
            try:
                widget = await page.query_selector("iframe[src*='challenges.cloudflare']")
                if widget:
                    box = await widget.bounding_box()
                    if box:
                        # Click center of the iframe (where checkbox typically is)
                        await page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        logger.info("Turnstile iframe clicked at center")
            except Exception:
                pass

        # Poll until the title changes (challenge resolved)
        while time.monotonic() - start < max_wait:
            await page.wait_for_timeout(1000)
            try:
                title = await page.title()
            except Exception:
                return
            if not any(t in title.lower() for t in cf_titles):
                elapsed = time.monotonic() - start
                logger.info("Cloudflare challenge resuelto en %.1fs", elapsed)
                await page.wait_for_timeout(2000)
                return

        logger.warning("Cloudflare challenge NO resuelto tras %.0fs", max_wait)

    # ------------------------------------------------------------------
    # Cookie consent auto-dismiss
    # ------------------------------------------------------------------

    _COOKIE_CONSENT_SELECTORS = [
        # Specific consent management platforms
        "#didomi-notice-agree-button",                              # Didomi (BackMarket)
        "#onetrust-accept-btn-handler",                             # OneTrust
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",   # CookieBot
        "[data-testid='uc-accept-all-button']",                     # Usercentrics
        "#accept-cookies",                                          # Generic
        ".accept-cookies",                                          # Generic
        "#cookies-accept-all",                                      # Various
        "button.js-accept-cookies",                                 # Various
        "[data-action='accept']",                                   # Various
        "#cookie-accept",                                           # Various
        ".cookie-accept",                                           # Various
        "#consent-accept",                                          # Various
        ".consent-accept",                                          # Various
        "#gdpr-accept",                                             # Various
        ".gdpr-accept",                                             # Various
        "#cookie-banner-accept",                                    # Various
        ".cookie-banner-accept",                                    # Various
    ]

    _COOKIE_ACCEPT_TEXTS = [
        "Aceptar todo",
        "Aceptar todas",
        "Aceptar y continuar",
        "Aceptar cookies",
        "Aceptar",
        "Accept all",
        "Accept cookies",
        "Accept",
        "Accepter tout",
        "Tout accepter",
        "Akzeptieren",
        "Alle akzeptieren",
    ]

    async def _dismiss_cookie_consent(self, page) -> None:
        """Try to find and click a cookie consent 'accept' button.

        Uses a single JS evaluation to test all selectors in parallel (~50ms)
        instead of sequential Playwright locator checks (300ms * N selectors).
        Falls back to text-based matching if no selector matches.
        """
        try:
            # Fase 1: JS paralelo — probar todos los selectores de golpe
            all_selectors = ",".join(self._COOKIE_CONSENT_SELECTORS)
            clicked = await page.evaluate(f"""() => {{
                const btn = document.querySelector({_js_string(all_selectors)});
                if (btn && btn.offsetParent !== null) {{
                    btn.click();
                    return true;
                }}
                return false;
            }}""")
            if clicked:
                logger.debug("Cookie consent dismissed via JS selector")
                await page.wait_for_timeout(500)
                return

            # Fase 2: text-based — solo 3 intentos (los más comunes)
            for text in self._COOKIE_ACCEPT_TEXTS[:3]:
                try:
                    btn = page.get_by_role("button", name=text, exact=False).first
                    if await btn.is_visible(timeout=200):
                        await btn.click(timeout=1500)
                        logger.debug("Cookie consent dismissed via text: '%s'", text)
                        await page.wait_for_timeout(500)
                        return
                except Exception:
                    continue

        except Exception:
            pass  # Non-critical — page may not have a consent dialog
