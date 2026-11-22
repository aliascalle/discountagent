#!/usr/bin/env python3
"""
Discount agent
==============

Checks shops for prices on the products listed in config.yaml, saves the
prices in data/, compares them with the previous run and sends an email
(from Gmail) when something got cheaper, a sale started or a new discount
code appeared.

How a shop is read (type: auto tries these in order):
  1. Shopify  -> {base_url}/products.json      (exact price + "compare at" price)
  2. WooCommerce -> {base_url}/wp-json/wc/store/v1/products
  3. Plain HTML -> the pages listed in config are opened, product links are
     followed and prices are read from the schema.org data (JSON-LD) that
     almost every web shop embeds for Google.

Usage:
  python discount_agent.py               # normal run (used by GitHub Actions)
  python discount_agent.py --dry-run     # print the email instead of sending, save nothing
  python discount_agent.py --list-all    # also print every product found (to tune config.yaml)
  python discount_agent.py --test-email  # only send a test email to check the Gmail setup

Email settings come from environment variables (GitHub secrets):
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD, EMAIL_TO (optional, defaults to GMAIL_ADDRESS)
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "prices.json"
HISTORY_FILE = DATA_DIR / "history.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "sv-SE,sv;q=0.9,da;q=0.8,en;q=0.7",
}
TIMEOUT = 25
FORGET_AFTER_DAYS = 30  # drop products not seen for this long

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)
    WARNINGS.append(msg)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def parse_price(value) -> float | None:
    """Turn '1 299,00 kr', '1.299,00', '349.00' or 349 into a float."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^\d,.\-]", "", str(value))
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[-1]) == 3):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def fmt_price(price, currency) -> str:
    if price is None:
        return "-"
    text = f"{price:,.2f}".replace(",", " ").replace(".00", "")
    return f"{text} {currency or ''}".strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


DEBUG = False
PLATFORM_HINTS = {
    "Shopify": "cdn.shopify.com", "WooCommerce": "woocommerce", "WordPress": "wp-content",
    "Centra": "centra", "Magento": "magento", "PrestaShop": "prestashop", "Abicart": "abicart",
    "Quickbutik": "quickbutik", "Wix": "wix.com", "Squarespace": "squarespace", "Next.js": "__NEXT_DATA__",
    "Nuxt": "__NUXT__", "Litium": "litium", "Askås": "askas", "Ehandel.se": "ehandel", "Jetshop/Norce": "jetshop",
    "Starweb": "starweb", "Shopware": "shopware", "Vue/Nuxt": "data-v-", "Cloudflare": "cf-chl",
    "Klarna": "klarna", "Voyado": "voyado", "Sitoo": "sitoo", "Lightspeed": "lightspeed",
}


def describe_page(url: str, resp: requests.Response) -> None:
    """--debug: print what a page contains, so the readers can be tuned from the log."""
    body = resp.text or ""
    low = body.lower()
    print(f"\n  [debug] {url}")
    print(f"    status={resp.status_code} final={resp.url} type={resp.headers.get('Content-Type')} bytes={len(body)}")
    hdrs = {k: v for k, v in resp.headers.items()
            if k.lower() in ("server", "x-powered-by", "x-shopid", "x-generator", "via", "cf-ray", "x-cache")}
    print(f"    headers={hdrs} cookies={sorted(resp.cookies.keys())[:10]}")
    print(f"    platform hints={[n for n, h in PLATFORM_HINTS.items() if h.lower() in low]}")
    if resp.status_code >= 400 or "html" not in resp.headers.get("Content-Type", ""):
        start = " ".join(body[:400].split())
        print(f"    body start: {start!r}")
        return
    soup = BeautifulSoup(resp.content, "html.parser")
    print(f"    title={soup.title.get_text(strip=True)[:100] if soup.title else None!r}")
    print(f"    json-ld types={[_types(n) for n in _jsonld_nodes(soup)][:15]}")
    metas = [(m.get("property") or m.get("itemprop") or m.get("name"), m.get("content"))
             for m in soup.find_all("meta") if re.search(r"price|currency|product", str(m.get("property") or m.get("itemprop") or m.get("name") or ""), re.I)]
    print(f"    price metas={metas[:10]}")
    micro = [(el.name, el.get("itemprop"), el.get("content") or el.get_text(" ", strip=True)[:60])
             for el in soup.find_all(attrs={"itemprop": re.compile(r"price|offers|name", re.I)})][:10]
    print(f"    microdata={micro}")
    priced = []
    for el in soup.find_all(class_=re.compile(r"price|pris", re.I)):
        t = el.get_text(" ", strip=True)
        if t and re.search(r"\d", t) and len(t) < 80 and t not in priced:
            priced.append(t)
    print(f"    elements with 'price' class={priced[:12]}")
    data_attrs = sorted({a for el in soup.find_all(True) for a in el.attrs if re.search(r"price|product", a, re.I)})[:15]
    print(f"    data attributes={data_attrs}")
    scripts = []
    for sc in soup.find_all("script"):
        txt = sc.string or ""
        if re.search(r"\bprice\b", txt, re.I) and not sc.get("type") == "application/ld+json":
            m = re.search(r".{0,120}\bprice\b.{0,160}", txt, re.I | re.S)
            scripts.append((sc.get("id") or sc.get("type") or sc.get("src") or "inline", re.sub(r"\s+", " ", m.group(0))))
    print(f"    scripts mentioning price ({len(scripts)}): {scripts[:4]}")
    kr = re.findall(r"[^<>]{0,40}\d[\d\s.,]*\s?(?:kr|SEK|DKK|:-)[^<>]{0,20}", body)
    kr = [" ".join(k.split()) for k in kr[:6]]
    print(f"    text with 'kr' ({len(kr)}): {kr}")
    links = [a["href"] for a in soup.find_all("a", href=True)]
    print(f"    {len(links)} links, sample: {links[:25]}")


