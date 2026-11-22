"""Offline tests: run with  python -m unittest discover tests"""
import json
import sys
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import discount_agent as da  # noqa: E402
import yaml  # noqa: E402

CONFIG = yaml.safe_load((Path(da.ROOT) / "config.yaml").read_text(encoding="utf-8"))
WATCHES = [da.Watch(w) for w in CONFIG["products"]]
SRC = {"id": "shop", "name": "Shop"}


def watch_for(text):
    w = da.find_watch(text, WATCHES)
    return w.id if w else None


class PriceParsing(unittest.TestCase):
    def test_formats(self):
        cases = {"349.00": 349, "1 299,00 kr": 1299, "1.299,00": 1299, "1,299.50": 1299.5,
                 "1.299": 1299, 799: 799, "99,5": 99.5, "": None, "kr": None}
        for raw, expected in cases.items():
            self.assertEqual(da.parse_price(raw), expected, raw)


class Matching(unittest.TestCase):
    def test_bergs(self):
        self.assertEqual(watch_for("Copenhagen pot Green Ø21"), "bergs-copenhagen-green-21")
        self.assertEqual(watch_for("Bergs Potter Copenhagen kruka 25 cm grön"), "bergs-copenhagen-green-25")
        self.assertEqual(watch_for("Copenhagen Grøn 25 cm"), "bergs-copenhagen-green-25")
        self.assertEqual(watch_for("Copenhagen Yellow 21 cm"), "bergs-copenhagen-yellow-21")
        self.assertEqual(watch_for("Copenhagen kruka gul 25cm"), "bergs-copenhagen-yellow-25")
        self.assertIsNone(watch_for("Copenhagen Green 30 cm"))
        self.assertIsNone(watch_for("Copenhagen Black 21 cm"))
        self.assertIsNone(watch_for("Copenhagen Green 210 cm"))

    def test_pans(self):
        self.assertEqual(watch_for("Kolstålspanna 28 cm"), "ps-carbon-pan")
        self.assertEqual(watch_for("Carbon Steel Pan 24cm"), "ps-carbon-pan")
        self.assertIsNone(watch_for("Kolstålsolja för panna"))
        self.assertIsNone(watch_for("Presentkort 500 kr"))


class JsonLd(unittest.TestCase):
    def test_product_with_offers_and_listprice(self):
        page = """<html><script type="application/ld+json">
        {"@context":"https://schema.org","@graph":[{"@type":"Product","name":"Kolstålspanna 28 cm",
         "offers":{"@type":"Offer","price":"799.00","priceCurrency":"SEK",
           "availability":"https://schema.org/InStock",
           "priceSpecification":[{"@type":"UnitPriceSpecification","priceType":"https://schema.org/StrikethroughPrice","price":999}]}}]}
        </script></html>"""
        items = da.products_from_jsonld(BeautifulSoup(page, "html.parser"), "https://x.se/p/pan", SRC)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["price"], 799)
        self.assertEqual(items[0]["regular_price"], 999)
        self.assertTrue(items[0]["on_sale"])
        self.assertEqual(items[0]["currency"], "SEK")

    def test_product_group_variants_and_aggregate(self):
        page = """<script type="application/ld+json">[{"@type":"ProductGroup","name":"Copenhagen",
          "hasVariant":[{"@type":"Product","name":"Copenhagen Green 21 cm","url":"/p?v=1",
             "offers":{"@type":"Offer","price":449,"priceCurrency":"DKK"}},
           {"@type":"Product","name":"Copenhagen Yellow 25 cm","url":"/p?v=2",
             "offers":{"@type":"AggregateOffer","lowPrice":"599","priceCurrency":"DKK"}}]}]</script>"""
        items = da.products_from_jsonld(BeautifulSoup(page, "html.parser"), "https://b.dk/p", SRC)
        self.assertEqual([i["price"] for i in items], [449, 599])
        self.assertEqual(items[0]["url"], "https://b.dk/p?v=1")
        self.assertEqual(watch_for(items[1]["_match_text"]), "bergs-copenhagen-yellow-25")


