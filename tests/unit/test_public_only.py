import socket

import httpx
import pytest

from augury.core.models import FetchState, RssRecipe, Source
from augury.sources.http import NotPublicAddress, PoliteClient
from augury.sources.public_only import PublicOnlyHttp
from augury.sources.registry import ADAPTERS
from tests.adapters.test_rss import FEED
from tests.helpers import CountingHttp, FakeTime, allow_robots

PUBLIC = "93.184.215.14"


async def public_dns(host: str) -> list[str]:
    return [PUBLIC]


async def no_dns(host: str) -> list[str]:
    raise AssertionError(f"{host} needed no DNS lookup")


def fake_dns(answers: dict[str, list[str]]):
    async def resolve(host: str) -> list[str]:
        if host not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "unknown host")
        return answers[host]

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1./",
        "http://2130706433/",  # decimal
        "http://0x7f000001/",  # hex
        "http://0177.0.0.1/",  # octal
        "http://0x7f.1/",
        "http://127.1/",  # short form
        "http://\uff11\uff12\uff17.0.0.1/",  # fullwidth digits, which IDNA folds to ASCII
        "http://10.0.0.1/",
        "http://172.16.5.4/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://0.0.0.0:8000/",
        "http://100.64.0.1/",  # shared (carrier-grade NAT)
        "http://224.0.0.1/",  # multicast
        "http://240.0.0.1/",  # reserved
        "http://255.255.255.255/",
        "http://[::1]/",
        "http://[::]/",
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped
        "http://[::ffff:7f00:1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://[64:ff9b::7f00:1]/",  # NAT64 of 127.0.0.1
        "http://[fe80::1]/",
        "http://[fe80::1%25en0]/",
        "http://[fc00::1]/",  # unique local
        "http://[fd12:3456::1]/",
        "http://[fec0::1]/",  # site local
        "http://[ff02::1]/",  # multicast
        "http://localhost:11434/api/tags",
        "http://LOCALHOST./",
        "http://api.localhost/",
    ],
)
async def test_addresses_that_are_not_public_are_refused_without_a_lookup(url):
    inner = CountingHttp()
    with pytest.raises(NotPublicAddress, match="not a public address"):
        await PublicOnlyHttp(inner, resolve=no_dns).get(url)
    assert inner.calls == []


@pytest.mark.parametrize(
    "url",
    [
        f"http://{PUBLIC}/",
        "https://[2606:4700::1111]/",
        "https://[64:ff9b::5db8:d70e]/",  # NAT64 of a public address (DNS64 networks)
        "https://[::ffff:93.184.215.14]/",
    ],
)
async def test_public_literals_are_allowed(url):
    inner = CountingHttp({url: b"ok"})
    resp = await PublicOnlyHttp(inner, resolve=no_dns).get(url)
    assert resp.content == b"ok"


@pytest.mark.parametrize(
    "host",
    ["intranet.example.com", "mixed.example.com", "v6.example.com", "mapped.example.com"],
)
async def test_a_name_with_any_private_address_is_refused(host):
    dns = fake_dns(
        {
            "intranet.example.com": ["10.0.0.5"],
            "mixed.example.com": [PUBLIC, "127.0.0.1"],
            "v6.example.com": ["fd00::1"],
            "mapped.example.com": ["::ffff:192.168.0.1"],
        }
    )
    inner = CountingHttp({f"https://{host}/": b"secret"})
    with pytest.raises(NotPublicAddress, match="not a public address"):
        await PublicOnlyHttp(inner, resolve=dns).get(f"https://{host}/")
    assert inner.calls == []


async def test_a_name_that_does_not_resolve_is_refused():
    for dns in (fake_dns({}), fake_dns({"empty.example.com": []})):
        with pytest.raises(NotPublicAddress):
            await PublicOnlyHttp(CountingHttp(), resolve=dns).get("https://empty.example.com/")


async def test_a_public_name_is_fetched_and_looked_up_once():
    lookups: list[str] = []

    async def dns(host: str) -> list[str]:
        lookups.append(host)
        return [PUBLIC, "2606:4700::1111"]

    pages = {"https://blog.example.com/a": b"a", "https://blog.example.com/b": b"b"}
    http = PublicOnlyHttp(CountingHttp(pages), resolve=dns)
    assert (await http.get("https://blog.example.com/a")).content == b"a"
    assert (await http.get("https://blog.example.com/b")).content == b"b"
    assert lookups == ["blog.example.com"]


async def test_sitemaps_are_only_read_from_public_origins():
    http = PublicOnlyHttp(CountingHttp(), resolve=no_dns)
    with pytest.raises(NotPublicAddress):
        await http.sitemaps("http://192.168.0.1/")


@pytest.mark.respx(assert_all_called=False)  # the internal routes must stay uncalled
async def test_a_redirect_to_loopback_is_refused_before_the_hop_is_sent(respx_mock):
    allow_robots(respx_mock, "https://pub.example.com")
    respx_mock.get("https://pub.example.com/post").mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1:8001/secrets"})
    )
    internal = respx_mock.get("http://127.0.0.1:8001/secrets").mock(
        return_value=httpx.Response(200, text="token")
    )
    t = FakeTime()
    async with PoliteClient(sleep=t.sleep, clock=t.clock) as client:
        http = PublicOnlyHttp(client, resolve=public_dns)
        with pytest.raises(NotPublicAddress, match="redirects to an address that is not public"):
            await http.get("https://pub.example.com/post")
    assert not internal.called


@pytest.mark.respx(assert_all_called=False)
async def test_a_robots_redirect_to_a_private_host_is_refused_too(respx_mock):
    respx_mock.get("https://pub.example.com/robots.txt").mock(
        return_value=httpx.Response(301, headers={"location": "http://10.0.0.1/robots.txt"})
    )
    internal = respx_mock.get("http://10.0.0.1/robots.txt")
    page = respx_mock.get("https://pub.example.com/post")
    t = FakeTime()
    async with PoliteClient(sleep=t.sleep, clock=t.clock) as client:
        http = PublicOnlyHttp(client, resolve=public_dns)
        with pytest.raises(NotPublicAddress):
            await http.get("https://pub.example.com/post")
        assert await http.sitemaps("https://pub.example.com/") == []
    assert not internal.called and not page.called


async def test_the_scout_still_reaches_a_lan_source(respx_mock):
    lan = "http://192.168.1.10"
    allow_robots(respx_mock, lan)
    allow_robots(respx_mock, "https://pub.example.com")
    respx_mock.get(f"{lan}/feed.xml").mock(return_value=httpx.Response(200, content=FEED))
    respx_mock.get("https://pub.example.com/").mock(return_value=httpx.Response(200))
    source = Source(
        id="nas", name="NAS", origin="user", recipe=RssRecipe(feed_url=f"{lan}/feed.xml")
    )
    t = FakeTime()
    async with PoliteClient(sleep=t.sleep, clock=t.clock) as client:
        discovery = PublicOnlyHttp(client, resolve=public_dns)  # on the same shared client
        await discovery.get("https://pub.example.com/")  # its hop check ends with the call
        with pytest.raises(NotPublicAddress):
            await discovery.get(f"{lan}/feed.xml")
        result = await ADAPTERS["rss"].fetch(source, FetchState(), client)  # the scout's
    assert len(result.items) == 3