class Fetcher:
    """requests.Session with a polite delay, retries and a small cache."""

    def __init__(self, delay: float = 1.0):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.delay = delay
        self.cache: dict[str, requests.Response] = {}

    def get(self, url: str) -> requests.Response:
        if url in self.cache:
            return self.cache[url]
        last_error = None
        for attempt in range(2):
            time.sleep(self.delay * (attempt + 1))
            try:
                resp = self.session.get(url, timeout=TIMEOUT)
                if DEBUG and attempt == 0:
                    describe_page(url, resp)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                    last_error = f"HTTP {resp.status_code}"
                    continue
                resp.raise_for_status()
                self.cache[url] = resp
                return resp
            except requests.RequestException as exc:
                last_error = exc
        raise RuntimeError(f"could not fetch {url}: {last_error}")

    def json(self, url: str):
        resp = self.get(url)
        if "json" not in resp.headers.get("Content-Type", "") and not resp.text.lstrip().startswith(("{", "[")):
            raise ValueError("not JSON")
        return resp.json()

    def soup(self, url: str) -> BeautifulSoup:
        # pass raw bytes so BeautifulSoup reads the page's own charset (å, ä, ö, ø)
        return BeautifulSoup(self.get(url).content, "html.parser")


# --------------------------------------------------------------------------
# Matching listings against the products you watch
# --------------------------------------------------------------------------

class Watch:
    def __init__(self, cfg: dict):
        self.id = cfg["id"]
        self.name = cfg["name"]
        self.group = cfg.get("group", "default")
        self.include = [re.compile(p, re.I) for p in cfg.get("match", [])]
        self.exclude = re.compile(cfg["exclude"], re.I) if cfg.get("exclude") else None

    def matches(self, text: str) -> bool:
        if self.exclude and self.exclude.search(text):
            return False
        return all(p.search(text) for p in self.include)


def find_watch(text: str, watches: list[Watch]) -> Watch | None:
    for w in watches:
        if w.matches(text):
            return w
    return None


def make_item(source: dict, title: str, variant: str, url: str, price, regular=None,
              currency=None, available=True) -> dict | None:
    price = parse_price(price)
    if price is None or price <= 0:
        return None
    regular = parse_price(regular)
    if regular is not None and regular <= price:
        regular = None
    return {
        "source": source["id"],
        "shop": source["name"],
        "title": html.unescape(title or "").strip(),
        "variant": html.unescape(variant or "").strip(),
        "url": url,
        "price": price,
        "regular_price": regular,
        "currency": currency or source.get("currency", ""),
        "available": bool(available),
        "on_sale": regular is not None,
    }


# --------------------------------------------------------------------------
# Readers for each kind of shop
# --------------------------------------------------------------------------

def read_shopify(f: Fetcher, source: dict) -> list[dict] | None:
    base = source["base_url"].rstrip("/")
    products = []
    for page in range(1, 11):
        try:
            data = f.json(f"{base}/products.json?limit=250&page={page}")
        except Exception:
            if page == 1:
                return None
            break
        batch = data.get("products") if isinstance(data, dict) else None
        if batch is None:
            return None if page == 1 else []
        if not batch:
            break
        products.extend(batch)

    currency = source.get("currency")
    if not currency:
        try:
            currency = f.json(f"{base}/cart.js").get("currency")
        except Exception:
            currency = ""

    items = []
    for p in products:
        for v in p.get("variants", []):
            vtitle = "" if v.get("title") in (None, "Default Title") else v["title"]
            item = make_item(
                source,
                title=p.get("title", ""),
                variant=vtitle,
                url=f"{base}/products/{p.get('handle')}?variant={v.get('id')}",
                price=v.get("price"),
                regular=v.get("compare_at_price"),
                currency=currency,
                available=v.get("available", True),
            )
            if item:
                item["_match_text"] = " ".join([p.get("title", ""), vtitle, p.get("vendor", ""),
                                                p.get("product_type", "")])
                items.append(item)
    print(f"  Shopify store: {len(products)} products")
    return items


