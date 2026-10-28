"""
LoadBridge — Matching Engine & In-Memory State Manager
Core business logic for local truck-load & spare-capacity matching platform.
"""

from __future__ import annotations
from typing import List, Dict, Optional, Any, Tuple
from pydantic import BaseModel, Field, field_validator, model_validator
import uuid
import threading
import math
from datetime import datetime


def _time_to_minutes(time_str: str) -> int:
    """Converts HH:MM string to minutes from midnight."""
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: '{time_str}'. Expected HH:MM")
    hours, minutes = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"Time out of bounds: '{time_str}'")
    return hours * 60 + minutes


def _minutes_to_time(minutes: int) -> str:
    """Converts minutes from midnight to HH:MM format."""
    hours = (minutes // 60) % 24
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"


# ============================================================================
# Corridor Geocoding & Waypoints along Pune — Mumbai Highway (NH-48)
# ============================================================================

CORRIDOR_WAYPOINTS: Dict[str, List[float]] = {
    "pune": [18.5204, 73.8567],
    "pimpri": [18.6298, 73.7997],
    "talegaon": [18.7303, 73.6841],
    "lonavala": [18.7557, 73.4091],
    "panvel": [18.9894, 73.1175],
    "vashi": [19.0771, 72.9986],
    "mumbai": [18.9515, 72.9499],
    # Additional corridor industrial gateways
    "chakan": [18.7606, 73.8580],
    "bhosari": [18.6279, 73.8443],
    "thane": [19.2183, 72.9781],
}


def haversine_distance(coord1: Tuple[float, float], coord2: Tuple[float, float]) -> float:
    """Computes great-circle distance between two (lat, lon) coordinates in kilometers."""
    R = 6371.0  # Earth radius in kilometers
    lat1, lon1 = math.radians(coord1[0]), math.radians(coord1[1])
    lat2, lon2 = math.radians(coord2[0]), math.radians(coord2[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2.0)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0)**2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


def get_corridor_coordinate(stop_name: str) -> List[float]:
    """Resolves GPS coordinates for a corridor stop with normalized lookup."""
    key = stop_name.strip().lower()
    if key in CORRIDOR_WAYPOINTS:
        return CORRIDOR_WAYPOINTS[key]
    for k, v in CORRIDOR_WAYPOINTS.items():
        if k in key or key in k:
            return v
    # Fallback to Pune coordinates if unknown
    return [18.5204, 73.8567]



# ============================================================================
# Pydantic Data Models
# ============================================================================

class TruckListing(BaseModel):
    id: str = Field(..., description="Unique truck identifier, e.g. TRK-101")
    transporter_name: str = Field(..., description="Name of logistics operator or owner")
    vehicle_body: str = Field(..., description="E.g. 14ft Closed Container, 17ft Closed Container")
    route_stops: List[str] = Field(..., description="Ordered route corridor stops")
    date: str = Field(..., description="Service date in YYYY-MM-DD")
    time_window_start: str = Field(..., description="Start time in HH:MM")
    time_window_end: str = Field(..., description="End time in HH:MM")
    available_weight_kg: float = Field(..., description="Remaining available cargo weight in kg")
    total_weight_kg: float = Field(..., description="Total vehicle payload capacity in kg")
    available_volume_m3: float = Field(..., description="Remaining available cargo volume in m3")
    total_volume_m3: float = Field(..., description="Total vehicle volumetric capacity in m3")
    allowed_cargo: List[str] = Field(..., description="List of accepted cargo categories")
    masked_phone: str = Field(..., description="Public privacy-protected phone number")
    full_phone: str = Field(..., description="Direct contact revealed upon booking confirmation")
    status: str = Field(default="AVAILABLE", description="AVAILABLE, PARTIALLY_BOOKED, FULL")

    @field_validator("route_stops")
    @classmethod
    def validate_route_stops(cls, v: List[str]) -> List[str]:
        cleaned = [s.strip() for s in v if s and s.strip()]
        if len(cleaned) < 2:
            raise ValueError("A truck route must contain at least 2 distinct stops (origin and destination).")
        return cleaned

    @field_validator("available_weight_kg", "total_weight_kg")
    @classmethod
    def validate_weight(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Weight capacity cannot be negative.")
        return round(float(v), 2)

    @field_validator("available_volume_m3", "total_volume_m3")
    @classmethod
    def validate_volume(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Volume capacity cannot be negative.")
        return round(float(v), 2)

    @model_validator(mode="after")
    def validate_truck_logic(self) -> TruckListing:
        start_min = _time_to_minutes(self.time_window_start)
        end_min = _time_to_minutes(self.time_window_end)
        if start_min >= end_min:
            raise ValueError(f"Truck time window start ({self.time_window_start}) must precede end time ({self.time_window_end}).")
        if self.available_weight_kg > self.total_weight_kg:
            raise ValueError("Available weight cannot exceed total weight capacity.")
        if self.available_volume_m3 > self.total_volume_m3:
            raise ValueError("Available volume cannot exceed total volume capacity.")
        return self


class CreateTruckRequest(BaseModel):
    transporter_name: str
    vehicle_body: str
    route_stops: List[str]
    date: str
    time_window_start: str
    time_window_end: str
    available_weight_kg: float
    total_weight_kg: Optional[float] = None
    available_volume_m3: float
    total_volume_m3: Optional[float] = None
    allowed_cargo: List[str]
    phone: str

    @model_validator(mode="after")
    def validate_create_truck(self) -> CreateTruckRequest:
        if not self.total_weight_kg:
            self.total_weight_kg = self.available_weight_kg * 1.5
        if not self.total_volume_m3:
            self.total_volume_m3 = self.available_volume_m3 * 1.5
        if self.available_weight_kg <= 0:
            raise ValueError("Available weight must be strictly greater than 0 kg.")
        if self.available_volume_m3 <= 0:
            raise ValueError("Available volume must be strictly greater than 0 m³.")
        start_min = _time_to_minutes(self.time_window_start)
        end_min = _time_to_minutes(self.time_window_end)
        if start_min >= end_min:
            raise ValueError("Time window start must be earlier than end time.")
        cleaned_stops = [s.strip() for s in self.route_stops if s and s.strip()]
        if len(cleaned_stops) < 2:
            raise ValueError("Route must include at least two stops.")
        return self


class ShipmentRequest(BaseModel):
    id: str = Field(..., description="Unique shipment request ID, e.g. REQ-01")
    shipper_name: str = Field(..., description="Shipper or manufacturing company name")
    pickup_stop: str = Field(..., description="Corridor pickup hub/city")
    drop_stop: str = Field(..., description="Corridor drop hub/city")
    date: str = Field(..., description="Required shipment date in YYYY-MM-DD")
    time_window_start: str = Field(..., description="Earliest readiness time in HH:MM")
    time_window_end: str = Field(..., description="Latest delivery/readiness window in HH:MM")
    weight_kg: float = Field(..., description="Cargo weight in kilograms (> 0)")
    volume_m3: float = Field(..., description="Cargo volume in cubic meters (> 0)")
    cargo_type: str = Field(..., description="Category: Auto Parts, Dry Cartons, Textiles, etc.")
    required_vehicle: str = Field(default="Closed Container", description="Closed Container, Open Body, Any")
    status: str = Field(default="OPEN", description="OPEN, BOOKED, CANCELLED")

    @field_validator("weight_kg")
    @classmethod
    def validate_weight_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Shipment weight must be strictly greater than 0 kg.")
        return round(float(v), 2)

    @field_validator("volume_m3")
    @classmethod
    def validate_volume_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Shipment volume must be strictly greater than 0 m³.")
        return round(float(v), 2)

    @model_validator(mode="after")
    def validate_shipment_logic(self) -> ShipmentRequest:
        p = self.pickup_stop.strip().lower()
        d = self.drop_stop.strip().lower()
        if not p or not d:
            raise ValueError("Both pickup and drop stops must be specified.")
        if p == d:
            raise ValueError("Pickup stop and Drop stop cannot be identical. A shipment requires distinct corridor endpoints.")
        start_min = _time_to_minutes(self.time_window_start)
        end_min = _time_to_minutes(self.time_window_end)
        if start_min >= end_min:
            raise ValueError(f"Pickup window start ({self.time_window_start}) must precede end time ({self.time_window_end}).")
        return self


class CreateShipmentInput(BaseModel):
    shipper_name: str
    pickup_stop: str
    drop_stop: str
    date: str
    time_window_start: str
    time_window_end: str
    weight_kg: float
    volume_m3: float
    cargo_type: str
    required_vehicle: Optional[str] = "Closed Container"

    @model_validator(mode="after")
    def validate_input(self) -> CreateShipmentInput:
        if not self.shipper_name.strip():
            raise ValueError("Shipper name is required.")
        p = self.pickup_stop.strip().lower()
        d = self.drop_stop.strip().lower()
        if p == d:
            raise ValueError("Pickup stop and Drop stop cannot be identical.")
        if self.weight_kg <= 0:
            raise ValueError("Shipment weight must be strictly greater than 0 kg.")
        if self.volume_m3 <= 0:
            raise ValueError("Shipment volume must be strictly greater than 0 m³.")
        start_min = _time_to_minutes(self.time_window_start)
        end_min = _time_to_minutes(self.time_window_end)
        if start_min >= end_min:
            raise ValueError("Pickup window start must precede end time.")
        return self


class RuleEvaluation(BaseModel):
    rule_1_route: bool = Field(..., description="Ordered Corridor Stop Check")
    rule_1_detail: str
    rule_2_time: bool = Field(..., description="Date & Time Window Overlap")
    rule_2_detail: str
    rule_3_capacity: bool = Field(..., description="Dual Capacity Fit (Weight & Volume)")
    rule_3_detail: str
    rule_4_suitability: bool = Field(..., description="Vehicle Body & Cargo Suitability")
    rule_4_detail: str
    is_compatible: bool = Field(..., description="True if and only if all 4 rules pass")


class ScoreBreakdown(BaseModel):
    weight_fill_pct: float
    volume_fill_pct: float
    time_overlap_mins: int
    directness_score: float
    total_fit_score: int


class MatchResult(BaseModel):
    truck: TruckListing
    request: ShipmentRequest
    evaluation: RuleEvaluation
    fit_score: int = Field(..., ge=0, le=99, description="Overall matching compatibility score (0-99%)")
    score_breakdown: ScoreBreakdown
    pickup_index: Optional[int] = None
    drop_index: Optional[int] = None
    overlap_window: Optional[str] = None


class CapacitySnapshot(BaseModel):
    weight_kg: float
    volume_m3: float


class CoordinationPass(BaseModel):
    pass_id: str
    booking_id: str
    truck_id: str
    transporter_name: str
    unmasked_phone: str
    driver_name: str
    vehicle_body: str
    shipper_name: str
    pickup_stop: str
    drop_stop: str
    date: str
    scheduled_window: str
    weight_kg: float
    volume_m3: float
    cargo_type: str
    before_capacity: CapacitySnapshot
    after_capacity: CapacitySnapshot
    issued_at: str
    verification_code: str


class Booking(BaseModel):
    id: str
    request_id: str
    truck_id: str
    status: str = Field(default="CONFIRMED", description="CONFIRMED, CANCELLED")
    coordination_pass: CoordinationPass
    created_at: str


class BookingConfirmRequest(BaseModel):
    request_id: str
    truck_id: str


class RouteWaypoint(BaseModel):
    stop_name: str
    lat: float
    lng: float
    index: int


class TruckTelemetry(BaseModel):
    truck_id: str
    status: str = Field(..., description="'Docked at Origin', 'In Transit', or 'Delivered'")
    progress_ratio: float = Field(..., ge=0.0, le=1.0)
    progress_pct: float = Field(..., ge=0.0, le=100.0)
    current_lat: float
    current_lng: float
    current_location: str
    speed_kmh: float
    next_stop: str
    distance_covered_km: float
    total_distance_km: float
    remaining_distance_km: float
    eta: str
    scheduled_window: str
    corridor_waypoints: List[RouteWaypoint]
    timestamp: str


TelemetryResponse = TruckTelemetry


def calculate_truck_telemetry(
    truck: TruckListing,
    simulated_time: Optional[str] = None,
    progress: Optional[float] = None,
) -> TruckTelemetry:
    """
    Computes live/simulated telemetry based on the truck's scheduled time window:
    - Calculates progress ratio along ordered stops polyline.
    - Computes interpolated current latitude and longitude.
    - Returns current estimated speed (km/h), next milestone stop, distance covered, and ETA.
    - Status:
        * 'Docked at Origin' if before departure (progress == 0.0)
        * 'In Transit' if transit active (0.0 < progress < 1.0)
        * 'Delivered' if after completion (progress >= 1.0)
    """
    # 1. Resolve waypoints and coordinates along the ordered stops
    waypoints: List[RouteWaypoint] = []
    coords: List[Tuple[float, float]] = []
    for idx, stop in enumerate(truck.route_stops):
        c = get_corridor_coordinate(stop)
        coords.append((c[0], c[1]))
        waypoints.append(RouteWaypoint(stop_name=stop, lat=c[0], lng=c[1], index=idx))

    # 2. Compute segment distances and total route distance
    segment_distances = [
        haversine_distance(coords[i], coords[i + 1])
        for i in range(len(coords) - 1)
    ]
    total_dist = sum(segment_distances)

    # 3. Time window metrics
    start_min = _time_to_minutes(truck.time_window_start)
    end_min = _time_to_minutes(truck.time_window_end)
    window_mins = max(1, end_min - start_min)

    # 4. Progress ratio calculation
    if progress is not None:
        prog_ratio = max(0.0, min(1.0, float(progress)))
    elif simulated_time is not None:
        sim_min = _time_to_minutes(simulated_time)
        if sim_min <= start_min:
            prog_ratio = 0.0
        elif sim_min >= end_min:
            prog_ratio = 1.0
        else:
            prog_ratio = (sim_min - start_min) / float(window_mins)
    else:
        now = datetime.now()
        now_min = now.hour * 60 + now.minute + (now.second / 60.0)
        if now_min <= start_min:
            prog_ratio = 0.0
        elif now_min >= end_min:
            prog_ratio = 1.0
        else:
            prog_ratio = (now_min - start_min) / float(window_mins)

    prog_ratio = max(0.0, min(1.0, prog_ratio))
    prog_pct = round(prog_ratio * 100.0, 1)

    # 5. Boundary & Interpolation Logic
    if prog_ratio == 0.0:
        status = "Docked at Origin"
        current_lat = coords[0][0]
        current_lng = coords[0][1]
        distance_covered = 0.0
        speed_kmh = 0.0
        next_stop = truck.route_stops[1] if len(truck.route_stops) > 1 else truck.route_stops[0]
        current_location = f"Docked at {truck.route_stops[0]} (Origin)"
        eta = truck.time_window_end
    elif prog_ratio >= 1.0:
        status = "Delivered"
        current_lat = coords[-1][0]
        current_lng = coords[-1][1]
        distance_covered = round(total_dist, 2)
        speed_kmh = 0.0
        next_stop = f"Destination Reached ({truck.route_stops[-1]})"
        current_location = f"Delivered at {truck.route_stops[-1]}"
        eta = "Delivered"
    else:
        status = "In Transit"
        target_dist = prog_ratio * total_dist
        distance_covered = round(target_dist, 2)

        # Realistic estimated speed on NH-48 highway corridor (nominal ~ 45-65 km/h)
        window_hours = window_mins / 60.0
        nominal_speed = total_dist / window_hours if window_hours > 0 else 55.0
        speed_kmh = round(max(42.0, min(70.0, nominal_speed)), 1)

        # Polyline interpolation across segments
        accum = 0.0
        current_lat, current_lng = coords[0]
        next_stop = truck.route_stops[1] if len(truck.route_stops) > 1 else truck.route_stops[0]
        current_location = f"En route to {next_stop}"

        for i in range(len(coords) - 1):
            seg_d = segment_distances[i]
            if accum + seg_d >= target_dist or i == len(coords) - 2:
                seg_frac = (target_dist - accum) / seg_d if seg_d > 0 else 0.0
                seg_frac = max(0.0, min(1.0, seg_frac))
                lat = coords[i][0] + seg_frac * (coords[i + 1][0] - coords[i][0])
                lng = coords[i][1] + seg_frac * (coords[i + 1][1] - coords[i][1])
                current_lat = round(lat, 5)
                current_lng = round(lng, 5)
                next_stop = truck.route_stops[i + 1]
                from_stop = truck.route_stops[i]
                pct_seg = int(seg_frac * 100)
                current_location = f"Between {from_stop} and {next_stop} ({pct_seg}%)"
                break
            accum += seg_d

        eta = truck.time_window_end

    remaining_distance = max(0.0, round(total_dist - distance_covered, 2))

    return TruckTelemetry(
        truck_id=truck.id,
        status=status,
        progress_ratio=round(prog_ratio, 4),
        progress_pct=prog_pct,
        current_lat=current_lat,
        current_lng=current_lng,
        current_location=current_location,
        speed_kmh=speed_kmh,
        next_stop=next_stop,
        distance_covered_km=distance_covered,
        total_distance_km=round(total_dist, 2),
        remaining_distance_km=remaining_distance,
        eta=eta,
        scheduled_window=f"{truck.time_window_start} - {truck.time_window_end}",
        corridor_waypoints=waypoints,
        timestamp=datetime.now().isoformat(),
    )



# ============================================================================
# 4-Rule Matching Engine
# ============================================================================

class MatchingEngine:
    """
    Evaluates compatibility between a ShipmentRequest and a TruckListing using
    four rigorous operational rules and produces a calibrated 0-99% Fit Score.
    """

    @staticmethod
    def evaluate_match(truck: TruckListing, req: ShipmentRequest) -> MatchResult:
        # --------------------------------------------------------------------
        # Rule 1: Ordered Corridor Stop Check
        # Pickup and Drop stops must exist in truck's route, and pickup must precede drop.
        # --------------------------------------------------------------------
        norm_truck_stops = [s.strip().lower() for s in truck.route_stops]
        norm_pickup = req.pickup_stop.strip().lower()
        norm_drop = req.drop_stop.strip().lower()

        p_idx = -1
        d_idx = -1
        try:
            p_idx = norm_truck_stops.index(norm_pickup)
        except ValueError:
            p_idx = -1

        try:
            d_idx = norm_truck_stops.index(norm_drop)
        except ValueError:
            d_idx = -1

        rule_1_pass = False
        if p_idx == -1 and d_idx == -1:
            rule_1_detail = f"Neither pickup '{req.pickup_stop}' nor drop '{req.drop_stop}' exist on truck corridor ({' -> '.join(truck.route_stops)})."
        elif p_idx == -1:
            rule_1_detail = f"Pickup stop '{req.pickup_stop}' is not in truck corridor stops."
        elif d_idx == -1:
            rule_1_detail = f"Drop stop '{req.drop_stop}' is not in truck corridor stops."
        elif p_idx >= d_idx:
            rule_1_detail = (
                f"Reverse corridor sequence rejected: '{req.pickup_stop}' (stop #{p_idx+1}) "
                f"is scheduled after or at '{req.drop_stop}' (stop #{d_idx+1}) in route: {' -> '.join(truck.route_stops)}."
            )
        else:
            rule_1_pass = True
            rule_1_detail = (
                f"Ordered corridor stop match: Stop #{p_idx+1} ({truck.route_stops[p_idx]}) -> "
                f"Stop #{d_idx+1} ({truck.route_stops[d_idx]}) along corridor."
            )

        # --------------------------------------------------------------------
        # Rule 2: Date & Time Window Overlap
        # Date must match; time intervals must have an intersection > 0 mins.
        # --------------------------------------------------------------------
        rule_2_pass = False
        overlap_mins = 0
        overlap_window_str = None

        if truck.date != req.date:
            rule_2_detail = f"Date mismatch: Truck runs on {truck.date}, but shipment requested for {req.date}."
        else:
            t_start = _time_to_minutes(truck.time_window_start)
            t_end = _time_to_minutes(truck.time_window_end)
            r_start = _time_to_minutes(req.time_window_start)
            r_end = _time_to_minutes(req.time_window_end)

            ov_start = max(t_start, r_start)
            ov_end = min(t_end, r_end)

            if ov_start < ov_end:
                rule_2_pass = True
                overlap_mins = ov_end - ov_start
                overlap_window_str = f"{_minutes_to_time(ov_start)} - {_minutes_to_time(ov_end)}"
                rule_2_detail = (
                    f"Operational overlap of {overlap_mins} mins: Truck [{truck.time_window_start}-{truck.time_window_end}] "
                    f"intersects Request [{req.time_window_start}-{req.time_window_end}] at [{overlap_window_str}]."
                )
            else:
                rule_2_detail = (
                    f"No time overlap: Truck [{truck.time_window_start}-{truck.time_window_end}] "
                    f"does not intersect Request [{req.time_window_start}-{req.time_window_end}]."
                )

        # --------------------------------------------------------------------
        # Rule 3: Dual Capacity Fit
        # req.weight <= truck.available_weight AND req.volume <= truck.available_volume
        # --------------------------------------------------------------------
        weight_fits = req.weight_kg <= truck.available_weight_kg
        volume_fits = req.volume_m3 <= truck.available_volume_m3
        rule_3_pass = weight_fits and volume_fits

        if rule_3_pass:
            rule_3_detail = (
                f"Capacity verified: Cargo ({req.weight_kg} kg / {req.volume_m3} m³) fits within "
                f"available spare capacity ({truck.available_weight_kg} kg / {truck.available_volume_m3} m³)."
            )
        else:
            deficits = []
            if not weight_fits:
                deficits.append(f"Weight deficit of {round(req.weight_kg - truck.available_weight_kg, 1)} kg")
            if not volume_fits:
                deficits.append(f"Volume deficit of {round(req.volume_m3 - truck.available_volume_m3, 2)} m³")
            rule_3_detail = f"Capacity exceeded: {', '.join(deficits)} (Truck has {truck.available_weight_kg} kg / {truck.available_volume_m3} m³ left)."

        # --------------------------------------------------------------------
        # Rule 4: Vehicle Body & Cargo Suitability
        # Cargo must be permitted, and vehicle type must satisfy requirements.
        # --------------------------------------------------------------------
        cargo_matched = any(
            req.cargo_type.strip().lower() == allowed.strip().lower()
            for allowed in truck.allowed_cargo
        )
        
        # Vehicle body check: req body type should be compatible with truck body
        req_veh = req.required_vehicle.strip().lower()
        truck_veh = truck.vehicle_body.strip().lower()
        
        if req_veh in ["any", "any vehicle", "standard", ""]:
            vehicle_matched = True
        else:
            vehicle_matched = (req_veh in truck_veh) or (truck_veh in req_veh)

        rule_4_pass = cargo_matched and vehicle_matched

        if rule_4_pass:
            rule_4_detail = (
                f"Suitability verified: Cargo category '{req.cargo_type}' is permitted on '{truck.vehicle_body}'."
            )
        else:
            reasons = []
            if not cargo_matched:
                reasons.append(f"Cargo '{req.cargo_type}' not in truck permitted list: [{', '.join(truck.allowed_cargo)}]")
            if not vehicle_matched:
                reasons.append(f"Vehicle '{truck.vehicle_body}' does not satisfy requested '{req.required_vehicle}'")
            rule_4_detail = f"Suitability mismatch: {'; '.join(reasons)}."

        # --------------------------------------------------------------------
        # Overall Compatibility & Calibrated Fit Score (0 - 99%)
        # --------------------------------------------------------------------
        is_compatible = rule_1_pass and rule_2_pass and rule_3_pass and rule_4_pass

        # Calculate metrics for scoring
        weight_fill_pct = round((req.weight_kg / max(truck.available_weight_kg, 1.0)) * 100, 1)
        volume_fill_pct = round((req.volume_m3 / max(truck.available_volume_m3, 0.01)) * 100, 1)

        directness_factor = 1.0
        if rule_1_pass and len(truck.route_stops) > 1:
            segment_distance = d_idx - p_idx
            total_corridor = len(truck.route_stops) - 1
            # Proximity factor: direct hops get slightly higher priority
            directness_factor = max(0.5, 1.0 - (0.2 * (segment_distance / total_corridor)))

        if not is_compatible:
            fit_score = 0
        else:
            # Calibrated scoring model:
            # Base = 45 points for satisfying all 4 hard operational constraints
            # Weight capacity efficiency (up to 24 pts)
            w_score = min(24.0, (req.weight_kg / max(truck.available_weight_kg, 1.0)) * 24.0)
            # Volume capacity efficiency (up to 20 pts)
            v_score = min(20.0, (req.volume_m3 / max(truck.available_volume_m3, 0.01)) * 20.0)
            # Time alignment overlap factor (up to 6 pts)
            req_duration = max(1.0, float(_time_to_minutes(req.time_window_end) - _time_to_minutes(req.time_window_start)))
            t_score = min(6.0, (overlap_mins / req_duration) * 6.0)
            # Route directness (up to 4 pts)
            r_score = min(4.0, directness_factor * 4.0)

            raw_score = 45.0 + w_score + v_score + t_score + r_score
            # Max score strictly capped at 99%
            fit_score = int(min(99, max(50, round(raw_score))))

        score_breakdown = ScoreBreakdown(
            weight_fill_pct=min(100.0, weight_fill_pct),
            volume_fill_pct=min(100.0, volume_fill_pct),
            time_overlap_mins=overlap_mins,
            directness_score=round(directness_factor * 100, 1),
            total_fit_score=fit_score,
        )

        evaluation = RuleEvaluation(
            rule_1_route=rule_1_pass,
            rule_1_detail=rule_1_detail,
            rule_2_time=rule_2_pass,
            rule_2_detail=rule_2_detail,
            rule_3_capacity=rule_3_pass,
            rule_3_detail=rule_3_detail,
            rule_4_suitability=rule_4_pass,
            rule_4_detail=rule_4_detail,
            is_compatible=is_compatible,
        )

        return MatchResult(
            truck=truck,
            request=req,
            evaluation=evaluation,
            fit_score=fit_score,
            score_breakdown=score_breakdown,
            pickup_index=p_idx if p_idx >= 0 else None,
            drop_index=d_idx if d_idx >= 0 else None,
            overlap_window=overlap_window_str,
        )


# ============================================================================
# Platform State Store (In-Memory with Pre-Seeding and Thread-Safety)
# ============================================================================

class PlatformState:
    """
    Central thread-safe repository managing trucks, shipment requests, and bookings.
    Pre-seeded with realistic Pune-Mumbai industrial corridor listings.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.trucks: Dict[str, TruckListing] = {}
        self.requests: Dict[str, ShipmentRequest] = {}
        self.bookings: Dict[str, Booking] = {}
        self._seed_initial_corridor_data()

    def _seed_initial_corridor_data(self):
        """Pre-seeds Pune-Mumbai highway industrial corridor data."""
        trk_101 = TruckListing(
            id="TRK-101",
            transporter_name="Shinde Transport",
            vehicle_body="14ft Closed Container",
            route_stops=["Pune", "Pimpri", "Lonavala", "Panvel", "Mumbai"],
            date="2026-10-20",
            time_window_start="09:00",
            time_window_end="11:00",
            available_weight_kg=1200.0,
            total_weight_kg=2500.0,
            available_volume_m3=8.0,
            total_volume_m3=14.0,
            allowed_cargo=["Auto Parts", "Dry Cartons", "Textiles"],
            masked_phone="+91 98XXX-XX101",
            full_phone="+91 98230-44101",
            status="AVAILABLE",
        )

        trk_108 = TruckListing(
            id="TRK-108",
            transporter_name="Deshmukh Logistics",
            vehicle_body="17ft Closed Container",
            route_stops=["Chakan", "Pimpri", "Panvel", "Vashi"],
            date="2026-10-20",
            time_window_start="10:00",
            time_window_end="12:00",
            available_weight_kg=750.0,
            total_weight_kg=3500.0,
            available_volume_m3=5.0,
            total_volume_m3=18.0,
            allowed_cargo=["Auto Parts", "Dry Cartons"],
            masked_phone="+91 98XXX-XX108",
            full_phone="+91 98230-77108",
            status="AVAILABLE",
        )

        req_01 = ShipmentRequest(
            id="REQ-01",
            shipper_name="Pimpri Auto Spares",
            pickup_stop="Pimpri",
            drop_stop="Panvel",
            date="2026-10-20",
            time_window_start="08:00",
            time_window_end="10:30",
            weight_kg=600.0,
            volume_m3=3.5,
            cargo_type="Auto Parts",
            required_vehicle="Closed Container",
            status="OPEN",
        )

        self.trucks[trk_101.id] = trk_101
        self.trucks[trk_108.id] = trk_108
        self.requests[req_01.id] = req_01

    def get_all_trucks(self) -> List[TruckListing]:
        with self._lock:
            return list(self.trucks.values())

    def get_truck(self, truck_id: str) -> Optional[TruckListing]:
        with self._lock:
            return self.trucks.get(truck_id)

    def add_truck(self, payload: CreateTruckRequest) -> TruckListing:
        with self._lock:
            truck_num = len(self.trucks) + 101
            truck_id = f"TRK-{truck_num}"
            while truck_id in self.trucks:
                truck_num += 1
                truck_id = f"TRK-{truck_num}"

            # Mask phone for privacy: format +91 98XXX-XX{last3}
            raw_phone = payload.phone.strip()
            digits = "".join(filter(str.isdigit, raw_phone))
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]

            if len(digits) >= 10:
                masked_phone = f"+91 {digits[:2]}XXX-XX{digits[-3:]}"
                full_phone = f"+91 {digits[:5]}-{digits[5:]}"
            else:
                masked_phone = "+91 98XXX-XX000"
                full_phone = raw_phone

            truck = TruckListing(
                id=truck_id,
                transporter_name=payload.transporter_name.strip(),
                vehicle_body=payload.vehicle_body.strip(),
                route_stops=[s.strip() for s in payload.route_stops if s.strip()],
                date=payload.date.strip(),
                time_window_start=payload.time_window_start.strip(),
                time_window_end=payload.time_window_end.strip(),
                available_weight_kg=payload.available_weight_kg,
                total_weight_kg=payload.total_weight_kg or (payload.available_weight_kg * 1.5),
                available_volume_m3=payload.available_volume_m3,
                total_volume_m3=payload.total_volume_m3 or (payload.available_volume_m3 * 1.5),
                allowed_cargo=[c.strip() for c in payload.allowed_cargo if c.strip()],
                masked_phone=masked_phone,
                full_phone=full_phone,
                status="AVAILABLE",
            )
            self.trucks[truck.id] = truck
            return truck

    def get_all_requests(self) -> List[ShipmentRequest]:
        with self._lock:
            return list(self.requests.values())

    def get_request(self, request_id: str) -> Optional[ShipmentRequest]:
        with self._lock:
            return self.requests.get(request_id)

    def add_request(self, payload: CreateShipmentInput) -> ShipmentRequest:
        with self._lock:
            req_num = len(self.requests) + 1
            req_id = f"REQ-{req_num:02d}"
            while req_id in self.requests:
                req_num += 1
                req_id = f"REQ-{req_num:02d}"

            req = ShipmentRequest(
                id=req_id,
                shipper_name=payload.shipper_name.strip(),
                pickup_stop=payload.pickup_stop.strip(),
                drop_stop=payload.drop_stop.strip(),
                date=payload.date.strip(),
                time_window_start=payload.time_window_start.strip(),
                time_window_end=payload.time_window_end.strip(),
                weight_kg=payload.weight_kg,
                volume_m3=payload.volume_m3,
                cargo_type=payload.cargo_type.strip(),
                required_vehicle=payload.required_vehicle or "Closed Container",
                status="OPEN",
            )
            self.requests[req.id] = req
            return req

    def find_matches(self, request_id: str) -> List[MatchResult]:
        with self._lock:
            req = self.requests.get(request_id)
            if not req:
                raise ValueError(f"Shipment request '{request_id}' not found.")
            truck_candidates = list(self.trucks.values())

        # Evaluate matches outside store lock to maintain high throughput
        results: List[MatchResult] = []
        for truck in truck_candidates:
            res = MatchingEngine.evaluate_match(truck, req)
            results.append(res)

        # Sort: compatible first, then by Fit Score descending, then by available weight
        results.sort(
            key=lambda x: (
                1 if x.evaluation.is_compatible else 0,
                x.fit_score,
                x.truck.available_weight_kg,
            ),
            reverse=True,
        )
        return results

    def confirm_booking(self, request_id: str, truck_id: str) -> Booking:
        """
        Executes an atomic, idempotent booking confirmation:
        1. Checks whether request is already booked (idempotent guard).
        2. Validates operational compatibility and capacity adequacy.
        3. Atomically deducts truck weight & volume capacity.
        4. Issues an unmasked Digital Coordination Pass.
        """
        with self._lock:
            req = self.requests.get(request_id)
            if not req:
                raise ValueError(f"Shipment request '{request_id}' not found.")

            truck = self.trucks.get(truck_id)
            if not truck:
                raise ValueError(f"Truck listing '{truck_id}' not found.")

            # Idempotency check: If this request already has a confirmed booking for this truck, return it
            for b in self.bookings.values():
                if b.request_id == request_id and b.status == "CONFIRMED":
                    if b.truck_id == truck_id:
                        return b
                    raise ValueError(f"Shipment request '{request_id}' is already booked with truck '{b.truck_id}'.")

            if req.status == "BOOKED":
                raise ValueError(f"Shipment request '{request_id}' has already been marked as BOOKED.")

            # Verify operational compatibility
            match_res = MatchingEngine.evaluate_match(truck, req)
            if not match_res.evaluation.is_compatible:
                failed_rules = []
                if not match_res.evaluation.rule_1_route:
                    failed_rules.append(f"Rule 1: {match_res.evaluation.rule_1_detail}")
                if not match_res.evaluation.rule_2_time:
                    failed_rules.append(f"Rule 2: {match_res.evaluation.rule_2_detail}")
                if not match_res.evaluation.rule_3_capacity:
                    failed_rules.append(f"Rule 3: {match_res.evaluation.rule_3_detail}")
                if not match_res.evaluation.rule_4_suitability:
                    failed_rules.append(f"Rule 4: {match_res.evaluation.rule_4_detail}")
                raise ValueError(f"Truck '{truck_id}' does not satisfy compatibility rules for '{request_id}': {' | '.join(failed_rules)}")

            # Capture snapshot before deduction
            before_cap = CapacitySnapshot(
                weight_kg=truck.available_weight_kg,
                volume_m3=truck.available_volume_m3,
            )

            # Atomic capacity deduction
            truck.available_weight_kg = round(truck.available_weight_kg - req.weight_kg, 2)
            truck.available_volume_m3 = round(truck.available_volume_m3 - req.volume_m3, 2)

            if truck.available_weight_kg <= 0 and truck.available_volume_m3 <= 0:
                truck.status = "FULL"
            else:
                truck.status = "PARTIAL"

            # Capture snapshot after deduction
            after_cap = CapacitySnapshot(
                weight_kg=truck.available_weight_kg,
                volume_m3=truck.available_volume_m3,
            )

            # Issue Digital Coordination Pass with UNMASKED direct driver phone
            booking_id = f"BKG-{uuid.uuid4().hex[:8].upper()}"
            pass_id = f"PASS-{truck.id}-{uuid.uuid4().hex[:6].upper()}"
            now_iso = datetime.now().isoformat()
            verification_code = f"LB-{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[:4].upper()}"

            coordination_pass = CoordinationPass(
                pass_id=pass_id,
                booking_id=booking_id,
                truck_id=truck.id,
                transporter_name=truck.transporter_name,
                unmasked_phone=truck.full_phone,
                driver_name=f"{truck.transporter_name} Fleet Dispatcher",
                vehicle_body=truck.vehicle_body,
                shipper_name=req.shipper_name,
                pickup_stop=req.pickup_stop,
                drop_stop=req.drop_stop,
                date=req.date,
                scheduled_window=f"{truck.time_window_start} - {truck.time_window_end}",
                weight_kg=req.weight_kg,
                volume_m3=req.volume_m3,
                cargo_type=req.cargo_type,
                before_capacity=before_cap,
                after_capacity=after_cap,
                issued_at=now_iso,
                verification_code=verification_code,
            )

            booking = Booking(
                id=booking_id,
                request_id=req.id,
                truck_id=truck.id,
                status="CONFIRMED",
                coordination_pass=coordination_pass,
                created_at=now_iso,
            )

            req.status = "BOOKED"
            self.bookings[booking.id] = booking
            return booking

    def cancel_booking(self, booking_id: str) -> Booking:
        """
        Executes pre-departure cancellation:
        1. Validates booking exists and is CONFIRMED.
        2. Restores cargo weight and volume back to the truck.
        3. Marks booking as CANCELLED and shipment request as OPEN.
        """
        with self._lock:
            booking = self.bookings.get(booking_id)
            if not booking:
                raise ValueError(f"Booking '{booking_id}' not found.")

            if booking.status == "CANCELLED":
                return booking  # Idempotent cancellation

            req = self.requests.get(booking.request_id)
            truck = self.trucks.get(booking.truck_id)

            if truck and req:
                # Restore capacity
                truck.available_weight_kg = min(
                    truck.total_weight_kg,
                    round(truck.available_weight_kg + req.weight_kg, 2)
                )
                truck.available_volume_m3 = min(
                    truck.total_volume_m3,
                    round(truck.available_volume_m3 + req.volume_m3, 2)
                )
                truck.status = "AVAILABLE"

            if req:
                req.status = "OPEN"

            booking.status = "CANCELLED"
            return booking

    def get_all_bookings(self) -> List[Booking]:
        with self._lock:
            # Sort newest first
            return sorted(self.bookings.values(), key=lambda b: b.created_at, reverse=True)

    def get_truck_telemetry(
        self,
        truck_id: str,
        simulated_time: Optional[str] = None,
        progress: Optional[float] = None,
    ) -> TruckTelemetry:
        """Retrieves real-time/simulated GPS telemetry for a truck."""
        with self._lock:
            truck = self.trucks.get(truck_id)
            if not truck:
                raise ValueError(f"Truck listing '{truck_id}' not found.")
        return calculate_truck_telemetry(truck, simulated_time=simulated_time, progress=progress)


# Global singleton instance for the platform
platform_state = PlatformState()

