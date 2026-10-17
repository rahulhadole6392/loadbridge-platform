"""
LoadBridge Automated Test Suite
Covers 4 Matching Rules, Reverse-Route Rejection, Input Guards,
Idempotency Lock, Fit-Score Ranking, Cancellation Restoration, and API Endpoints.
"""

import pytest
from fastapi.testclient import TestClient
from app.matching_engine import (
    PlatformState,
    MatchingEngine,
    TruckListing,
    ShipmentRequest,
    CreateTruckRequest,
    CreateShipmentInput,
    _time_to_minutes,
)
from app.main import app


# ============================================================================
# Unit Tests: 4-Rule Matching Engine Core Logic
# ============================================================================

@pytest.fixture
def base_truck():
    return TruckListing(
        id="TEST-TRK-01",
        transporter_name="Sahyadri Express Logistics",
        vehicle_body="14ft Closed Container",
        route_stops=["Pune", "Pimpri", "Lonavala", "Panvel", "Mumbai"],
        date="2026-10-20",
        time_window_start="09:00",
        time_window_end="12:00",
        available_weight_kg=1000.0,
        total_weight_kg=2500.0,
        available_volume_m3=8.0,
        total_volume_m3=14.0,
        allowed_cargo=["Auto Parts", "Dry Cartons", "Textiles"],
        masked_phone="+91 98XXX-XX101",
        full_phone="+91 98230-44101",
        status="AVAILABLE",
    )


@pytest.fixture
def base_request():
    return ShipmentRequest(
        id="TEST-REQ-01",
        shipper_name="Maharashtra Industrial Goods",
        pickup_stop="Pimpri",
        drop_stop="Panvel",
        date="2026-10-20",
        time_window_start="09:30",
        time_window_end="11:30",
        weight_kg=500.0,
        volume_m3=3.0,
        cargo_type="Auto Parts",
        required_vehicle="Closed Container",
        status="OPEN",
    )


def test_rule_1_ordered_corridor_success(base_truck, base_request):
    """Pimpri (index 1) to Panvel (index 3) is a valid forward corridor hop."""
    match = MatchingEngine.evaluate_match(base_truck, base_request)
    assert match.evaluation.rule_1_route is True
    assert match.pickup_index == 1
    assert match.drop_index == 3
    assert "Ordered corridor stop match" in match.evaluation.rule_1_detail


def test_rule_1_reverse_route_rejection(base_truck):
    """Panvel (index 3) to Pimpri (index 1) is a reverse route and must fail Rule 1."""
    reverse_req = ShipmentRequest(
        id="REV-01",
        shipper_name="Reverse Freight Ltd",
        pickup_stop="Panvel",
        drop_stop="Pimpri",
        date="2026-10-20",
        time_window_start="09:30",
        time_window_end="11:30",
        weight_kg=400.0,
        volume_m3=2.0,
        cargo_type="Auto Parts",
        required_vehicle="Closed Container",
    )
    match = MatchingEngine.evaluate_match(base_truck, reverse_req)
    assert match.evaluation.rule_1_route is False
    assert match.evaluation.is_compatible is False
    assert match.fit_score == 0
    assert "Reverse corridor sequence rejected" in match.evaluation.rule_1_detail


def test_rule_1_missing_stop_rejection(base_truck):
    """Stops outside the corridor route must fail Rule 1."""
    off_corridor_req = ShipmentRequest(
        id="OFF-01",
        shipper_name="Nashik Agro",
        pickup_stop="Nashik",  # Not on Pune-Mumbai corridor
        drop_stop="Mumbai",
        date="2026-10-20",
        time_window_start="09:30",
        time_window_end="11:30",
        weight_kg=300.0,
        volume_m3=2.0,
        cargo_type="Auto Parts",
        required_vehicle="Closed Container",
    )
    match = MatchingEngine.evaluate_match(base_truck, off_corridor_req)
    assert match.evaluation.rule_1_route is False
    assert match.evaluation.is_compatible is False
    assert "Pickup stop 'Nashik' is not in truck corridor stops" in match.evaluation.rule_1_detail