def read_woocommerce(f: Fetcher, source: dict) -> list[dict] | None:
    base = source["base_url"].rstrip("/")
    terms = source.get("search") or [""]
    seen, items = set(), []
    for term in terms:
        for page in range(1, 6):
            url = f"{base}/wp-json/wc/store/v1/products?per_page=100&page={page}"
            if term:
                url += "&search=" + requests.utils.quote(term)
            try:
                batch = f.json(url)
            except Exception:
                if page == 1 and not items and term == terms[0]:
                    return None
                break
            if not isinstance(batch, list):
                return None if not items else items
            if not batch:
                break
            for p in batch:
                if p.get("id") in seen:
                    continue
                seen.add(p.get("id"))
                prices = p.get("prices", {})
                div = 10 ** int(prices.get("currency_minor_unit", 2))
                to_num = lambda x: (int(x) / div) if str(x or "").isdigit() else None  # noqa: E731
                item = make_item(
                    source,
                    title=p.get("name", ""),
                    variant="",
                    url=p.get("permalink", base),
                    price=to_num(prices.get("price")),
                    regular=to_num(prices.get("regular_price")),
                    currency=prices.get("currency_code"),
                    available=p.get("is_in_stock", True),
                )
                if item:
                    items.append(item)
    print(f"  WooCommerce store: {len(items)} products")
    return items


def _jsonld_nodes(soup: BeautifulSoup):
    def walk(node):
        if isinstance(node, list):
            for n in node:
                yield from walk(n)
        elif isinstance(node, dict):
            yield node
            for key in ("@graph", "itemListElement", "item", "mainEntity"):
                if key in node:
                    yield from walk(node[key])

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        yield from walk(data)


def _types(node: dict) -> list[str]:
    t = node.get("@type", [])
    return t if isinstance(t, list) else [t]


def _offer_list(offers):
    if isinstance(offers, dict):
        if "offers" in offers and isinstance(offers["offers"], list) and "lowPrice" not in offers:
            return offers["offers"]
        return [offers]
    if isinstance(offers, list):
        return [o for o in offers if isinstance(o, dict)]
    return []


def _regular_from_offer(offer: dict):
    specs = offer.get("priceSpecification")
    for spec in specs if isinstance(specs, list) else [specs]:
        if isinstance(spec, dict) and re.search(r"ListPrice|StrikethroughPrice", str(spec.get("priceType", ""))):
            return spec.get("price")
    return None


def products_from_jsonld(soup: BeautifulSoup, page_url: str, source: dict) -> list[dict]:
    items = []

    def handle_product(node: dict, parent_name: str = ""):
        name = node.get("name") or parent_name
        url = urljoin(page_url, node.get("url") or node.get("@id") or page_url).split("#")[0]
        variant_bits = [str(node.get(k)) for k in ("color", "size") if node.get(k)]
        offers = _offer_list(node.get("offers"))
        for offer in offers:
            price = offer.get("price", offer.get("lowPrice"))
            spec = offer.get("priceSpecification")
            if isinstance(spec, list):  # e.g. Nordic Nest: [{price}, {price, priceType: StrikethroughPrice}]
                spec = next((x for x in spec if isinstance(x, dict) and x.get("price") is not None
                             and not re.search(r"ListPrice|StrikethroughPrice", str(x.get("priceType", "")))), None)
            if price is None and isinstance(spec, dict):
                price = spec.get("price")
            variant = " ".join(variant_bits)
            if offer.get("name") and offer.get("name") != name:
                variant = (variant + " " + str(offer["name"])).strip()
            title = name if not parent_name or name == parent_name else f"{parent_name} {name}"
            item = make_item(
                source, title=title, variant=variant,
                url=urljoin(page_url, offer.get("url") or url),
                price=price, regular=_regular_from_offer(offer),
                currency=offer.get("priceCurrency") or (spec.get("priceCurrency") if isinstance(spec, dict) else None),
                available="OutOfStock" not in str(offer.get("availability", "")),
            )
            if item:
                item["_match_text"] = f"{title} {variant} {urlparse(item['url']).path}"
                items.append(item)
        for v in node.get("hasVariant", []) or []:
            if isinstance(v, dict):
                handle_product(v, parent_name=name)

    for node in _jsonld_nodes(soup):
        types = _types(node)
        if "Product" in types or "ProductGroup" in types:
            handle_product(node)
    return items