class Promos(unittest.TestCase):
    def test_codes_and_campaigns(self):
        page = """<div class="banner"><p>Black Week! Använd koden BLACK25 för 25% rabatt</p></div>
                  <p>Rabatkode: <b>SOMMER10</b></p><nav><a>Sale</a></nav><p>Slutar om 3 dagar - REA</p>
                  <p>Read our code of conduct</p>"""
        codes, snippets = da.find_promos(BeautifulSoup(page, "html.parser"))
        self.assertEqual(codes, {"BLACK25", "SOMMER10"})
        self.assertTrue(any("BLACK25" in s for s in snippets.values()))
        self.assertIn(da.promo_key("Slutar om 3 dagar - REA"), snippets)
        # countdown changes must not look like a new campaign
        self.assertEqual(da.promo_key("Slutar om 2 dagar - REA"), da.promo_key("Slutar om 3 dagar - REA"))
        self.assertNotEqual(da.promo_key("20% rabatt"), da.promo_key("30% rabatt"))


def item(price, regular=None, variant="21 cm"):
    it = da.make_item(SRC, "Copenhagen Green", variant, "https://b.dk/p", price, regular, "SEK")
    it.update(watch_id="w", watch_name="Copenhagen green 21")
    return it


class Comparison(unittest.TestCase):
    def setUp(self):
        self.sources = {"shop": SRC}
        self.key = "shop|https://b.dk/p|21 cm"

    def test_first_run_sends_no_change_events(self):
        self.assertEqual(da.compare({}, {self.key: item(400)}, {}, self.sources), [])

    def test_price_drop_sale_and_codes(self):
        state = da.new_state({}, {self.key: item(500)}, {"shop": {"codes": {}, "snippets": {}}})
        # same price -> nothing
        self.assertEqual(da.compare(state, {self.key: item(500)}, {}, self.sources), [])
        # price up -> nothing
        self.assertEqual(da.compare(state, {self.key: item(550)}, {}, self.sources), [])
        # price drop
        ev = da.compare(state, {self.key: item(450)}, {}, self.sources)
        self.assertEqual([e["kind"] for e in ev], ["price_drop"])
        self.assertEqual(ev[0]["old_price"], 500)
        # sale flag without price change
        ev = da.compare(state, {self.key: item(500, regular=600)}, {}, self.sources)
        self.assertEqual([e["kind"] for e in ev], ["sale"])
        # new code + new campaign text
        promos = {"shop": {"codes": {"XMAS20": "https://b.dk"},
                           "snippets": {"x": {"text": "20% rabatt", "url": "https://b.dk"}}}}
        ev = da.compare(state, {self.key: item(500)}, promos, self.sources)
        self.assertEqual(sorted(e["kind"] for e in ev), ["campaign", "code"])
        subject, text, body_html = da.build_email(ev + da.compare(state, {self.key: item(450)}, {}, self.sources))
        self.assertIn("XMAS20", text)
        self.assertIn("Old price: 500 SEK", text)
        self.assertIn("New price: 450 SEK", text)
        self.assertIn("https://b.dk/p", body_html)

    def test_failed_shop_keeps_last_price(self):
        state = da.new_state({}, {self.key: item(500)}, {})
        state2 = da.new_state(state, {}, {})
        self.assertEqual(state2["items"][self.key]["price"], 500)
        self.assertEqual(json.loads(json.dumps(state2)), state2)


class ShopifyReader(unittest.TestCase):
    def test_variants(self):
        class FakeFetcher:
            def json(self, url):
                if "page=1" in url:
                    return {"products": [{"title": "Copenhagen", "handle": "copenhagen", "vendor": "Bergs Potter",
                                          "variants": [{"id": 1, "title": "Green / 21 cm", "price": "449.00",
                                                        "compare_at_price": "549.00", "available": True},
                                                       {"id": 2, "title": "Yellow / 25 cm", "price": "599.00",
                                                        "compare_at_price": None, "available": False}]}]}
                if "cart.js" in url:
                    return {"currency": "DKK"}
                return {"products": []}

        items = da.read_shopify(FakeFetcher(), {"id": "b", "name": "B", "base_url": "https://b.dk"})
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0]["on_sale"])
        self.assertEqual(items[0]["currency"], "DKK")
        self.assertEqual(watch_for(items[0]["_match_text"]), "bergs-copenhagen-green-21")
        self.assertEqual(watch_for(items[1]["_match_text"]), "bergs-copenhagen-yellow-25")
        self.assertFalse(items[1]["available"])


if __name__ == "__main__":
    unittest.main()
