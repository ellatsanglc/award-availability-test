from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Literal
from datetime import date, timedelta


CabinClass = Literal["economy", "premium_economy", "business", "first"]

CABIN_LABELS = {
    "economy": "Economy",
    "premium_economy": "Premium Economy",
    "business": "Business",
    "first": "First",
}


class SearchRequest(BaseModel):
    origin: str = "HKG"
    destinations: List[str] = Field(..., min_length=1)
    trip_type: Literal["one_way", "return"] = "return"
    cabin_classes: List[CabinClass] = ["economy"]
    departure_date_start: date
    departure_date_end: date
    min_nights: Optional[int] = None  # return trips: minimum nights away
    max_nights: Optional[int] = None  # return trips: maximum nights away

    def total_searches(self) -> int:
        return len(self.get_combinations())

    def get_combinations(self) -> List[Dict]:
        """Generate all (destination, departure_date, return_date, cabin) combos"""
        combos = []
        current = self.departure_date_start
        while current <= self.departure_date_end:
            for dest in self.destinations:
                for cabin in self.cabin_classes:
                    if self.trip_type == "return":
                        min_n = self.min_nights or 1
                        max_n = self.max_nights or 7
                        for nights in range(min_n, max_n + 1):
                            combos.append({
                                "destination": dest,
                                "departure_date": current,
                                "return_date": current + timedelta(days=nights),
                                "cabin_class": cabin,
                            })
                    else:
                        combos.append({
                            "destination": dest,
                            "departure_date": current,
                            "return_date": None,
                            "cabin_class": cabin,
                        })
            current += timedelta(days=1)
        return combos


class FlightOption(BaseModel):
    flight_numbers: Optional[List[str]] = None
    departure_time: Optional[str] = None
    arrival_time: Optional[str] = None
    duration: Optional[str] = None
    stops: Optional[str] = None
    miles: Optional[int] = None
    taxes: Optional[str] = None
    available: Optional[bool] = None


class FlightResult(BaseModel):
    origin: str
    destination: str
    departure_date: str
    return_date: Optional[str] = None
    cabin_class: str
    available: bool = False
    outbound: Optional[FlightOption] = None
    inbound: Optional[FlightOption] = None
    total_miles: Optional[int] = None
    error: Optional[str] = None
    flights: Optional[List[FlightOption]] = None          # outbound options
    inbound_flights: Optional[List[FlightOption]] = None  # return-leg options
    note: Optional[str] = None                            # e.g. dates adjusted due to side-by-side layout
