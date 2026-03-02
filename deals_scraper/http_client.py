"""Cliente HTTP anti-ban: UA rotation, browser-consistent headers, cookie jars,
referrer chain, per-domain rate limiting, gaussian delay, exponential backoff, proxy."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections import defaultdict
from typing import NamedTuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# User-Agent pool — 25+ real browser strings (Chrome 120-125, Firefox 123-126,
# Safari 17, Edge 124-125) across Windows, macOS and Linux.
# ---------------------------------------------------------------------------

class _UAMeta(NamedTuple):
    ua: str
    browser: str          # "chrome" | "firefox" | "safari" | "edge"
    platform: str         # "Windows" | "macOS" | "Linux"
    # For Chrome/Edge: the brand token used in Sec-CH-UA
    ch_brand: str         # e.g. '"Chromium";v="124"'
    ch_brand_full: str    # e.g. '"Google Chrome";v="124"'
    ch_version: str       # e.g. "124"


# Each entry is (_UAMeta) — adding ch_brand/ch_brand_full only makes sense for
# Chrome/Edge; for Firefox/Safari those fields are empty strings.
USER_AGENTS: tuple[_UAMeta, ...] = (
    # ── Chrome 120 ──────────────────────────────────────────────────────────
    _UAMeta(
        ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        browser="chrome", platform="Windows",
        ch_brand='"Chromium";v="120", "Not_A Brand";v="24"',
        ch_brand_full='"Google Chrome";v="120", "Not_A Brand";v="24", "Chromium";v="120"',
        ch_version="120",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        browser="chrome", platform="macOS",
        ch_brand='"Chromium";v="120", "Not_A Brand";v="24"',
        ch_brand_full='"Google Chrome";v="120", "Not_A Brand";v="24", "Chromium";v="120"',
        ch_version="120",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        browser="chrome", platform="Linux",
        ch_brand='"Chromium";v="120", "Not_A Brand";v="24"',
        ch_brand_full='"Google Chrome";v="120", "Not_A Brand";v="24", "Chromium";v="120"',
        ch_version="120",
    ),
    # ── Chrome 122 ──────────────────────────────────────────────────────────
    _UAMeta(
        ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        browser="chrome", platform="Windows",
        ch_brand='"Chromium";v="122", "Not_A Brand";v="24"',
        ch_brand_full='"Google Chrome";v="122", "Not_A Brand";v="24", "Chromium";v="122"',
        ch_version="122",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        browser="chrome", platform="macOS",
        ch_brand='"Chromium";v="122", "Not_A Brand";v="24"',
        ch_brand_full='"Google Chrome";v="122", "Not_A Brand";v="24", "Chromium";v="122"',
        ch_version="122",
    ),
    # ── Chrome 124 ──────────────────────────────────────────────────────────
    _UAMeta(
        ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        browser="chrome", platform="Windows",
        ch_brand='"Chromium";v="124", "Google Chrome";v="124", "Not_A Brand";v="99"',
        ch_brand_full='"Google Chrome";v="124", "Not_A Brand";v="99", "Chromium";v="124"',
        ch_version="124",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        browser="chrome", platform="macOS",
        ch_brand='"Chromium";v="124", "Google Chrome";v="124", "Not_A Brand";v="99"',
        ch_brand_full='"Google Chrome";v="124", "Not_A Brand";v="99", "Chromium";v="124"',
        ch_version="124",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        browser="chrome", platform="Linux",
        ch_brand='"Chromium";v="124", "Google Chrome";v="124", "Not_A Brand";v="99"',
        ch_brand_full='"Google Chrome";v="124", "Not_A Brand";v="99", "Chromium";v="124"',
        ch_version="124",
    ),
    # ── Chrome 125 ──────────────────────────────────────────────────────────
    _UAMeta(
        ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        browser="chrome", platform="Windows",
        ch_brand='"Chromium";v="125", "Google Chrome";v="125", "Not_A Brand";v="99"',
        ch_brand_full='"Google Chrome";v="125", "Not_A Brand";v="99", "Chromium";v="125"',
        ch_version="125",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        browser="chrome", platform="macOS",
        ch_brand='"Chromium";v="125", "Google Chrome";v="125", "Not_A Brand";v="99"',
        ch_brand_full='"Google Chrome";v="125", "Not_A Brand";v="99", "Chromium";v="125"',
        ch_version="125",
    ),
    # ── Firefox 123 ─────────────────────────────────────────────────────────
    _UAMeta(
        ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0',
        browser="firefox", platform="Windows",
        ch_brand="", ch_brand_full="", ch_version="",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:123.0) Gecko/20100101 Firefox/123.0',
        browser="firefox", platform="macOS",
        ch_brand="", ch_brand_full="", ch_version="",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (X11; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0',
        browser="firefox", platform="Linux",
        ch_brand="", ch_brand_full="", ch_version="",
    ),
    # ── Firefox 124 ─────────────────────────────────────────────────────────
    _UAMeta(
        ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0',
        browser="firefox", platform="Windows",
        ch_brand="", ch_brand_full="", ch_version="",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:124.0) Gecko/20100101 Firefox/124.0',
        browser="firefox", platform="macOS",
        ch_brand="", ch_brand_full="", ch_version="",
    ),
    # ── Firefox 125 ─────────────────────────────────────────────────────────
    _UAMeta(
        ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
        browser="firefox", platform="Windows",
        ch_brand="", ch_brand_full="", ch_version="",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0',
        browser="firefox", platform="Linux",
        ch_brand="", ch_brand_full="", ch_version="",
    ),
    # ── Firefox 126 ─────────────────────────────────────────────────────────
    _UAMeta(
        ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0',
        browser="firefox", platform="Windows",
        ch_brand="", ch_brand_full="", ch_version="",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:126.0) Gecko/20100101 Firefox/126.0',
        browser="firefox", platform="macOS",
        ch_brand="", ch_brand_full="", ch_version="",
    ),
    # ── Safari 17 ───────────────────────────────────────────────────────────
    _UAMeta(
        ua='Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15',
        browser="safari", platform="macOS",
        ch_brand="", ch_brand_full="", ch_version="",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15',
        browser="safari", platform="macOS",
        ch_brand="", ch_brand_full="", ch_version="",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
        browser="safari", platform="macOS",
        ch_brand="", ch_brand_full="", ch_version="",
    ),
    # ── Edge 124 ────────────────────────────────────────────────────────────
    _UAMeta(
        ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0',
        browser="edge", platform="Windows",
        ch_brand='"Chromium";v="124", "Microsoft Edge";v="124", "Not_A Brand";v="99"',
        ch_brand_full='"Microsoft Edge";v="124", "Not_A Brand";v="99", "Chromium";v="124"',
        ch_version="124",
    ),
    _UAMeta(
        ua='Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.2478.67',
        browser="edge", platform="macOS",
        ch_brand='"Chromium";v="124", "Microsoft Edge";v="124", "Not_A Brand";v="99"',
        ch_brand_full='"Microsoft Edge";v="124", "Not_A Brand";v="99", "Chromium";v="124"',
        ch_version="124",
    ),
    # ── Edge 125 ────────────────────────────────────────────────────────────
    _UAMeta(
        ua='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0',
        browser="edge", platform="Windows",
        ch_brand='"Chromium";v="125", "Microsoft Edge";v="125", "Not_A Brand";v="99"',
        ch_brand_full='"Microsoft Edge";v="125", "Not_A Brand";v="99", "Chromium";v="125"',
        ch_version="125",
    ),
)


# ---------------------------------------------------------------------------
# Header builder — returns a header dict consistent with the chosen UA
# ---------------------------------------------------------------------------

def _platform_ch_value(platform: str) -> str:
    """Map platform name to the Sec-CH-UA-Platform token."""
    mapping = {"Windows": '"Windows"', "macOS": '"macOS"', "Linux": '"Linux"'}
    return mapping.get(platform, '"Unknown"')


def build_headers(url: str, ua_meta: _UAMeta | None = None) -> dict[str, str]:
    """Return a browser-consistent header set for *url*.

    If *ua_meta* is None a random entry from USER_AGENTS is chosen.
    """
    if ua_meta is None:
        ua_meta = random.choice(USER_AGENTS)

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # Base headers present in every browser
    headers: dict[str, str] = {
        "User-Agent": ua_meta.ua,
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": origin + "/",
    }

    browser = ua_meta.browser

    # Accept — Safari uses slightly different Accept ordering
    if browser == "safari":
        headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        )
    elif browser == "firefox":
        headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        )
    else:  # chrome / edge
        headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        )

    # Sec-Fetch-* — present in Chrome, Edge, Firefox (>= 90), NOT in old Safari
    if browser in ("chrome", "edge", "firefox"):
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = "same-origin"
        headers["Sec-Fetch-User"] = "?1"

    # Sec-CH-* — Chrome and Edge only
    if browser in ("chrome", "edge"):
        headers["Sec-CH-UA"] = ua_meta.ch_brand
        headers["Sec-CH-UA-Mobile"] = "?0"
        headers["Sec-CH-UA-Platform"] = _platform_ch_value(ua_meta.platform)

    return headers


# ---------------------------------------------------------------------------
# Gaussian delay helper
# ---------------------------------------------------------------------------

def _gaussian_delay(center: float, stddev: float, min_val: float, max_val: float) -> float:
    """Return a delay sampled from a gaussian distribution, clamped to [min_val, max_val]."""
    raw = random.gauss(center, stddev)
    return max(min_val, min(max_val, raw))


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class HttpClient:
    """Async HTTP client with comprehensive anti-ban capabilities.

    Features
    --------
    - 25+ real user-agent strings covering Chrome 120-125, Firefox 123-126,
      Safari 17, and Edge 124-125 across Windows / macOS / Linux.
    - Browser-consistent header sets (Sec-CH-UA only for Chromium engines,
      correct Accept for Safari, no Sec-CH-* for Firefox).
    - Per-domain cookie jar — cookies received in responses are stored and
      re-sent on subsequent requests to the same domain.
    - Referrer chain: optionally prime the cookie jar by visiting the site
      homepage before fetching a deal URL.
    - Per-domain rate limiter (configurable max requests/minute).
    - Random delay with gaussian distribution (center=3.5 s, stddev=1 s)
      clamped to [delay_min, delay_max].
    - Retries with exponential backoff on HTTP 429, 5xx, and timeouts.
    - Optional proxy (passed straight through to httpx).
    - One httpx.AsyncClient per domain so cookies are automatically tracked
      by httpx's internal CookieJar per session.
    """

    def __init__(
        self,
        delay_min: float = 2.0,
        delay_max: float = 5.0,
        max_requests_per_minute: int = 10,
        proxy_url: str | None = None,
        max_retries: int = 3,
    ) -> None:
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.max_rpm = max_requests_per_minute
        self.max_retries = max_retries
        self._proxy_url = proxy_url

        # Per-domain httpx.AsyncClient — each carries its own CookieJar which
        # httpx automatically populates from Set-Cookie response headers.
        self._domain_clients: dict[str, httpx.AsyncClient] = {}

        # Per-domain UA — pick once and reuse so cookies are consistent
        self._domain_ua: dict[str, _UAMeta] = {}

        # Per-domain rate-limit log: {domain: [monotonic timestamps]}
        self._request_log: dict[str, list[float]] = defaultdict(list)

        # Track which domains have been "primed" (homepage visited)
        self._primed_domains: set[str] = set()

        # Lock per domain to avoid concurrent priming races
        self._prime_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------------
    # Per-domain client management
    # ------------------------------------------------------------------

    def _get_domain_ua(self, domain: str) -> _UAMeta:
        """Return a stable UA for *domain*, picking one at random on first call."""
        if domain not in self._domain_ua:
            self._domain_ua[domain] = random.choice(USER_AGENTS)
            logger.debug("UA asignado a %s: %s", domain, self._domain_ua[domain].browser)
        return self._domain_ua[domain]

    def _get_client(self, domain: str, proxy_override: str | None = None) -> httpx.AsyncClient:
        """Return (creating if needed) the persistent AsyncClient for *domain*.

        If proxy_override is provided, a separate client keyed by (domain, proxy_url)
        is created so that per-store proxies don't interfere with each other.
        """
        cache_key = f"{domain}|{proxy_override}" if proxy_override else domain
        if cache_key not in self._domain_clients:
            proxy_arg = proxy_override or self._proxy_url or None
            self._domain_clients[cache_key] = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(retries=0),
                proxy=proxy_arg,
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
            )
            logger.debug("Nuevo cliente creado para dominio: %s (proxy: %s)", domain, proxy_arg)
        return self._domain_clients[cache_key]

    # ------------------------------------------------------------------
    # Rate limiter per domain
    # ------------------------------------------------------------------

    async def _wait_for_rate_limit(self, domain: str) -> None:
        """Block until the per-domain rate limit allows another request."""
        now = time.monotonic()
        window = 60.0

        self._request_log[domain] = [
            t for t in self._request_log[domain] if now - t < window
        ]

        if len(self._request_log[domain]) >= self.max_rpm:
            oldest = self._request_log[domain][0]
            wait = window - (now - oldest) + 0.5
            logger.info("Rate limit alcanzado para %s, esperando %.1f s", domain, wait)
            await asyncio.sleep(wait)

        self._request_log[domain].append(time.monotonic())

    # ------------------------------------------------------------------
    # Gaussian random delay
    # ------------------------------------------------------------------

    async def _random_delay(self) -> None:
        delay = _gaussian_delay(
            center=3.5,
            stddev=1.0,
            min_val=self.delay_min,
            max_val=self.delay_max,
        )
        logger.debug("Delay: %.2f s", delay)
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Referrer chain / homepage priming
    # ------------------------------------------------------------------

    async def _prime_domain(self, domain: str, scheme: str) -> None:
        """Visit the homepage once to acquire initial cookies and simulate natural
        browsing behaviour.  Subsequent calls for the same domain are no-ops.
        """
        async with self._prime_locks[domain]:
            if domain in self._primed_domains:
                return

            homepage = f"{scheme}://{domain}/"
            logger.info("Priming dominio %s con visita a homepage: %s", domain, homepage)

            client = self._get_client(domain)
            ua_meta = self._get_domain_ua(domain)
            # For the homepage visit we set Sec-Fetch-Site to "none" (direct navigation)
            headers = build_headers(homepage, ua_meta)
            headers["Sec-Fetch-Site"] = "none"
            headers.pop("Referer", None)  # no referrer on direct navigation

            try:
                resp = await client.get(homepage, headers=headers)
                logger.debug(
                    "Homepage %s → HTTP %d, cookies: %s",
                    homepage, resp.status_code, dict(client.cookies),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo hacer priming de %s: %s", domain, exc)

            self._primed_domains.add(domain)

            # Small pause after homepage visit
            await asyncio.sleep(_gaussian_delay(1.5, 0.5, 1.0, 3.0))

    # ------------------------------------------------------------------
    # Core fetch with retries and exponential backoff
    # ------------------------------------------------------------------

    async def fetch(self, url: str, prime: bool = True, proxy_override: str | None = None) -> str:
        """Fetch *url* and return the response body as a string.

        Parameters
        ----------
        url:
            Target URL.
        prime:
            When True (default) the client will visit the domain homepage first
            if it hasn't done so yet, building a natural referrer chain and
            picking up any consent/session cookies.
        proxy_override:
            Optional per-store proxy URL. If provided, uses a separate client
            with this proxy instead of the global one.
        """
        parsed = urlparse(url)
        domain = parsed.netloc
        scheme = parsed.scheme

        if prime:
            await self._prime_domain(domain, scheme)

        await self._random_delay()
        await self._wait_for_rate_limit(domain)

        client = self._get_client(domain, proxy_override=proxy_override)
        ua_meta = self._get_domain_ua(domain)
        headers = build_headers(url, ua_meta)

        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(
                    "GET %s (intento %d/%d, UA=%s)",
                    url, attempt, self.max_retries, ua_meta.browser,
                )
                resp = await client.get(url, headers=headers)

                if resp.status_code == 429:
                    backoff = _backoff(attempt)
                    logger.warning(
                        "429 Too Many Requests en %s — backoff %.1f s", url, backoff
                    )
                    await asyncio.sleep(backoff)
                    # Rotate UA for retry (also update domain UA)
                    ua_meta = random.choice(USER_AGENTS)
                    self._domain_ua[domain] = ua_meta
                    headers = build_headers(url, ua_meta)
                    continue

                if 500 <= resp.status_code < 600:
                    backoff = _backoff(attempt)
                    logger.warning(
                        "HTTP %d en %s — backoff %.1f s", resp.status_code, url, backoff
                    )
                    await asyncio.sleep(backoff)
                    continue

                resp.raise_for_status()
                logger.debug(
                    "HTTP %d para %s — cookies actuales: %s",
                    resp.status_code, domain, dict(client.cookies),
                )
                return resp.text

            except httpx.TimeoutException as exc:
                last_exc = exc
                backoff = _backoff(attempt)
                logger.warning("Timeout en %s (intento %d) — backoff %.1f s", url, attempt, backoff)
                await asyncio.sleep(backoff)

            except httpx.HTTPStatusError:
                # Non-retryable status codes (4xx except 429)
                raise

            except httpx.HTTPError as exc:
                last_exc = exc
                logger.error("Error HTTP en %s: %s", url, exc)
                if attempt == self.max_retries:
                    raise

        raise httpx.HTTPError(
            f"No se pudo obtener {url} tras {self.max_retries} intentos"
        ) from last_exc

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close all underlying httpx clients."""
        for domain, client in self._domain_clients.items():
            try:
                await client.aclose()
                logger.debug("Cliente cerrado para dominio: %s", domain)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error cerrando cliente para %s: %s", domain, exc)
        self._domain_clients.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _backoff(attempt: int, base: float = 2.0, jitter: float = 2.0) -> float:
    """Return exponential backoff with random jitter: base^attempt + U(0, jitter)."""
    return math.pow(base, attempt) + random.uniform(0.0, jitter)