def products_from_meta(soup: BeautifulSoup, page_url: str, source: dict) -> list[dict]:
    """Fallback for pages without JSON-LD: Open Graph / microdata price tags."""
    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"itemprop": n})
            if tag and tag.get("content"):
                return tag["content"]
        return None

    price = meta("product:price:amount", "og:price:amount", "price")
    if price is None:
        return []
    title = meta("og:title") or (soup.title.string if soup.title else "")
    item = make_item(source, title=title, variant="", url=page_url, price=price,
                     regular=meta("product:original_price:amount"),
                     currency=meta("product:price:currency", "og:price:currency", "priceCurrency"))
    if item:
        item["_match_text"] = f"{title} {urlparse(page_url).path}"
        return [item]
    return []


PRICE_KEYS = ("salesPriceInclVat", "salePrice", "currentPrice", "finalPrice", "sellingPrice", "priceInclVat",
              "inclVat", "amount", "value", "price", "current")
REGULAR_KEYS = ("recommendedPriceInclVat", "regularPriceInclVat", "regularPrice", "originalPrice", "listPrice",
                "compareAtPrice", "compare_at_price", "previousPrice", "beforePrice", "ordinaryPrice", "original",
                "regular")
NAME_KEYS = ("name", "title", "productName", "displayName")
URL_KEYS = ("url", "href", "link", "uri", "path", "canonicalUrl", "productUrl", "slug")
VARIANT_KEYS = ("variantName", "variant", "color", "colour", "size")


def _iter_embedded_json(soup: BeautifulSoup):
    """Yield JSON data that shops embed in <script> tags (window.__STATE__ = {...}, application/json, ...)."""
    decoder = json.JSONDecoder()
    for sc in soup.find_all("script"):
        if sc.get("src") or sc.get("type") == "application/ld+json":
            continue
        text = sc.string or sc.get_text() or ""
        if len(text) < 200 or "price" not in text.lower():
            continue
        starts = [0] if text.lstrip()[:1] in "{[" else []
        starts += [m.end() - 1 for m in re.finditer(r"(?:=|\(|:)\s*[\[{]", text)][:50]
        consumed = -1
        for pos in starts:
            if pos <= consumed:
                continue
            try:
                data, end = decoder.raw_decode(text.lstrip() if pos == 0 else text[pos:])
            except ValueError:
                continue
            consumed = pos + end
            yield data


def _walk_dicts(node, limit=200000):
    stack, seen = [node], 0
    while stack and seen < limit:
        cur = stack.pop()
        seen += 1
        if isinstance(cur, dict):
            yield cur
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))


