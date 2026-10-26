from __future__ import annotations

from datetime import date, timedelta
import unittest

from ndc_ops_copilot import NdcAgent, Passenger, SearchRequest
from ndc_ops_copilot.models import ConflictError, NotFoundError, utc_now
from ndc_ops_copilot.parser import parse_command, parse_search_args
from ndc_ops_copilot.providers.mock import MockSupplierAdapter


class NdcAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = NdcAgent(MockSupplierAdapter(), session_id="test-session")
        self.future_date = date.today() + timedelta(days=90)

    def test_complete_search_price_order_flow(self) -> None:
        offers = self.agent.search(SearchRequest("sha", "fra", self.future_date, 1))
        self.assertEqual(len(offers), 3)

        priced = self.agent.price_offer(offers[0].offer_id)
        pending = self.agent.prepare_order(offers[0].offer_id)
        order = self.agent.confirm_order(
            pending.token,
            (Passenger("ZHANG/SAN"),),
        )
        retrieved = self.agent.retrieve_order(order.order_id)

        self.assertEqual(retrieved.order_id, order.order_id)
        self.assertEqual(retrieved.offer_id, priced.offer.offer_id)
        self.assertEqual(retrieved.ticket_status.value, "not_ticketed")

    def test_order_requires_priced_offer(self) -> None:
        offer = self.agent.search(SearchRequest("SHA", "FRA", self.future_date))[0]
        with self.assertRaises(ConflictError):
            self.agent.prepare_order(offer.offer_id)

    def test_confirmation_token_is_single_use(self) -> None:
        offer = self.agent.search(SearchRequest("SHA", "FRA", self.future_date))[0]
        self.agent.price_offer(offer.offer_id)
        pending = self.agent.prepare_order(offer.offer_id)
        passengers = (Passenger("ZHANG/SAN"),)
        self.agent.confirm_order(pending.token, passengers)

        with self.assertRaises(NotFoundError):
            self.agent.confirm_order(pending.token, passengers)

    def test_new_intent_invalidates_old_offer(self) -> None:
        old_offer = self.agent.search(SearchRequest("SHA", "FRA", self.future_date))[0]
        self.agent.search(SearchRequest("SHA", "LHR", self.future_date))

        with self.assertRaises(NotFoundError):
            self.agent.price_offer(old_offer.offer_id)

    def test_chinese_search_command(self) -> None:
        parsed = parse_command(f"查询 SHA 到 FRA {self.future_date.isoformat()} 2人")
        request = parse_search_args(parsed.args)
        self.assertEqual(parsed.name, "search")
        self.assertEqual(request.origin, "SHA")
        self.assertEqual(request.destination, "FRA")
        self.assertEqual(request.adults, 2)

    def test_chinese_search_normalizes_copied_unicode_characters(self) -> None:
        fullwidth_date = self.future_date.isoformat().translate(
            str.maketrans("0123456789-", "０１２３４５６７８９－")
        )
        inputs = (
            f"查询\u00a0SHA\u00a0到\u00a0FRA\u00a0{self.future_date.isoformat()}\u00a01人\u00a0",
            f"查询　ＳＨＡ 到 ＦＲＡ {fullwidth_date} １人",
            f"查询 SHA 到 FRA {self.future_date.isoformat()} 1人\ufeff",
        )

        for text in inputs:
            with self.subTest(text=repr(text)):
                request = parse_search_args(parse_command(text).args)
                self.assertEqual(request.origin, "SHA")
                self.assertEqual(request.destination, "FRA")
                self.assertEqual(request.departure_date, self.future_date)
                self.assertEqual(request.adults, 1)


if __name__ == "__main__":
    unittest.main()