def test_rule_2_time_window_overlap(base_truck, base_request):
    """Truck (09:00 - 12:00) overlaps Request (09:30 - 11:30) by 120 mins."""
    match = MatchingEngine.evaluate_match(base_truck, base_request)
    assert match.evaluation.rule_2_time is True
    assert match.score_breakdown.time_overlap_mins == 120
    assert match.overlap_window == "09:30 - 11:30"


def test_rule_2_disjoint_time_fails(base_truck, base_request):
    """Truck (09:00 - 12:00) does not overlap evening request (14:00 - 17:00)."""
    base_request.time_window_start = "14:00"
    base_request.time_window_end = "17:00"
    match = MatchingEngine.evaluate_match(base_truck, base_request)
    assert match.evaluation.rule_2_time is False
    assert match.evaluation.is_compatible is False
    assert match.fit_score == 0
    assert "No time overlap" in match.evaluation.rule_2_detail


def test_rule_2_date_mismatch_fails(base_truck, base_request):
    """Different operational dates must fail Rule 2."""
    base_request.date = "2026-10-25"
    match = MatchingEngine.evaluate_match(base_truck, base_request)
    assert match.evaluation.rule_2_time is False
    assert match.evaluation.is_compatible is False
    assert "Date mismatch" in match.evaluation.rule_2_detail


def test_rule_3_capacity_fit_and_deficit(base_truck, base_request):
    """Valid weight & volume fits; exceeding either triggers Rule 3 failure."""
    # Fitting scenario
    match = MatchingEngine.evaluate_match(base_truck, base_request)
    assert match.evaluation.rule_3_capacity is True

    # Weight deficit scenario (1200kg > available 1000kg)
    heavy_req = base_request.model_copy(update={"weight_kg": 1200.0})
    match_heavy = MatchingEngine.evaluate_match(base_truck, heavy_req)
    assert match_heavy.evaluation.rule_3_capacity is False
    assert match_heavy.evaluation.is_compatible is False
    assert "Weight deficit" in match_heavy.evaluation.rule_3_detail

    # Volume deficit scenario (9.5 m3 > available 8.0 m3)
    bulky_req = base_request.model_copy(update={"volume_m3": 9.5})
    match_bulky = MatchingEngine.evaluate_match(base_truck, bulky_req)
    assert match_bulky.evaluation.rule_3_capacity is False
    assert match_bulky.evaluation.is_compatible is False
    assert "Volume deficit" in match_bulky.evaluation.rule_3_detail


def test_rule_4_cargo_and_vehicle_suitability(base_truck, base_request):
    """Tests allowed cargo categories and vehicle body compatibility."""
    match = MatchingEngine.evaluate_match(base_truck, base_request)
    assert match.evaluation.rule_4_suitability is True

    # Disallowed cargo (Chemicals not in truck's allowed cargo)
    chem_req = base_request.model_copy(update={"cargo_type": "Hazardous Chemicals"})
    match_chem = MatchingEngine.evaluate_match(base_truck, chem_req)
    assert match_chem.evaluation.rule_4_suitability is False
    assert match_chem.evaluation.is_compatible is False
    assert "not in truck permitted list" in match_chem.evaluation.rule_4_detail

    # Incompatible vehicle body (Refrigerated required vs Closed Container)
    reefer_req = base_request.model_copy(update={"required_vehicle": "Refrigerated Reefer"})
    match_reefer = MatchingEngine.evaluate_match(base_truck, reefer_req)
    assert match_reefer.evaluation.rule_4_suitability is False
    assert match_reefer.evaluation.is_compatible is False


