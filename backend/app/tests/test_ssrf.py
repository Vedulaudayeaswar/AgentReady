import httpx
import pytest

from app.services.crawler.fetcher import BlockedURL, SafeFetcher
from app.tests.sitekit import PUBLIC_IP


def make(settings, handler=None, ips=None):
    async def resolver(host, port):
        return ips or [PUBLIC_IP]

    transport = httpx.MockTransport(handler or (lambda r: httpx.Response(200, text="<html>ok</html>",
                                                                          headers={"content-type": "text/html"})))
    return SafeFetcher(settings, transport=transport, resolver=resolver)


@pytest.mark.parametrize("url", [
    "http://localhost/", "http://127.0.0.1/", "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/",
    "http://100.64.0.1/", "http://metadata.google.internal/", "http://printer.local/", "http://shop.example:8080/",
    "file:///etc/passwd", "gopher://shop.example/",
])
async def test_blocks_internal_targets(settings, url):
    f = make(settings)
    with pytest.raises(BlockedURL):
        await f.check_url(url)
    await f.aclose()


async def test_blocks_hostname_resolving_to_private_ip(settings):
    f = make(settings, ips=["93.184.216.34", "10.1.2.3"])  # any private address => blocked
    with pytest.raises(BlockedURL):
        await f.check_url("https://rebind.example/")
    res = await f.fetch("https://rebind.example/")
    assert res.error and res.error.startswith("blocked")
    await f.aclose()


async def test_connection_is_pinned_to_validated_ip(settings):
    seen = {}

    def handler(request: httpx.Request):
        seen["host_in_url"] = request.url.host
        seen["host_header"] = request.headers["host"]
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    f = make(settings, handler)
    res = await f.fetch("https://shop.example/page")
    assert res.ok
    assert seen == {"host_in_url": PUBLIC_IP, "host_header": "shop.example", "sni": "shop.example"}
    await f.aclose()


async def test_redirect_to_internal_address_is_blocked(settings):
    def handler(request: httpx.Request):
        if request.url.host == PUBLIC_IP:
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        raise AssertionError("must never connect to the metadata endpoint")

    f = make(settings, handler)
    res = await f.fetch("https://shop.example/")
    assert not res.ok and "blocked" in (res.error or "")
    await f.aclose()


async def test_response_size_limit(settings):
    settings.max_response_bytes = 1000
    f = make(settings, lambda r: httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/html"}))
    res = await f.fetch("https://shop.example/")
    assert res.error == "response exceeds size limit"
    await f.aclose()


async def test_non_text_content_is_not_read(settings):
    f = make(settings, lambda r: httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}))
    res = await f.fetch("https://shop.example/logo")
    assert res.error and "unsupported content type" in res.error
    assert res.content == b""
    await f.aclose()


async def test_too_many_redirects(settings):
    f = make(settings, lambda r: httpx.Response(302, headers={"location": "/loop"}))
    res = await f.fetch("https://shop.example/")
    assert res.error == "too many redirects"
    await f.aclose()
