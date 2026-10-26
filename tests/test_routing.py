from __future__ import annotations

from datetime import date, timedelta
import unittest

from ndc_ops_copilot import NdcAgent, Passenger, SearchRequest
from ndc_ops_copilot.models import SupplierUnavailableError
from ndc_ops_copilot.providers.mock import MockSupplierAdapter
from ndc_ops_copilot.providers.routing import MultiSupplierRouter, SupplierRegistration


class FailingSupplier(MockSupplierAdapter):
    def search(self, request):
        raise RuntimeError("supplier timeout")


class MultiSupplierRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.future_date = date.today() + timedelta(days=90)
        self.request = SearchRequest("SHA", "FRA", self.future_date, 1)

    def test_search_aggregates_and_sorts_multiple_suppliers(self) -> None:
        router = MultiSupplierRouter(
            [
                SupplierRegistration(MockSupplierAdapter("GDS", "1.00"), priority=10),
                SupplierRegistration(MockSupplierAdapter("NDC", "0.95"), priority=20),
            ]
        )

        offers = router.search(self.request)

        self.assertEqual(len(offers), 6)
        self.assertEqual({offer.owner for offer in offers}, {"GDS", "NDC"})
        self.assertTrue(all("::" in offer.offer_id for offer in offers))
        self.assertEqual(
            [offer.total_amount for offer in offers],
            sorted(offer.total_amount for offer in offers),
        )

    def test_price_and_order_keep_supplier_affinity(self) -> None:
        router = MultiSupplierRouter(
            [
                SupplierRegistration(MockSupplierAdapter("GDS"), priority=10),
                SupplierRegistration(MockSupplierAdapter("NDC", "0.95"), priority=20),
            ]
        )
        agent = NdcAgent(router)
        offer = next(item for item in agent.search(self.request) if item.owner == "NDC")

        priced = agent.price_offer(offer.offer_id)
        pending = agent.prepare_order(offer.offer_id)
        order = agent.confirm_order(pending.token, (Passenger("ZHANG/SAN"),))
        retrieved = agent.retrieve_order(order.order_id)

        self.assertEqual(priced.offer.owner, "NDC")
        self.assertEqual(order.owner, "NDC")
        self.assertTrue(order.order_id.startswith("NDC::"))
        self.assertEqual(retrieved.order_id, order.order_id)

    def test_route_rules_limit_eligible_suppliers(self) -> None:
        router = MultiSupplierRouter(
            [
                SupplierRegistration(
                    MockSupplierAdapter("EUROPE"),
                    routes=frozenset({("SHA", "FRA")}),
                ),
                SupplierRegistration(
                    MockSupplierAdapter("AMERICA"),
                    routes=frozenset({("SHA", "LAX")}),
                ),
            ]
        )

        offers = router.search(self.request)

        self.assertEqual(len(offers), 3)
        self.assertEqual({offer.owner for offer in offers}, {"EUROPE"})

    def test_search_continues_when_one_supplier_fails(self) -> None:
        router = MultiSupplierRouter(
            [
                SupplierRegistration(FailingSupplier("BROKEN"), priority=10),
                SupplierRegistration(MockSupplierAdapter("HEALTHY"), priority=20),
            ]
        )

        offers = router.search(self.request)

        self.assertEqual(len(offers), 3)
        self.assertEqual({offer.owner for offer in offers}, {"HEALTHY"})
        statuses = {item.supplier: item.status for item in router.last_diagnostics}
        self.assertEqual(statuses, {"BROKEN": "failed", "HEALTHY": "success"})

    def test_all_supplier_failures_raise_clear_error(self) -> None:
        router = MultiSupplierRouter(
            [SupplierRegistration(FailingSupplier("BROKEN"))]
        )

        with self.assertRaises(SupplierUnavailableError):
            router.search(self.request)

    def test_circuit_breaker_temporarily_skips_failed_supplier(self) -> None:
        router = MultiSupplierRouter(
            [
                SupplierRegistration(FailingSupplier("BROKEN"), priority=10),
                SupplierRegistration(MockSupplierAdapter("HEALTHY"), priority=20),
            ],
            failure_threshold=1,
            circuit_break_seconds=60,
        )

        router.search(self.request)
        router.search(self.request)

        self.assertEqual(
            [item.supplier for item in router.last_diagnostics],
            ["HEALTHY"],
        )
        broken = next(item for item in router.status() if item["supplier"] == "BROKEN")
        self.assertIsNotNone(broken["circuit_open_until"])


if __name__ == "__main__":
    unittest.main()