def _first(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def products_from_embedded_json(soup: BeautifulSoup, page_url: str, source: dict) -> list[dict]:
    items, seen = [], set()
    for data in _iter_embedded_json(soup):
        for d in _walk_dicts(data):
            name = _first(d, NAME_KEYS)
            price_field = d.get("price", _first(d, ("prices", "pricing", "priceInfo")))
            if not isinstance(name, str) or price_field is None:
                continue
            currency = _first(d, ("currency", "currencyCode", "priceCurrency"))
            regular = _first(d, REGULAR_KEYS)
            if isinstance(price_field, dict):
                price = _first(price_field, PRICE_KEYS)
                regular = regular or _first(price_field, REGULAR_KEYS)
                currency = currency or _first(price_field, ("currency", "currencyCode", "priceCurrency"))
            else:
                price = _first(d, PRICE_KEYS[:5]) or price_field
            if isinstance(price, dict):
                price = _first(price, PRICE_KEYS)
            if isinstance(regular, dict):
                regular = _first(regular, PRICE_KEYS)
            if not isinstance(price, (int, float, str)):
                continue
            link = _first(d, URL_KEYS)
            url = urljoin(page_url, link) if isinstance(link, str) and not link.startswith(("data:", "javascript")) else page_url
            variant = " ".join(str(d[k]) for k in VARIANT_KEYS if isinstance(d.get(k), (str, int, float)))
            key = (name, url, variant, str(price))
            if key in seen:
                continue
            seen.add(key)
            item = make_item(source, title=name, variant=variant, url=url, price=price, regular=regular,
                             currency=currency if isinstance(currency, str) else None,
                             available=d.get("inStock", d.get("available", True)) is not False)
            if item:
                item["_match_text"] = f"{name} {variant} {urlparse(url).path}"
                items.append(item)
    return items


TEXT_NOW_RE = re.compile(r"(?i)\b(?:nu|now|pris|price)\s*:?\s*(\d[\d\s.]*(?:,\d{2})?)\s*(?:kr|sek|:-)")
TEXT_REG_RE = re.compile(r"(?i)\(\s*(?:ord\.?|ordinarie|ord pris|før|tidigare)?\s*:?\s*(\d[\d\s.]*(?:,\d{2})?)\s*(?:kr|sek|:-)\s*\)")
TEXT_ANY_RE = re.compile(r"(?i)(?<![\w%])(\d{1,3}(?:[ .]?\d{3})*(?:,\d{2})?)\s?(?:kr|sek|:-)(?!\w)")


def products_from_text(soup: BeautifulSoup, page_url: str, source: dict) -> list[dict]:
    """Last resort for simple sites: read 'NU 449kr (ord 749kr)' style prices from the page text."""
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ").replace("\u200b", "").split())
    now = TEXT_NOW_RE.search(text)
    if now:
        price = now.group(1)
        reg = TEXT_REG_RE.search(text[now.end():now.end() + 80])
    else:
        anyp = TEXT_ANY_RE.search(text)
        if not anyp:
            return []
        price = anyp.group(1)
        reg = TEXT_REG_RE.search(text[anyp.end():anyp.end() + 80])
    title = soup.title.get_text(strip=True) if soup.title else ""
    title = re.split(r"\s+[-|–]\s+", title)[0]
    h1 = soup.find("h1")
    if not title and h1:
        title = h1.get_text(" ", strip=True)
    item = make_item(source, title=title, variant="", url=page_url, price=price,
                     regular=reg.group(1) if reg else None, currency=source.get("currency", "SEK"))
    if item:
        item["_match_text"] = f"{title} {urlparse(page_url).path}"
        return [item]
    return []


def read_html(f: Fetcher, source: dict, watches: list[Watch]) -> list[dict]:
    pages = source.get("pages", [])
    link_re = re.compile(source["link_pattern"], re.I) if source.get("link_pattern") else None
    keys = [w.include[0] for w in watches if w.include]
    require = re.compile(source["require"], re.I) if source.get("require") else None
    max_pages = int(source.get("max_product_pages", 25))
    note = (lambda m: print(f"  note: {m}")) if source.get("optional") else warn

    items, listing_items, candidates = [], [], []
    for page in pages:
        try:
            soup = f.soup(page)
        except Exception as exc:
            note(f"{source['name']}: {exc}")
            continue
        items += products_from_jsonld(soup, page, source)
        listing_items += products_from_embedded_json(soup, page, source)
        host = urlparse(page).netloc
        # product links listed in JSON-LD ItemLists (search/category pages)
        for node in _jsonld_nodes(soup):
            link = node.get("url") if "ListItem" in _types(node) else None
            if isinstance(link, str):
                href = urljoin(page, link)
                if href not in candidates and (not require or require.search(href)) and any(k.search(href) for k in keys):
                    candidates.append(href)
        for a in soup.find_all("a", href=True):
            href = urljoin(page, a["href"]).split("#")[0]
            if source.get("ignore_query"):  # same page linked once per colour/size variant
                href = href.split("?")[0]
            if urlparse(href).netloc != host or href in candidates or href in pages:
                continue
            text = f"{a.get_text(' ', strip=True)} {urlparse(href).path}"
            if link_re and not link_re.search(href):
                continue
            if require and not require.search(text):
                continue
            if not any(k.search(text) for k in keys):
                continue
            candidates.append(href)

    if len(candidates) > max_pages:
        note(f"{source['name']}: {len(candidates)} product links found, only checking the first {max_pages}")
    for url in candidates[:max_pages]:
        try:
            soup = f.soup(url)
        except Exception as exc:
            note(f"{source['name']}: {exc}")
            continue
        found = (products_from_jsonld(soup, url, source) or products_from_meta(soup, url, source)
                 or products_from_embedded_json(soup, url, source))
        if not found and source.get("text_prices"):
            found = products_from_text(soup, url, source)
        items += found
    if not items:
        items = listing_items  # only use data embedded in list pages when product pages gave nothing
    print(f"  HTML pages: {len(pages)} listing pages, {len(candidates[:max_pages])} product pages, {len(items)} prices")
    return items


# --------------------------------------------------------------------------
# Discount codes and campaign texts
# --------------------------------------------------------------------------

CODE_RE = re.compile(
    r"(?i:\b(?:rabatt?koden?|rabatt?kode[n]?|kampanjkoden?|discount\s*code|promo\s*code|koden?|kode[n]?|code))"
    r"\s*[:\-–]?\s*[\"'“”«»‘’]?\s*([A-Z0-9][A-Z0-9_\-]{2,19})\b"
)
PROMO_RE = re.compile(
    r"(?i)(black\s*(friday|week)|cyber\s*monday|singles\s*day|mellandagsrea|\brea\b|\budsalg|"
    r"\bsale\b|kampanj|\btilbud|\brabatt?\b|\d{1,2}\s?%\s*(rabatt?|off|på|\-)|\-\s?\d{1,2}\s?%|fri frakt|gratis fragt|free shipping)"
)
CODE_STOPWORDS = {"SE", "DK", "SEK", "DKK", "EUR", "PDF", "HTML", "CODE", "KOD"}


def promo_key(text: str) -> str:
    """Normalise a snippet so countdown timers etc. don't look like new campaigns."""
    return re.sub(r"(?<!\d)\d+(?!\d)(?!\s?%)", "#", re.sub(r"\s+", " ", text.lower())).strip()


def find_promos(soup: BeautifulSoup) -> tuple[set[str], dict[str, str]]:
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    codes, snippets = set(), {}
    candidates, short_texts = [], set()
    for el in soup.find_all(["p", "a", "span", "div", "li", "h1", "h2", "h3", "h4", "strong", "b", "button"]):
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if 8 <= len(text) <= 160:
            short_texts.add(text)
            if PROMO_RE.search(text):
                candidates.append(text)
    for text in sorted(set(candidates), key=len, reverse=True):
        if not any(text in kept for kept in snippets.values()):
            snippets[promo_key(text)] = text
    for text in short_texts | set(soup.stripped_strings):
        for m in CODE_RE.finditer(text):
            code = m.group(1)
            if re.search(r"[A-Z]", code) and len(code) >= 4 and code not in CODE_STOPWORDS:
                codes.add(code)
    return codes, snippets


def scan_promos(f: Fetcher, source: dict) -> dict | None:
    urls = list(dict.fromkeys(([source["base_url"]] if source.get("base_url") else []) +
                              source.get("promo_pages", []) + source.get("pages", [])))
    codes, snippets, ok = {}, {}, False
    for url in urls:
        try:
            c, s = find_promos(f.soup(url))
        except Exception:
            continue  # already warned in read_html, or a missing optional page
        ok = True
        for code in c:
            codes.setdefault(code, url)
        for k, v in s.items():
            snippets.setdefault(k, {"text": v, "url": url})
    return {"codes": codes, "snippets": snippets} if ok else None


# --------------------------------------------------------------------------
# Running all sources
# --------------------------------------------------------------------------

def collect(config: dict, list_all: bool = False):
    watches_all = [Watch(w) for w in config["products"]]
    f = Fetcher(delay=float(config.get("request_delay_seconds", 1.0)))
    items: dict[str, dict] = {}
    promos: dict[str, dict] = {}
    ok_sources: set[str] = set()

    for source in config["sources"]:
        if source.get("enabled", True) is False:
            continue
        print(f"\n== {source['name']} ({source['id']})")
        watches = [w for w in watches_all if w.group in source.get("groups", [w.group])]
        kind = source.get("type", "auto")
        found = None
        try:
            if kind in ("auto", "shopify") and source.get("base_url"):
                found = read_shopify(f, source)
            if found is None and kind in ("auto", "woocommerce") and source.get("base_url"):
                found = read_woocommerce(f, source)
            if found is None and kind in ("auto", "html"):
                found = read_html(f, source, watches)
        except Exception as exc:  # never let one shop break the whole run
            warn(f"{source['name']}: unexpected error: {exc}")
            found = None
        if found is None:
            found = []

        require = re.compile(source["require"], re.I) if source.get("require") else None
        matched = 0
        for it in found:
            text = it.pop("_match_text", None) or f"{it['title']} {it['variant']}"
            if list_all:
                print(f"    seen: {it['title']} | {it['variant']} | {fmt_price(it['price'], it['currency'])} | {it['url']}")
            if require and not require.search(f"{text} {it['url']}"):
                continue
            w = find_watch(text, watches)
            if not w:
                continue
            it["watch_id"], it["watch_name"] = w.id, w.name
            key = f"{it['source']}|{it['url']}|{it['variant']}"
            items[key] = it
            matched += 1
        print(f"  -> {matched} matching prices")
        if found:
            ok_sources.add(source["id"])
        elif source.get("optional"):
            print("  (optional source gave no prices; skipping)")
        else:
            warn(f"{source['name']}: no prices found")

        p = scan_promos(f, source)
        if p is not None:
            promos[source["id"]] = p
            print(f"  promo texts: {len(p['snippets'])}, codes: {sorted(p['codes']) or 'none'}")

    return items, promos, ok_sources


# --------------------------------------------------------------------------
# State (data/prices.json) and comparison
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def compare(state: dict, items: dict, promos: dict, sources_by_id: dict) -> list[dict]:
    first_run = not state
    if first_run:
        return []
    old_items = state.get("items", {})
    old_promos = state.get("promos", {})
    events = []

    for key, it in items.items():
        old = old_items.get(key)
        if old is None:
            if it["on_sale"]:
                events.append({"kind": "sale", "item": it, "old_price": it["regular_price"]})
            continue
        same_currency = (old.get("currency") or "") == (it.get("currency") or "")
        if same_currency and it["price"] < old["price"] - 0.009:
            events.append({"kind": "price_drop", "item": it, "old_price": old["price"]})
        elif it["on_sale"] and not old.get("on_sale"):
            events.append({"kind": "sale", "item": it, "old_price": it["regular_price"] or old["price"]})

    for sid, p in promos.items():
        shop = sources_by_id[sid]["name"]
        before = old_promos.get(sid)
        old_codes = set((before or {}).get("codes", {}))
        for code, url in p["codes"].items():
            if code not in old_codes:
                events.append({"kind": "code", "shop": shop, "code": code, "url": url})
        if before is None:
            continue  # first time this shop's pages were readable: don't flood
        for k, s in p["snippets"].items():
            if k not in before.get("snippets", {}):
                events.append({"kind": "campaign", "shop": shop, "text": s["text"], "url": s["url"]})
    return events


def new_state(state: dict, items: dict, promos: dict) -> dict:
    ts = now_iso()
    merged = {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=FORGET_AFTER_DAYS)).isoformat()
    for key, old in state.get("items", {}).items():
        if key not in items and old.get("last_seen", ts) >= cutoff:
            merged[key] = old  # shop failed today or item hidden: keep last known price
    for key, it in items.items():
        old = state.get("items", {}).get(key, {})
        lowest = min(filter(None, [old.get("lowest_price"), it["price"]]))
        merged[key] = {**it, "first_seen": old.get("first_seen", ts), "last_seen": ts, "lowest_price": lowest}
    all_promos = dict(state.get("promos", {}))
    all_promos.update(promos)
    return {"last_run": ts, "items": dict(sorted(merged.items())), "promos": all_promos}