def test_fit_score_bounds_and_ranking():
    """Validates that fit scores range between 0 and 99 and favor tighter fill ratios."""
    state = PlatformState()
    matches = state.find_matches("REQ-01")
    assert len(matches) >= 2

    for m in matches:
        if m.evaluation.is_compatible:
            assert 50 <= m.fit_score <= 99
        else:
            assert m.fit_score == 0

    # Best match should be first in the sorted array
    assert matches[0].fit_score >= matches[1].fit_score


# ============================================================================
# Input Validation Guards
# ============================================================================

def test_reject_identical_pickup_and_drop():
    """Identical pickup and drop stops must be rejected immediately."""
    with pytest.raises(ValueError, match="cannot be identical"):
        ShipmentRequest(
            id="FAIL-01",
            shipper_name="Test Shipper",
            pickup_stop="Pune",
            drop_stop="Pune",
            date="2026-10-20",
            time_window_start="09:00",
            time_window_end="11:00",
            weight_kg=100.0,
            volume_m3=1.0,
            cargo_type="Auto Parts",
        )


def test_reject_non_positive_weight_and_volume():
    """Zero or negative weight and volume must raise validation errors."""
    with pytest.raises(ValueError, match="strictly greater than 0 kg"):
        ShipmentRequest(
            id="FAIL-02",
            shipper_name="Test Shipper",
            pickup_stop="Pune",
            drop_stop="Mumbai",
            date="2026-10-20",
            time_window_start="09:00",
            time_window_end="11:00",
            weight_kg=0.0,
            volume_m3=1.0,
            cargo_type="Auto Parts",
        )

    with pytest.raises(ValueError, match="strictly greater than 0 m³"):
        ShipmentRequest(
            id="FAIL-03",
            shipper_name="Test Shipper",
            pickup_stop="Pune",
            drop_stop="Mumbai",
            date="2026-10-20",
            time_window_start="09:00",
            time_window_end="11:00",
            weight_kg=100.0,
            volume_m3=-2.5,
            cargo_type="Auto Parts",
        )


def test_reject_invalid_time_window():
    """Start time >= end time must be rejected."""
    with pytest.raises(ValueError, match="must precede end time"):
        ShipmentRequest(
            id="FAIL-04",
            shipper_name="Test Shipper",
            pickup_stop="Pune",
            drop_stop="Mumbai",
            date="2026-10-20",
            time_window_start="14:00",
            time_window_end="10:00",
            weight_kg=100.0,
            volume_m3=1.0,
            cargo_type="Auto Parts",
        )


# ============================================================================
# Idempotent Booking & Cancellation Capacity Restoration
# ============================================================================

def test_booking_confirmation_and_idempotency():
    """Verifies atomic capacity deduction and idempotent re-confirmation."""
    state = PlatformState()
    initial_truck = state.get_truck("TRK-101")
    init_wt = initial_truck.available_weight_kg
    init_vol = initial_truck.available_volume_m3

    req = state.get_request("REQ-01")

    # First confirmation
    booking = state.confirm_booking("REQ-01", "TRK-101")
    assert booking.status == "CONFIRMED"
    assert booking.coordination_pass.unmasked_phone == initial_truck.full_phone
    assert booking.coordination_pass.driver_name is not None

    # Verify capacity deducted
    updated_truck = state.get_truck("TRK-101")
    assert updated_truck.available_weight_kg == pytest.approx(init_wt - req.weight_kg)
    assert updated_truck.available_volume_m3 == pytest.approx(init_vol - req.volume_m3)
    assert state.get_request("REQ-01").status == "BOOKED"

    # Idempotent confirmation: calling confirm again on same request & truck returns existing booking
    repeat_booking = state.confirm_booking("REQ-01", "TRK-101")
    assert repeat_booking.id == booking.id
    # Capacity must NOT be deducted twice
    assert updated_truck.available_weight_kg == pytest.approx(init_wt - req.weight_kg)

    # Attempting to double-book to another truck must be blocked
    with pytest.raises(ValueError, match="already booked"):
        state.confirm_booking("REQ-01", "TRK-108")


