"""
LoadBridge — Local Truck-Load & Spare-Capacity Matching Platform
FastAPI Application & RESTful API Service
"""

import os
from pathlib import Path
from typing import List, Dict, Any
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.matching_engine import (
    platform_state,
    TruckListing,
    CreateTruckRequest,
    ShipmentRequest,
    CreateShipmentInput,
    MatchResult,
    Booking,
    BookingConfirmRequest,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"

app = FastAPI(
    title="LoadBridge Platform",
    description="Local Truck-Load & Spare-Capacity Matching Platform for Industrial Freight Corridors",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Enable CORS for local development and portfolio showcases
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files directory if it exists
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============================================================================
# Frontend Root Route
# ============================================================================

@app.get("/", response_class=FileResponse, summary="Serve LoadBridge Single-Page Web Application")
async def read_root():
    if not INDEX_FILE.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Frontend index.html not found.",
        )
    return FileResponse(str(INDEX_FILE), media_type="text/html")


# ============================================================================
# Transporter & Truck Fleet Endpoints
# ============================================================================

@app.get("/api/trucks", response_model=List[TruckListing], tags=["Transporters"])
def get_truck_listings():
    """Retrieve all active truck capacity listings across the corridor."""
    return platform_state.get_all_trucks()


@app.post("/api/trucks", response_model=TruckListing, status_code=status.HTTP_201_CREATED, tags=["Transporters"])
def publish_truck_capacity(payload: CreateTruckRequest):
    """Publish spare truck weight & volume capacity with ordered corridor stops."""
    try:
        new_truck = platform_state.add_truck(payload)
        return new_truck
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@app.get("/api/trucks/{truck_id}", response_model=TruckListing, tags=["Transporters"])
def get_truck_by_id(truck_id: str):
    """Get detailed specification for a specific truck listing."""
    truck = platform_state.get_truck(truck_id)
    if not truck:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Truck listing '{truck_id}' not found.",
        )
    return truck


# ============================================================================
# Shipper & Shipment Request Endpoints
# ============================================================================

@app.get("/api/requests", response_model=List[ShipmentRequest], tags=["Shippers"])
def list_shipment_requests():
    """List all open and active shipment requests."""
    return platform_state.get_all_requests()


@app.post("/api/requests", response_model=ShipmentRequest, status_code=status.HTTP_201_CREATED, tags=["Shippers"])
def create_shipment_request(payload: CreateShipmentInput):
    """Submit a new shipment cargo request with weight, volume, pickup, and drop."""
    try:
        new_req = platform_state.add_request(payload)
        return new_req
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@app.get("/api/requests/{request_id}", response_model=ShipmentRequest, tags=["Shippers"])
def get_shipment_request(request_id: str):
    """Get shipment details by ID."""
    req = platform_state.get_request(request_id)
    if not req:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Shipment request '{request_id}' not found.",
        )
    return req


# ============================================================================
# 4-Rule Matching Engine API
# ============================================================================

@app.get("/api/matches/{request_id}", response_model=List[MatchResult], tags=["Matching Engine"])
def get_matches_for_request(request_id: str):
    """
    Evaluates all corridor trucks against the 4-Rule Matching Engine:
    1. Ordered Corridor Stop Check
    2. Date & Time Window Overlap
    3. Dual Capacity Fit (Weight & Volume)
    4. Vehicle Body & Cargo Suitability Check

    Returns matches ranked by Fit Score (0-99%).
    """
    try:
        matches = platform_state.find_matches(request_id)
        return matches
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ============================================================================
# Bookings & Digital Coordination Pass Endpoints
# ============================================================================

@app.post("/api/bookings/confirm", response_model=Booking, status_code=status.HTTP_201_CREATED, tags=["Bookings"])
def confirm_booking(payload: BookingConfirmRequest):
    """
    Atomically books spare capacity on a selected truck:
    - Verifies compatibility & capacity availability
    - Idempotently deducts cargo weight and volume
    - Issues an unmasked Digital Coordination Pass with direct driver contact
    """
    try:
        booking = platform_state.confirm_booking(payload.request_id, payload.truck_id)
        return booking
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.post("/api/bookings/{booking_id}/cancel", response_model=Booking, tags=["Bookings"])
def cancel_booking(booking_id: str):
    """
    Pre-departure cancellation:
    - Atomically restores the booked weight & volume back to the truck
    - Updates booking status to CANCELLED and shipment request to OPEN
    """
    try:
        booking = platform_state.cancel_booking(booking_id)
        return booking
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@app.get("/api/bookings", response_model=List[Booking], tags=["Bookings"])
def get_all_bookings():
    """Retrieve full audit ledger of Digital Coordination Passes and bookings."""
    return platform_state.get_all_bookings()


# ============================================================================
# Platform Metrics & Corridor Telemetry
# ============================================================================

@app.get("/api/stats", tags=["System"])
def get_platform_statistics() -> Dict[str, Any]:
    """Summary metrics of corridor capacity, request volume, and booking fill rates."""
    trucks = platform_state.get_all_trucks()
    requests = platform_state.get_all_requests()
    bookings = platform_state.get_all_bookings()

    total_avail_wt = sum(t.available_weight_kg for t in trucks)
    total_cap_wt = sum(t.total_weight_kg for t in trucks)
    total_avail_vol = sum(t.available_volume_m3 for t in trucks)
    total_cap_vol = sum(t.total_volume_m3 for t in trucks)

    confirmed_bookings = [b for b in bookings if b.status == "CONFIRMED"]

    return {
        "active_trucks": len(trucks),
        "total_available_weight_kg": round(total_avail_wt, 1),
        "total_capacity_weight_kg": round(total_cap_wt, 1),
        "weight_utilization_pct": round(((total_cap_wt - total_avail_wt) / max(total_cap_wt, 1)) * 100, 1),
        "total_available_volume_m3": round(total_avail_vol, 2),
        "total_capacity_volume_m3": round(total_cap_vol, 2),
        "volume_utilization_pct": round(((total_cap_vol - total_avail_vol) / max(total_cap_vol, 0.01)) * 100, 1),
        "total_requests": len(requests),
        "open_requests": len([r for r in requests if r.status == "OPEN"]),
        "confirmed_bookings": len(confirmed_bookings),
        "corridor": "Pune — Mumbai Industrial Superhighway (NH-48)",
    }
