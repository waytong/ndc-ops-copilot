"""Run a complete tool bridge flow without an external LLM."""

from datetime import date, timedelta
import json

from ndc_ops_copilot import (
    AgentToolBridge,
    MockSupplierAdapter,
    MultiSupplierRouter,
    NdcAgent,
    SupplierRegistration,
    ToolCall,
)
from ndc_ops_copilot.serialization import to_primitive


router = MultiSupplierRouter(
    [
        SupplierRegistration(MockSupplierAdapter("TRAVELPORT"), priority=10),
        SupplierRegistration(MockSupplierAdapter("AIRLINE_NDC", "0.96"), priority=20),
    ]
)
bridge = AgentToolBridge(NdcAgent(router))
departure_date = (date.today() + timedelta(days=90)).isoformat()

search = bridge.execute(
    ToolCall(
        "1",
        "search_flights",
        {
            "origin": "SHA",
            "destination": "FRA",
            "departure_date": departure_date,
            "adults": 1,
        },
    )
)
offer_id = search.output[0]["offer_id"]
bridge.execute(ToolCall("2", "price_offer", {"offer_id": offer_id}))
prepared = bridge.execute(ToolCall("3", "prepare_order", {"offer_id": offer_id}))
token = prepared.output["token"]

confirm_call = ToolCall(
    "4",
    "confirm_order",
    {
        "confirmation_token": token,
        "passengers": [{"name": "ZHANG/SAN", "type_code": "ADT"}],
    },
)
blocked = bridge.execute(confirm_call)
bridge.approve(confirm_call)  # Trusted application code approves these exact arguments.
confirmed = bridge.execute(confirm_call)

print(
    json.dumps(
        {
            "search": search,
            "blocked_before_approval": blocked,
            "confirmed_after_approval": confirmed,
        },
        ensure_ascii=False,
        indent=2,
        default=to_primitive,
    )
)
