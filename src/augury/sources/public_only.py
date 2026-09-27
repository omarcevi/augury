"""Public addresses only, for fetches a model or a fetched page chose (the discovery tools).

A discovery run follows URLs that a model wrote or a page supplied (feed links, robots.txt
Sitemap: lines, sitemap <loc>s, redirects), so without a check a hostile page could point it at
the user's machine, LAN or cloud metadata service and read the answer back to the model. The
scout is not wrapped: a source the user confirmed may well live on their LAN.

PublicOnlyHttp checks each URL before it is requested, sets sources.http.URL_GUARD so
PoliteClient checks every hop the call sends (redirects, robots.txt), and checks the final URL.
Residual risk: DNS rebinding. The name is resolved for the check and again, separately, when
httpx connects, so a name that answers public then private in between is not caught."""

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from augury.sources.http import URL_GUARD, HttpClient, HttpError, NotPublicAddress, Response

Resolve = Callable[[str], Awaitable[list[str]]]
IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

REFUSED = "refused: not a public address"
REDIRECTED = "refused: it redirects to an address that is not public"
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")  # DNS64 networks answer with these for public hosts
# The legacy IPv4 forms inet_aton accepts: 2130706433, 0x7f000001, 0177.0.0.1, 127.1, ...
_LEGACY_IPV4 = re.compile(
    r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}", re.IGNORECASE | re.ASCII
)


async def system_resolve(host: str) -> list[str]:
    """Every address the system resolver gives for `host` (loop.getaddrinfo)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_public(addr: IpAddress) -> bool:
    """A globally reachable unicast address. Refuses loopback, private, link-local (so the
    169.254.169.254 metadata service), unique-local, site-local, multicast, reserved, shared
    (100.64/10) and unspecified addresses; IPv4-mapped and NAT64 IPv6 are judged by their IPv4."""
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return is_public(addr.ipv4_mapped)
        if addr in _NAT64:
            return is_public(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
        if addr.is_site_local:
            return False
    return addr.is_global and not (
        addr.is_multicast
        or addr.is_reserved
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_unspecified
    )


def ip_literal(host: str) -> IpAddress | None:
    """`host` as an IP address when it is one in any spelling a resolver would accept."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if _LEGACY_IPV4.fullmatch(host):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    return None


def _host(url: str) -> str:
    """The URL's host, normalized the way a resolver sees it (IDNA, no trailing dot)."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    host = host.rstrip(".")
    if not host:
        raise NotPublicAddress(url, "refused: no host name")
    if ip_literal(host) is None:
        try:
            host = host.encode("idna").decode("ascii").lower()  # NFKC: fullwidth digits too
        except UnicodeError as e:
            raise NotPublicAddress(url, "refused: not a valid host name") from e
    return host


class PublicOnlyHttp:
    """An HttpClient that only reaches public addresses (see the module docstring)."""

    # For the HttpClient protocol only: the wrapped client does the waiting, and reports it
    # through its own on_wait.
    on_wait: Callable[[str, float], None] | None = None

    def __init__(self, http: HttpClient, *, resolve: Resolve | None = None) -> None:
        self._http = http
        self._resolve = resolve
        self._verdicts: dict[str, bool] = {}  # host -> public, for this client's lifetime

    async def check(self, url: str) -> None:
        """Raises NotPublicAddress unless every address of the URL's host is public."""
        host = _host(url)
        public = self._verdicts.get(host)
        if public is None:
            public = await self._judge(url, host)
            self._verdicts[host] = public
        if not public:
            raise NotPublicAddress(url, REFUSED)

    async def _judge(self, url: str, host: str) -> bool:
        if host == "localhost" or host.endswith(".localhost"):
            return False
        if (literal := ip_literal(host)) is not None:
            return is_public(literal)
        resolve = self._resolve or system_resolve  # looked up per call, so tests can swap it
        try:
            answers = await resolve(host)
        except OSError as e:
            raise NotPublicAddress(url, "refused: the host name doesn't resolve") from e
        addresses = [ip_literal(a) for a in answers]
        return bool(addresses) and all(a is not None and is_public(a) for a in addresses)

    async def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        respect_robots: bool = True,
    ) -> Response:
        await self.check(url)
        token = URL_GUARD.set(self.check)
        try:
            resp = await self._http.get(
                url, etag=etag, last_modified=last_modified, respect_robots=respect_robots
            )
        except NotPublicAddress as e:  # a hop: name the URL asked for, not where it led
            raise NotPublicAddress(url, REDIRECTED) from e
        finally:
            URL_GUARD.reset(token)
        try:
            await self.check(resp.url)  # the final URL, for clients without the hop check
        except HttpError as e:
            raise NotPublicAddress(url, REDIRECTED) from e
        return resp

    async def sitemaps(self, url: str) -> list[str]:
        await self.check(url)
        token = URL_GUARD.set(self.check)
        try:
            return await self._http.sitemaps(url)
        except NotPublicAddress:
            return []  # robots.txt redirects somewhere private: no sitemaps from it
        finally:
            URL_GUARD.reset(token)
