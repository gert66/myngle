"""
lusha_api_server.py
-------------------
FastAPI micro-server exposing a single contact-lookup endpoint for the
mYngle Caller Prep React frontend (Lovable).

One company at a time only — batch enrichment is NOT supported here.
Raw Lusha responses are never forwarded to the frontend.

Start:
    uvicorn lusha_api_server:app --host 127.0.0.1 --port 8008 --reload

Environment variables:
    LUSHA_API_KEY          — required for live calls
    LUSHA_ALLOWED_ORIGINS  — comma-separated CORS origins
                             default: http://localhost:5173,http://127.0.0.1:5173
"""

import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, model_validator

import lusha_client as lc
import lusha_ranker  as lr

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

_DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
_allowed_origins = [
    o.strip()
    for o in os.environ.get("LUSHA_ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",")
    if o.strip()
]

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="mYngle Lusha Contact API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ContactRequest(BaseModel):
    companyName: Optional[str] = None
    domain:      Optional[str] = None
    country:     Optional[str] = None
    industry:    Optional[str] = None

    @model_validator(mode="after")
    def require_name_or_domain(self) -> "ContactRequest":
        if not self.companyName and not self.domain:
            raise ValueError("Provide at least companyName or domain.")
        return self


class ContactResponse(BaseModel):
    status:   str
    source:   str = "Lusha"
    contacts: list[dict] = []
    message:  Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/lusha/contacts", response_model=ContactResponse)
def get_contacts(req: ContactRequest) -> ContactResponse:
    """
    Look up decision-maker contacts for a single company via Lusha,
    rank them for mYngle relevance, and return the normalised result.

    Raw Lusha data is never returned. Email/phone are included only
    when Lusha reveals them (depends on plan/credits).
    """
    try:
        raw_contacts = lc.find_contacts(
            company_name=req.companyName or "",
            domain=req.domain or "",
            country=req.country,
        )
    except RuntimeError as exc:
        # Convert internal error to safe user-facing message
        return ContactResponse(
            status="error",
            contacts=[],
            message=str(exc),
        )
    except Exception:
        return ContactResponse(
            status="error",
            contacts=[],
            message="An unexpected error occurred while contacting Lusha.",
        )

    if not raw_contacts:
        return ContactResponse(
            status="not_found",
            contacts=[],
            message="No relevant contacts found",
        )

    ranked = lr.rank_contacts_for_myngle(raw_contacts, industry=req.industry)

    # Strip internal fields before returning to frontend
    clean = [
        {k: v for k, v in c.items() if not k.startswith("_")}
        for c in ranked
    ]

    return ContactResponse(status="ok", contacts=clean)