def test_pre_departure_cancellation_restores_capacity():
    """Canceling a booking must restore cargo weight and volume back to the truck."""
    state = PlatformState()
    truck_before = state.get_truck("TRK-108")
    before_wt = truck_before.available_weight_kg
    before_vol = truck_before.available_volume_m3

    req = state.get_request("REQ-01")

    booking = state.confirm_booking("REQ-01", "TRK-108")
    assert state.get_truck("TRK-108").available_weight_kg == pytest.approx(before_wt - req.weight_kg)

    # Cancel booking
    cancelled_booking = state.cancel_booking(booking.id)
    assert cancelled_booking.status == "CANCELLED"
    assert state.get_request("REQ-01").status == "OPEN"

    # Capacity should be fully restored
    restored_truck = state.get_truck("TRK-108")
    assert restored_truck.available_weight_kg == pytest.approx(before_wt)
    assert restored_truck.available_volume_m3 == pytest.approx(before_vol)


# ============================================================================
# FastAPI Integration Tests via TestClient
# ============================================================================

client = TestClient(app)


def test_api_trucks_list():
    res = client.get("/api/trucks")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    truck_ids = [t["id"] for t in data]
    assert "TRK-101" in truck_ids
    assert "TRK-108" in truck_ids


def test_api_requests_list():
    res = client.get("/api/requests")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 1
    assert data[0]["id"] == "REQ-01"


def test_api_matches_ranking():
    res = client.get("/api/matches/REQ-01")
    assert res.status_code == 200
    matches = res.json()
    assert len(matches) >= 2
    # Check 4 rules are evaluated
    top = matches[0]
    assert "evaluation" in top
    eval_obj = top["evaluation"]
    assert "rule_1_route" in eval_obj
    assert "rule_2_time" in eval_obj
    assert "rule_3_capacity" in eval_obj
    assert "rule_4_suitability" in eval_obj
    assert top["fit_score"] > 0
    assert top["fit_score"] <= 99


def test_api_publish_truck_and_create_shipment():
    # 1. Publish a new truck
    truck_payload = {
        "transporter_name": "Kalyani Freight Lines",
        "vehicle_body": "19ft Closed Container",
        "route_stops": ["Pune", "Pimpri", "Talegaon", "Panvel", "Thane"],
        "date": "2026-10-21",
        "time_window_start": "07:00",
        "time_window_end": "11:00",
        "available_weight_kg": 1500.0,
        "available_volume_m3": 10.0,
        "allowed_cargo": ["Auto Parts", "FMCG"],
        "phone": "+91 98220-11223"
    }
    t_res = client.post("/api/trucks", json=truck_payload)
    assert t_res.status_code == 201
    new_truck = t_res.json()
    assert new_truck["transporter_name"] == "Kalyani Freight Lines"
    assert new_truck["masked_phone"].startswith("+91 98XXX")

    # 2. Submit a new shipment request
    req_payload = {
        "shipper_name": "Talegaon Electronics",
        "pickup_stop": "Talegaon",
        "drop_stop": "Thane",
        "date": "2026-10-21",
        "time_window_start": "07:30",
        "time_window_end": "10:00",
        "weight_kg": 450.0,
        "volume_m3": 2.8,
        "cargo_type": "Auto Parts",
        "required_vehicle": "Closed Container"
    }
    r_res = client.post("/api/requests", json=req_payload)
    assert r_res.status_code == 201
    new_req = r_res.json()
    assert new_req["shipper_name"] == "Talegaon Electronics"

    # 3. Check match ranking for new request
    m_res = client.get(f"/api/matches/{new_req['id']}")
    assert m_res.status_code == 200
    matches = m_res.json()
    assert len(matches) > 0
    assert matches[0]["evaluation"]["is_compatible"] is True


def test_api_stats():
    res = client.get("/api/stats")
    assert res.status_code == 200
    stats = res.json()
    assert "active_trucks" in stats
    assert "total_available_weight_kg" in stats
    assert "corridor" in stats