def append_history(items: dict) -> None:
    new_file = not HISTORY_FILE.exists()
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["date", "shop", "product", "listing", "variant", "price", "regular_price",
                        "currency", "available", "url"])
        day = datetime.now(timezone.utc).date().isoformat()
        for it in sorted(items.values(), key=lambda x: (x["shop"], x["watch_name"], x["price"])):
            w.writerow([day, it["shop"], it["watch_name"], it["title"], it["variant"], it["price"],
                        it["regular_price"] or "", it["currency"], it["available"], it["url"]])


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def build_email(events: list[dict]) -> tuple[str, str, str]:
    drops = [e for e in events if e["kind"] in ("price_drop", "sale")]
    codes = [e for e in events if e["kind"] == "code"]
    camps = [e for e in events if e["kind"] == "campaign"]
    parts = []
    if drops:
        parts.append(f"{len(drops)} lower price{'s' if len(drops) > 1 else ''}/sale{'s' if len(drops) > 1 else ''}")
    if codes:
        parts.append(f"{len(codes)} discount code{'s' if len(codes) > 1 else ''}")
    if camps:
        parts.append(f"{len(camps)} new campaign{'s' if len(camps) > 1 else ''}")
    subject = "Discount agent: " + ", ".join(parts)

    text, rows = [], []
    esc = html.escape
    for e in drops:
        it = e["item"]
        label = "Price drop" if e["kind"] == "price_drop" else "On sale"
        name = f"{it['title']} {it['variant']}".strip()
        stock = "" if it["available"] else " (out of stock)"
        text.append(f"{label}: {it['watch_name']}\n  {name}{stock}\n  Shop: {it['shop']}\n"
                    f"  Old price: {fmt_price(e['old_price'], it['currency'])}\n"
                    f"  New price: {fmt_price(it['price'], it['currency'])}\n  Link: {it['url']}\n")
        rows.append(f"<tr><td>{label}</td><td><b>{esc(it['watch_name'])}</b><br><small>{esc(name)}{stock}</small></td>"
                    f"<td>{esc(it['shop'])}</td><td><s>{fmt_price(e['old_price'], it['currency'])}</s></td>"
                    f"<td><b>{fmt_price(it['price'], it['currency'])}</b></td>"
                    f"<td><a href=\"{esc(it['url'])}\">Open</a></td></tr>")
    code_html = []
    for e in codes:
        text.append(f"Discount code at {e['shop']}: {e['code']}\n  Seen on: {e['url']}\n")
        code_html.append(f"<li><b>{esc(e['shop'])}</b>: code <code style=\"font-size:1.1em\">{esc(e['code'])}</code> "
                         f"(<a href=\"{esc(e['url'])}\">page</a>)</li>")
    for e in camps:
        text.append(f"New campaign text at {e['shop']}: \"{e['text']}\"\n  Page: {e['url']}\n")
        code_html.append(f"<li><b>{esc(e['shop'])}</b>: “{esc(e['text'])}” (<a href=\"{esc(e['url'])}\">page</a>)</li>")

    body_text = "\n".join(text) + "\n-- Your discount agent (GitHub Actions)"
    body_html = "<html><body style=\"font-family:sans-serif\">"
    if rows:
        body_html += ("<h3>Lower prices and sales</h3><table border=\"1\" cellpadding=\"6\" "
                      "style=\"border-collapse:collapse\"><tr><th></th><th>Product</th><th>Shop</th>"
                      "<th>Old price</th><th>New price</th><th>Link</th></tr>" + "".join(rows) + "</table>")
    if code_html:
        body_html += "<h3>Discount codes and campaigns</h3><ul>" + "".join(code_html) + "</ul>"
    body_html += "<p style=\"color:#888\">Sent by your discount agent on GitHub Actions.</p></body></html>"
    return subject, body_text, body_html


