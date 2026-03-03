"""
PDF Report Generation
Monthly SLA and Incident summary reports using fpdf2.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, date
from calendar import monthrange

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.deps import get_session
from app.database import fetch_all

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Colour palette (dark-blue NOC theme) ──────────────────────────────────────
_BRAND    = (0,   180, 255)   # cyan accent
_HDR_BG   = (6,   9,   15)   # near-black header
_HDR_FG   = (255, 255, 255)
_ROW_ALT  = (11,  17,  32)
_ROW_NORM = (15,  22,  40)
_OK_BG    = (0,   180, 100)
_WARN_BG  = (255, 165,  0)
_CRIT_BG  = (220,  50,  50)
_TEXT     = (200, 215, 230)


def _build_pdf(title: str) -> "FPDF":
    from fpdf import FPDF

    class _NocPDF(FPDF):
        def header(self):
            self.set_fill_color(*_HDR_BG)
            self.rect(0, 0, 210, 18, "F")
            self.set_text_color(*_BRAND)
            self.set_font("Helvetica", "B", 13)
            self.set_xy(10, 4)
            self.cell(0, 10, "NOC Sentinel  |  " + title, align="L")
            self.set_text_color(*_HDR_FG)
            self.set_font("Helvetica", "", 8)
            self.set_xy(-55, 4)
            self.cell(45, 10, datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), align="R")
            self.ln(14)

        def footer(self):
            self.set_y(-12)
            self.set_font("Helvetica", "I", 7)
            self.set_text_color(100, 120, 140)
            self.cell(0, 8, f"Page {self.page_no()}", align="C")

    pdf = _NocPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.set_margins(10, 20, 10)
    return pdf


# ── SLA Report ─────────────────────────────────────────────────────────────────

@router.get("/reports/sla/pdf")
async def sla_pdf_report(
    month: str = Query(default=None, description="YYYY-MM, defaults to current month"),
    session: dict = Depends(get_session),
):
    """Download a monthly SLA PDF report."""
    if not month:
        month = datetime.utcnow().strftime("%Y-%m")
    try:
        year, mon = int(month[:4]), int(month[5:7])
    except Exception:
        year, mon = datetime.utcnow().year, datetime.utcnow().month

    month_label = date(year, mon, 1).strftime("%B %Y")
    _, days_in_month = monthrange(year, mon)
    period_start = f"{year}-{mon:02d}-01"
    period_end   = f"{year}-{mon:02d}-{days_in_month}"

    # Fetch SLA rows
    rows = await fetch_all(
        """SELECT s.name, s.target_pct,
                  COALESCE(sm.uptime_pct, 0) AS actual_pct,
                  COALESCE(sm.downtime_minutes, 0) AS downtime_min
           FROM sla s
           LEFT JOIN sla_measurements sm
             ON sm.sla_id=s.id AND sm.period_start=%s
           ORDER BY s.name""",
        (period_start,),
    )

    # Fallback: compute from incidents if no sla_measurements table
    if not rows:
        rows = await fetch_all("SELECT name, target_pct FROM sla ORDER BY name")
        rows = [{**r, "actual_pct": 0.0, "downtime_min": 0} for r in rows]

    # Fetch incident summary
    incidents = await fetch_all(
        """SELECT severity, COUNT(*) AS cnt, AVG(TIMESTAMPDIFF(MINUTE, created_at, COALESCE(resolved_at, NOW()))) AS avg_min
           FROM incidents
           WHERE DATE(created_at) BETWEEN %s AND %s
           GROUP BY severity""",
        (period_start, period_end),
    )

    from fpdf import FPDF  # noqa: F401 — ensure available
    pdf = _build_pdf(f"SLA Report – {month_label}")
    pdf.add_page()

    # ── SLA table ──────────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(*_HDR_BG)
    pdf.set_text_color(*_HDR_FG)
    COL = [80, 30, 30, 35, 40]  # widths
    HDRS = ["Service", "Target %", "Actual %", "Downtime (min)", "Status"]
    for i, h in enumerate(HDRS):
        pdf.cell(COL[i], 8, h, border=1, fill=True, align="C")
    pdf.ln()

    for idx, r in enumerate(rows):
        fill_bg = _ROW_ALT if idx % 2 == 0 else _ROW_NORM
        actual  = float(r.get("actual_pct") or 0)
        target  = float(r.get("target_pct") or 99.9)
        down    = int(r.get("downtime_min") or 0)

        if actual == 0:
            status, sbg = "NO DATA", (80, 90, 100)
        elif actual >= target:
            status, sbg = "OK", _OK_BG
        elif actual >= target - 1:
            status, sbg = "AT RISK", _WARN_BG
        else:
            status, sbg = "BREACHED", _CRIT_BG

        pdf.set_fill_color(*fill_bg)
        pdf.set_text_color(*_TEXT)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(COL[0], 7, str(r.get("name", "")), border=1, fill=True)
        pdf.cell(COL[1], 7, f"{target:.2f}", border=1, fill=True, align="C")
        pdf.cell(COL[2], 7, f"{actual:.3f}" if actual else "—", border=1, fill=True, align="C")
        pdf.cell(COL[3], 7, str(down) if down else "—", border=1, fill=True, align="C")

        pdf.set_fill_color(*sbg)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(COL[4], 7, status, border=1, fill=True, align="C")
        pdf.set_text_color(*_TEXT)
        pdf.ln()

    # ── Incident summary ───────────────────────────────────────────────────────
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*_BRAND)
    pdf.cell(0, 8, "Incident Summary", ln=True)

    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(*_HDR_BG)
    pdf.set_text_color(*_HDR_FG)
    ICOL = [50, 30, 50]
    for h in ["Severity", "Count", "Avg Resolution (min)"]:
        pdf.cell(ICOL[ICOL.index(next(x for x in ICOL if x))], 7, h, border=1, fill=True, align="C")
    # simpler: fixed widths
    for h, w in [("Severity", 50), ("Count", 30), ("Avg Resolution (min)", 60)]:
        pdf.cell(w, 7, h, border=1, fill=True, align="C")
    pdf.ln()

    for inc in (incidents or [{"severity": "—", "cnt": 0, "avg_min": 0}]):
        pdf.set_font("Helvetica", "", 9)
        pdf.set_fill_color(*_ROW_NORM)
        pdf.set_text_color(*_TEXT)
        pdf.cell(50, 6, str(inc.get("severity") or "—"), border=1, fill=True)
        pdf.cell(30, 6, str(inc.get("cnt") or 0), border=1, fill=True, align="C")
        avg = inc.get("avg_min")
        pdf.cell(60, 6, f"{float(avg):.1f}" if avg else "—", border=1, fill=True, align="C")
        pdf.ln()

    pdf_bytes = pdf.output()
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="sla_report_{month}.pdf"'},
    )


# ── Incident Report ────────────────────────────────────────────────────────────

@router.get("/reports/incidents/pdf")
async def incidents_pdf_report(
    month: str = Query(default=None, description="YYYY-MM, defaults to current month"),
    session: dict = Depends(get_session),
):
    """Download a monthly Incidents PDF report."""
    if not month:
        month = datetime.utcnow().strftime("%Y-%m")
    try:
        year, mon = int(month[:4]), int(month[5:7])
    except Exception:
        year, mon = datetime.utcnow().year, datetime.utcnow().month

    month_label  = date(year, mon, 1).strftime("%B %Y")
    _, days_in_m = monthrange(year, mon)
    period_start = f"{year}-{mon:02d}-01"
    period_end   = f"{year}-{mon:02d}-{days_in_m}"

    rows = await fetch_all(
        """SELECT id, title, severity, status, source,
                  DATE_FORMAT(created_at,'%%Y-%%m-%%d %%H:%%i') AS opened,
                  DATE_FORMAT(resolved_at,'%%Y-%%m-%%d %%H:%%i') AS resolved
           FROM incidents
           WHERE DATE(created_at) BETWEEN %s AND %s
           ORDER BY created_at DESC""",
        (period_start, period_end),
    )

    pdf = _build_pdf(f"Incident Report – {month_label}")
    pdf.add_page()

    # Table header
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(*_HDR_BG)
    pdf.set_text_color(*_HDR_FG)
    COLS = [12, 90, 22, 22, 22, 38, 38]
    HDRS = ["ID", "Title", "Severity", "Status", "Source", "Opened", "Resolved"]
    for w, h in zip(COLS, HDRS):
        pdf.cell(w, 7, h, border=1, fill=True, align="C")
    pdf.ln()

    SEV_COLOR = {
        "critical": _CRIT_BG, "high": (200, 80, 0), "warning": _WARN_BG,
        "average": (180, 130, 0), "info": (0, 120, 200), "ok": _OK_BG,
    }
    STA_COLOR = {
        "open": _CRIT_BG, "in_progress": _WARN_BG, "resolved": _OK_BG, "closed": (60, 80, 100),
    }

    for idx, r in enumerate(rows):
        fill_bg = _ROW_ALT if idx % 2 == 0 else _ROW_NORM
        pdf.set_fill_color(*fill_bg)
        pdf.set_text_color(*_TEXT)
        pdf.set_font("Helvetica", "", 7)

        pdf.cell(COLS[0], 6, str(r.get("id") or ""), border=1, fill=True, align="C")

        # Title cell (may be long)
        title = str(r.get("title") or "")[:60]
        pdf.cell(COLS[1], 6, title, border=1, fill=True)

        sev = str(r.get("severity") or "").lower()
        pdf.set_fill_color(*SEV_COLOR.get(sev, (80, 90, 100)))
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 7)
        pdf.cell(COLS[2], 6, sev.upper(), border=1, fill=True, align="C")

        sta = str(r.get("status") or "").lower()
        pdf.set_fill_color(*STA_COLOR.get(sta, (80, 90, 100)))
        pdf.cell(COLS[3], 6, sta.upper(), border=1, fill=True, align="C")

        pdf.set_fill_color(*fill_bg)
        pdf.set_text_color(*_TEXT)
        pdf.set_font("Helvetica", "", 7)
        pdf.cell(COLS[4], 6, str(r.get("source") or "manual"), border=1, fill=True, align="C")
        pdf.cell(COLS[5], 6, str(r.get("opened") or "—"), border=1, fill=True, align="C")
        pdf.cell(COLS[6], 6, str(r.get("resolved") or "—"), border=1, fill=True, align="C")
        pdf.ln()

    # Summary line
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*_BRAND)
    pdf.cell(0, 8, f"Total incidents in {month_label}: {len(rows)}", ln=True)

    pdf_bytes = pdf.output()
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="incidents_report_{month}.pdf"'},
    )