def send_email(subject: str, body_text: str, body_html: str | None = None) -> None:
    sender = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    to = os.environ.get("EMAIL_TO", "").strip() or sender
    if not sender or not password:
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set (as GitHub secrets).")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Discount agent <{sender}>", to
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)
    print(f"Email sent: {subject}")


def first_run_email(items: dict, promos: dict) -> tuple[str, str]:
    lines = ["Your discount agent ran for the first time. This is what it found.",
             "From now on you only get an email when something changes.\n"]
    for it in sorted(items.values(), key=lambda x: (x["watch_name"], x["shop"], x["price"])):
        lines.append(f"- {it['watch_name']} | {it['shop']} | {fmt_price(it['price'], it['currency'])}"
                     f"{' (on sale)' if it['on_sale'] else ''} | {it['url']}")
    if not items:
        lines.append("(No prices found yet - see the Actions run summary for warnings.)")
    codes = [f"{c} ({sid})" for sid, p in promos.items() for c in p["codes"]]
    if codes:
        lines.append("\nDiscount codes seen: " + ", ".join(codes))
    if WARNINGS:
        lines.append("\nWarnings:\n" + "\n".join(f"- {w}" for w in WARNINGS))
    return "Discount agent is up and running", "\n".join(lines)


def write_summary(items: dict, events: list[dict]) -> None:
    """Shows a table on the GitHub Actions run page."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    out = ["## Discount agent", f"{len(items)} prices found, {len(events)} changes to report.\n",
           "| Product | Shop | Price | Sale | Link |", "|---|---|---|---|---|"]
    for it in sorted(items.values(), key=lambda x: (x["watch_name"], x["price"])):
        out.append(f"| {it['watch_name']} – {it['title']} {it['variant']} | {it['shop']} | "
                   f"{fmt_price(it['price'], it['currency'])} | "
                   f"{'was ' + fmt_price(it['regular_price'], it['currency']) if it['on_sale'] else ''} | "
                   f"[link]({it['url']}) |")
    if WARNINGS:
        out += ["\n### Warnings (a shop could not be read)"] + [f"- {w}" for w in WARNINGS]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the email, don't send or save anything")
    ap.add_argument("--list-all", action="store_true", help="print every product found, matched or not")
    ap.add_argument("--test-email", action="store_true", help="only send a test email")
    ap.add_argument("--debug", action="store_true",
                    help="print details of every page fetched (implies --dry-run and --list-all)")
    args = ap.parse_args()
    if args.debug:
        global DEBUG
        DEBUG = args.dry_run = args.list_all = True

    if args.test_email:
        send_email("Discount agent: test email",
                   "It works! Your discount agent can send email from GitHub Actions.")
        return 0

    WARNINGS.clear()
    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    sources_by_id = {s["id"]: s for s in config["sources"]}
    state = load_state()
    items, promos, ok_sources = collect(config, list_all=args.list_all)
    events = compare(state, items, promos, sources_by_id)
    write_summary(items, events)
    print(f"\n{len(items)} matching prices, {len(events)} changes to report, {len(WARNINGS)} warnings")

    if not state:
        subject, body = first_run_email(items, promos)
        body_html = None
    elif events:
        subject, body, body_html = build_email(events)
    else:
        subject = None

    if args.dry_run:
        if subject:
            print(f"\n--- email (not sent) ---\nSubject: {subject}\n\n{body}")
        return 0

    if subject:
        send_email(subject, body, body_html)

    DATA_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(new_state(state, items, promos), indent=2,
                                     ensure_ascii=False) + "\n", encoding="utf-8")
    if items:
        append_history(items)

    if not ok_sources:
        print("ERROR: no shop could be read at all.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
