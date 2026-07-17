"""
pdf_renderer.py  —  Resume & Cover Letter PDF Generator  v6
===========================================================
All values extracted directly from master_resume.pdf via pdfplumber.

KEY CORRECTIONS IN v6:
  - Sidebar section bars: very slightly darker teal #006996
    (black 10% opacity over #0074A6) — barely perceptible darkening
  - Tenure dates: lighter grey #8C8C8C — visually recedes behind role title
  - Role/company/bullets: no indent gap — start at left edge of right col
    (tenure is now right-aligned so the old date-column space is freed)

EXACT VALUES:
  Sidebar bg:        #0074A6  (0.0, 0.4588, 0.6549)
  Sidebar sec bars:  #006996  (0.0, 0.413,  0.589)  — very slightly darker teal
  All sidebar text:  #FFFFFF  white
  Right-col headers: #0074A6  teal, 13.24pt Regular
  Body text:         #363C49  (0.2118, 0.2392, 0.2863) warm dark slate
  Tenure text:       #8C8C8C  (0.55, 0.55, 0.55)  lighter grey
  Dividers:          #DEDEDE  0.6pt
  Company names:     italic 9.02pt
  Role titles:       regular 10.23pt
  Bullets:           regular 7.82pt
"""

import sys, json, textwrap, re
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle

# ── Colours ────────────────────────────────────────────────────────────────────
TEAL        = colors.Color(0.0,    0.4588, 0.6549)  # #0074A6 sidebar bg
TEAL_DARK   = colors.Color(0.0,    0.413,  0.589 )  # #006996 section bars — black 10% opacity over teal (very slightly darker)
BLACK       = colors.Color(0.0,    0.0,    0.0   )
WHITE       = colors.Color(1.0,    1.0,    1.0   )
DIVIDER     = colors.Color(0.8745, 0.8745, 0.8745)  # #DEDEDE
DARK        = colors.Color(0.2118, 0.2392, 0.2863)  # #363C49 all body text
TENURE_GREY = colors.Color(0.55,   0.55,   0.55  )  # lighter grey for tenure dates
ROLE_BOLD   = colors.Color(0.32,   0.32,   0.32  )  # #525252 bold role/degree — lighter than DARK, not too light

# ── Page geometry ──────────────────────────────────────────────────────────────
PW, PH   = A4                   # 595.28 × 841.89 pts
SB_W     = 190.73               # sidebar width
RX       = SB_W
RW       = PW - SB_W
SPAD     = 14.0                 # sidebar left padding
RPAD     = 14.0                 # right-col left padding from RX
RRIGHT   = PW - 14.0            # right-col right edge
RTW      = RRIGHT - (RX+RPAD)  # right-col text width

# Date-col and role-col x positions
# With tenure now right-aligned, role/company/bullets start at the left
# edge of the right column — no indentation gap needed.
DATE_X   = RX + RPAD            # 204.9 — left edge of right col
ROLE_X   = RX + RPAD            # same as DATE_X — no indent gap
ROLE_TW  = RRIGHT - ROLE_X      # full text width

# ── Font sizes (pts, from PDF) ─────────────────────────────────────────────────
FS_NAME   = 22.86   # sidebar name
FS_PROF   = 10.23   # sidebar profession lines
FS_SHEAD  = 13.24   # sidebar section header labels
FS_SLBL   = 10.23   # sidebar field labels (Email, Phone…)
FS_SVAL   =  9.02   # sidebar field values
FS_SITEM  =  7.82   # sidebar list items
FS_RCHEAD = 13.24   # right-col section headers (teal)
FS_ROLE   = 10.23   # job role title
FS_CODATE =  9.02   # company name + date
FS_BULLET =  7.82   # bullet text
FS_CLLBL  = 10.23   # cert name
FS_CLVAL  =  9.02   # cert provider
SEC_BAR_H = 25.3    # section bar height

MONTH_SHORT = {
    "01":"Jan","02":"Feb","03":"Mar","04":"Apr","05":"May","06":"Jun",
    "07":"Jul","08":"Aug","09":"Sep","10":"Oct","11":"Nov","12":"Dec",
}

def _safe_html(text: str) -> str:
    """Escape HTML entities and normalise curly/smart quotes for reportlab Paragraph."""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace("“", '"').replace("”", '"')   # " "
    text = text.replace("‘", "'").replace("’", "'")   # ' '
    return text

def fmt_date(raw):
    """'2025-04-2026-03' → 'Apr 2025 – Mar 2026'; '2026-03-present' → 'Mar 2026 – Present'"""
    if not raw:
        return ""
    parts = str(raw).split("-")
    if len(parts) == 4:
        y1, m1, y2, m2 = parts
        return f"{MONTH_SHORT.get(m1,m1)} {y1} – {MONTH_SHORT.get(m2,m2)} {y2}"
    if len(parts) == 3 and parts[2].lower() == "present":
        y1, m1 = parts[0], parts[1]
        return f"{MONTH_SHORT.get(m1,m1)} {y1} – Present"
    return raw

# ─────────────────────────────────────────────────────────────────────────────
# Canvas helpers
# ─────────────────────────────────────────────────────────────────────────────

def cy(y_top, fs=0):
    return PH - y_top - fs

def draw_sidebar_bg(c):
    c.setFillColor(TEAL)
    c.rect(0, 0, SB_W, PH, fill=1, stroke=0)

def draw_section_bar(c, y_top, label):
    """Darker-teal bar with white label."""
    bar_canvas_y = cy(y_top) - SEC_BAR_H
    c.setFillColor(TEAL_DARK)
    c.rect(0, bar_canvas_y, SB_W, SEC_BAR_H, fill=1, stroke=0)
    text_y = bar_canvas_y + (SEC_BAR_H - FS_SHEAD) / 2 + 1
    c.setFillColor(WHITE)
    c.setFont("Helvetica", FS_SHEAD)          # Regular weight, matching PDF
    c.drawString(SPAD, text_y, label)
    return y_top + SEC_BAR_H

def divider_line(c, y_top):
    c.setFillColor(DIVIDER)
    c.rect(RX+RPAD, cy(y_top), RTW, 0.6, fill=1, stroke=0)
    return y_top + 1.5

def rc_section_header(c, y_top, text):
    """Teal, 13.24pt Regular — matches 'Work History' and 'Education' in PDF."""
    c.setFillColor(TEAL)
    c.setFont("Helvetica", FS_RCHEAD)         # Regular, not bold
    c.drawString(RX+RPAD, cy(y_top, FS_RCHEAD), text)
    return y_top + FS_RCHEAD + 3

def sb_line(c, y_top, text, size=None, indent=None):
    """Single sidebar line — always white, always Regular."""
    fs = size or FS_SITEM
    xi = indent if indent is not None else SPAD
    c.setFillColor(WHITE)
    c.setFont("Helvetica", fs)
    c.drawString(xi, cy(y_top, fs), text)
    return y_top + fs * 1.6

def sb_wrapped(c, y_top, text, size=None, indent=None):
    """Sidebar wrapped text."""
    fs     = size or FS_SITEM
    xi     = indent if indent is not None else SPAD
    avail  = SB_W - xi - 6
    cpl    = max(1, int(avail / (fs * 0.52)))
    lines  = textwrap.wrap(text, width=cpl) or [text]
    c.setFillColor(WHITE)
    c.setFont("Helvetica", fs)
    for ln in lines:
        c.drawString(xi, cy(y_top, fs), ln)
        y_top += fs * 1.45
    return y_top + fs * 0.15

def rc_para(c, x, y_top, text, fs, color, italic=False, bold=False, tw=None):
    """Wrapped paragraph in right column. italic and bold are mutually exclusive."""
    avail = tw if tw is not None else (RRIGHT - x)
    if italic:   fn = "Helvetica-Oblique"
    elif bold:   fn = "Helvetica-Bold"
    else:        fn = "Helvetica"
    style = ParagraphStyle("p", fontName=fn, fontSize=fs,
                           leading=fs*1.45, textColor=color)
    p = Paragraph(_safe_html(text), style)
    w, h = p.wrap(avail, PH)
    p.drawOn(c, x, cy(y_top) - h)
    return y_top + h + 1.5

def rc_bullet(c, y_top, text):
    fs = FS_BULLET
    avail = RRIGHT - ROLE_X - 10
    style = ParagraphStyle("b", fontName="Helvetica", fontSize=fs,
                           leading=fs*1.48, textColor=DARK)
    p = Paragraph(_safe_html(text), style)
    w, h = p.wrap(avail, PH)
    p.drawOn(c, ROLE_X+10, cy(y_top) - h)
    c.setFillColor(DARK)
    c.circle(ROLE_X+3.5, cy(y_top) - fs*0.65, 1.1, fill=1, stroke=0)
    return y_top + h + 2.5

def check_page(c, y_top, needed=60):
    if y_top + needed > PH - 20:
        c.showPage()
        draw_sidebar_bg(c)
        return 18
    return y_top

# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# VALIDATION HELPER
# ─────────────────────────────────────────────────────────────────────────────

_PLACEHOLDERS = {"FILL_ME", "FILL_DATE", "TODO", "TBD"}

def _validate_doc(data: dict, kind: str):
    """Warn loudly if obvious placeholder strings remain in key fields."""
    def _has_placeholder(val: str) -> bool:
        return any(p in val.upper() for p in _PLACEHOLDERS)

    field_checks = {
        "resume": ["summary"],
        "cover":  ["date", "recipient", "salutation"],
    }
    for field in field_checks.get(kind, []):
        val = str(data.get(field, ""))
        if _has_placeholder(val):
            print(f"[pdf_renderer] ⚠  WARNING: '{field}' contains placeholder text: {val!r}")
    if kind == "cover":
        for i, para in enumerate(data.get("paragraphs", [])):
            if _has_placeholder(str(para)):
                print(f"[pdf_renderer] ⚠  WARNING: paragraphs[{i}] contains placeholder text")
        n = len(data.get("paragraphs", []))
        if n < 4:
            print(f"[pdf_renderer] ⚠  WARNING: cover letter has {n} paragraphs (expected 4)")


# SINGLE-COLUMN RESUME (UK default)
# ─────────────────────────────────────────────────────────────────────────────

def _build_single_column(data: dict, output_path: str):
    """Full-width single-column A4 resume — UK market default."""
    c = rl_canvas.Canvas(output_path, pagesize=A4)
    c.setTitle("Vinay Patidar — CV")
    c.setAuthor("Vinay Patidar")
    c.setCreator("Vinay Patidar")
    c.setSubject("Analytics Professional")

    LMAR   = 40.0
    RMAR   = 40.0
    CTW    = PW - LMAR - RMAR   # 515.28 pts usable width
    CRIGHT = PW - RMAR          # 555.28

    # ── HEADER ───────────────────────────────────────────────────
    contact = data.get("contact", {})
    y = 18.0

    # Contact lines drawn FIRST in PDF stream (right-aligned, stacked, grey).
    # Drawing order matters: name drawn second so PDF text-stream order is
    # contact-info → name, preventing macOS data-detector from concatenating
    # the last name with the email address into a malformed mailto: link.
    contact_lines = [v for v in [
        contact.get("email", ""),
        contact.get("phone", ""),
        contact.get("linkedin", ""),
        contact.get("github", ""),
        contact.get("address", ""),
    ] if v]
    yc = y
    for cl in contact_lines:
        c.setFillColor(TENURE_GREY)
        c.setFont("Helvetica", FS_SVAL)
        c.drawRightString(CRIGHT, cy(yc, FS_SVAL), cl)
        yc += FS_SVAL * 1.4  # 1.4× keeps 5 contact lines within title-block height

    # Name — left, bold, dark (drawn after contact lines in stream order)
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", FS_NAME)
    c.drawString(LMAR, cy(y, FS_NAME), data["name"])
    name_bottom = y + FS_NAME  # visual bottom of name in top-down coords

    # Pre-compute rule_y so title_lines can be centred in the available space.
    # title_content_h = visual height of N lines: (N-1) inter-line gaps + one font-height.
    title_lines_list = data.get("title_lines", [])
    N = len(title_lines_list)
    title_content_h = ((N - 1) * FS_PROF * 1.5 + FS_PROF) if N > 0 else 0
    min_gap = 5  # minimum spacing above and below title block
    rule_y = max(name_bottom + min_gap + title_content_h + min_gap, yc + 2)

    # Draw title lines — centred between name bottom and rule (equal gap above and below).
    if N > 0:
        equal_gap = (rule_y - name_bottom - title_content_h) / 2
        ty = name_bottom + equal_gap
        c.setFillColor(TEAL)
        c.setFont("Helvetica", FS_PROF)
        for tl in title_lines_list:
            c.drawString(LMAR, cy(ty, FS_PROF), tl)
            ty += FS_PROF * 1.5

    y = rule_y

    # Teal rule
    c.setFillColor(TEAL)
    c.rect(LMAR, cy(y), CTW, 2.0, fill=1, stroke=0)
    y += 8

    # ── INNER HELPERS (closures over c, LMAR, CRIGHT, CTW) ───────

    def sc_section_header(y_top, text):
        c.setFillColor(TEAL)
        c.setFont("Helvetica", FS_RCHEAD)
        c.drawString(LMAR, cy(y_top, FS_RCHEAD), text)
        y_top += FS_RCHEAD + 3
        c.setFillColor(DIVIDER)
        c.rect(LMAR, cy(y_top), CTW, 0.6, fill=1, stroke=0)
        return y_top + 4

    def sc_para(y_top, text, fs=FS_BULLET, color=DARK, italic=False, bold=False):
        fn = ("Helvetica-Oblique" if italic else
              "Helvetica-Bold"   if bold   else "Helvetica")
        style = ParagraphStyle("sc", fontName=fn, fontSize=fs,
                               leading=fs * 1.45, textColor=color)
        p = Paragraph(_safe_html(text), style)
        _, h = p.wrap(CTW, PH)
        p.drawOn(c, LMAR, cy(y_top) - h)
        return y_top + h + 1.5

    def sc_bullet(y_top, text):
        fs    = FS_BULLET
        avail = CTW - 12
        style = ParagraphStyle("scb", fontName="Helvetica", fontSize=fs,
                               leading=fs * 1.48, textColor=DARK)
        p = Paragraph(_safe_html(text), style)
        _, h = p.wrap(avail, PH)
        p.drawOn(c, LMAR + 12, cy(y_top) - h)
        c.setFillColor(DARK)
        c.circle(LMAR + 4, cy(y_top) - fs * 0.65, 1.1, fill=1, stroke=0)
        return y_top + h + 2.5

    def sc_check_page(y_top, needed=60):
        if y_top + needed > PH - 20:
            c.showPage()
            return 18
        return y_top

    # ── PROFILE SUMMARY ──────────────────────────────────────────
    if data.get("summary"):
        y = sc_section_header(y, "Profile Summary")
        y = sc_para(y, data["summary"])
        y += 6

    # ── CORE EXPERTISE + SKILLS (full-width stacked — ATS safe) ──
    expertise  = data.get("core_expertise", [])
    skills     = data.get("skills", [])
    exp_text   = ", ".join(expertise)
    skill_text = ", ".join(skills)

    lbl_fs = 8.5
    if expertise:
        c.setFillColor(TEAL)
        c.setFont("Helvetica", lbl_fs)
        c.drawString(LMAR, cy(y, lbl_fs), "CORE EXPERTISE")
        y += lbl_fs + 3
        y = sc_para(y, exp_text)
        y += 4

    if skills:
        c.setFillColor(TEAL)
        c.setFont("Helvetica", lbl_fs)
        c.drawString(LMAR, cy(y, lbl_fs), "SKILLS")
        y += lbl_fs + 3
        y = sc_para(y, skill_text)
        y += 6

    # ── WORK HISTORY ─────────────────────────────────────────────
    y = sc_section_header(y, "Work History")

    jobs = data.get("work_history", [])
    for i, job in enumerate(jobs):
        y = sc_check_page(y, needed=70)

        tenure     = fmt_date(job.get("dates", ""))
        role_avail = CTW - c.stringWidth(tenure, "Helvetica", FS_CODATE) - 8

        style_role = ParagraphStyle("sr", fontName="Helvetica-Bold", fontSize=FS_ROLE,
                                    leading=FS_ROLE * 1.3, textColor=ROLE_BOLD)
        p_role = Paragraph(_safe_html(job["role"]), style_role)
        _, role_h = p_role.wrap(role_avail, PH)
        p_role.drawOn(c, LMAR, cy(y) - role_h)
        c.setFillColor(TENURE_GREY)
        c.setFont("Helvetica", FS_CODATE)
        c.drawRightString(CRIGHT, cy(y, FS_CODATE), tenure)
        y += max(role_h, FS_CODATE) + 2

        _loc = job.get("location", "")
        _co_loc = f"{job['company']}, {_loc}" if _loc else job['company']
        y = sc_para(y, _co_loc, fs=FS_CODATE, italic=True)
        y += 2

        # Optional featured link (e.g. published article about this role's work)
        fl = job.get("featured_link")
        if fl and fl.get("text"):
            fl_text = fl["text"]
            fl_url  = fl.get("url", "")
            fsize   = 7.5
            c.setFillColor(TEAL)
            c.setFont("Helvetica-Oblique", fsize)
            c.drawString(LMAR, cy(y, fsize), fl_text)
            if fl_url:
                tw = c.stringWidth(fl_text, "Helvetica-Oblique", fsize)
                yc = cy(y, fsize)
                c.linkURL(fl_url, (LMAR, yc - 2, LMAR + tw, yc + fsize))
            y += fsize * 1.6 + 1

        for bullet in job.get("bullets", []):
            y = sc_check_page(y, needed=28)
            y = sc_bullet(y, bullet)

        # Thin divider between entries (helps ATS parsers identify entry boundaries)
        if i < len(jobs) - 1:
            y += 4
            c.setFillColor(DIVIDER)
            c.rect(LMAR, cy(y), CTW, 0.4, fill=1, stroke=0)
            y += 5
        else:
            y += 7

    # ── EDUCATION ────────────────────────────────────────────────
    y = sc_check_page(y, needed=50)
    y = sc_section_header(y, "Education")

    for edu in data.get("education", []):
        y = sc_para(y, edu["degree"], fs=FS_ROLE, color=ROLE_BOLD, bold=True)
        detail = edu["institution"]
        if edu.get("dates"): detail += f"  ·  {edu['dates']}"
        if edu.get("gpa"):   detail += f"  ·  GPA: {edu['gpa']}"
        y = sc_para(y, detail, fs=FS_CODATE, italic=True)
        y += 4

    # ── CERTIFICATIONS (one per line — ATS safe, readable) ──────
    if data.get("certifications"):
        y = sc_check_page(y, needed=50)
        y = sc_section_header(y, "Certifications")
        certs = data["certifications"]
        claude_certs = [ct for ct in certs if "claude" in ct.lower() or "anthropic" in ct.lower()]
        other_certs  = [ct for ct in certs if ct not in claude_certs]
        for cert in claude_certs + other_certs:
            y = sc_check_page(y, needed=20)
            y = sc_para(y, cert, fs=8.5, color=DARK)
            y += 2

    c.save()
    _check_page_count(output_path)
    print(f"[pdf_renderer] Resume (single-column) → {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# RESUME
# ─────────────────────────────────────────────────────────────────────────────

def _check_page_count(path: str, max_pages: int = 2):
    """Warn if rendered PDF exceeds max_pages."""
    try:
        data = open(path, "rb").read()
        pages = len(re.findall(rb'/Type\s*/Page[^s]', data))
        if pages > max_pages:
            print(f"[pdf_renderer] ⚠ RESUME IS {pages} PAGES (limit: {max_pages}). "
                  f"Trim bullets from earliest role (Coviam) and re-render.")
    except Exception:
        pass


def build_resume(data: dict, output_path: str, layout: str = "single"):
    _validate_doc(data, "resume")
    if layout != "two_column":
        return _build_single_column(data, output_path)
    c = rl_canvas.Canvas(output_path, pagesize=A4)
    c.setTitle("Vinay Patidar — CV")
    c.setAuthor("Vinay Patidar")
    c.setCreator("Vinay Patidar")
    c.setSubject("Analytics Professional")
    draw_sidebar_bg(c)

    # ── LEFT COLUMN ──────────────────────────────────────────────────────────
    y = 14.0

    # Name — 22.86pt white Regular
    c.setFillColor(WHITE)
    c.setFont("Helvetica", FS_NAME)
    c.drawString(SPAD, cy(y, FS_NAME), data["name"])
    y += FS_NAME + 9                          # 9pt gap name → profession

    # Profession lines — 10.23pt white, wrapped within sidebar
    for line in data.get("title_lines", []):
        y = sb_wrapped(c, y, line, size=FS_PROF)
    y += 5

    # CONTACT
    y = draw_section_bar(c, y, "Contact")
    y += 7
    contact = data.get("contact", {})
    for label, value in [
        ("Email",    contact.get("email","")),
        ("Phone",    contact.get("phone","")),
        ("Address",  contact.get("address","")),
        ("LinkedIn", contact.get("linkedin","")),
    ]:
        if not value: continue
        y = sb_line(c, y, label, size=FS_SLBL)   # Regular (not bold) — matches PDF
        y -= 3
        if label == "Address":
            parts = [p.strip() for p in value.split(",",1)]
            for i,pt in enumerate(parts):
                suffix = "," if i==0 and len(parts)>1 else ""
                y = sb_wrapped(c, y, pt+suffix, size=FS_SVAL)
        else:
            y = sb_wrapped(c, y, value, size=FS_SVAL)
        y += 3
    y += 4

    # CORE EXPERTISE
    y = draw_section_bar(c, y, "Core Expertise")
    y += 7
    for item in data.get("core_expertise", []): y = sb_wrapped(c, y, item, size=FS_SITEM)
    y += 6

    # SKILLS
    y = draw_section_bar(c, y, "Skills")
    y += 7
    for skill in data.get("skills", []): y = sb_line(c, y, skill, size=FS_SITEM)
    y += 6

    # CERTIFICATES — claude certs first, then rest
    if data.get("certifications"):
        y = draw_section_bar(c, y, "Certificates")
        y += 7
        certs = data["certifications"]
        # Split claude vs others
        claude_certs = [c2 for c2 in certs if "claude" in c2.lower() or "anthropic" in c2.lower()]
        other_certs  = [c2 for c2 in certs if c2 not in claude_certs]
        ordered = claude_certs + other_certs
        for cert in ordered:
            nm, pv = (cert.split(" — ",1) if " — " in cert
                      else cert.split(" - ",1) if " - " in cert
                      else (cert,""))
            y = sb_wrapped(c, y, nm, size=FS_CLLBL)
            y -= 3
            if pv: y = sb_wrapped(c, y, pv, size=FS_CLVAL)
            y += 5

    # ── RIGHT COLUMN ─────────────────────────────────────────────────────────
    yr = 14.0

    # Summary — 7.82pt Regular dark
    if data.get("summary"):
        yr = rc_para(c, RX+RPAD, yr, data["summary"], FS_BULLET, DARK, tw=RTW)
        yr += 5

    # Work History
    yr = divider_line(c, yr)
    yr = rc_section_header(c, yr, "Work History")
    yr = divider_line(c, yr)
    yr += 5

    for job in data.get("work_history", []):
        yr = check_page(c, yr, needed=70)

        tenure = fmt_date(job.get("dates",""))

        # Row: role title left (bold, lighter dark), tenure right-aligned grey
        role_avail = ROLE_TW - c.stringWidth(tenure, "Helvetica", FS_CODATE) - 8
        style_role = ParagraphStyle("r", fontName="Helvetica-Bold", fontSize=FS_ROLE,
                                    leading=FS_ROLE*1.3, textColor=ROLE_BOLD)
        p_role = Paragraph(job["role"], style_role)
        _, role_h = p_role.wrap(role_avail, PH)
        p_role.drawOn(c, ROLE_X, cy(yr) - role_h)
        # Tenure right-aligned, lighter grey
        c.setFillColor(TENURE_GREY)
        c.setFont("Helvetica", FS_CODATE)
        c.drawRightString(RRIGHT, cy(yr, FS_CODATE), tenure)
        yr += max(role_h, FS_CODATE) + 2

        # Company italic below role
        yr = rc_para(c, ROLE_X, yr, f"{job['company']}, {job.get('location','')}",
                     FS_CODATE, DARK, italic=True, tw=ROLE_TW)
        yr += 2

        for bullet in job.get("bullets",[]):
            yr = check_page(c, yr, needed=28)
            yr = rc_bullet(c, yr, bullet)
        yr += 7

    # Education
    yr = check_page(c, yr, needed=50)
    yr = divider_line(c, yr)
    yr = rc_section_header(c, yr, "Education")
    yr = divider_line(c, yr)
    yr += 5
    for edu in data.get("education",[]):
        yr = rc_para(c, ROLE_X, yr, edu["degree"], FS_ROLE, ROLE_BOLD, bold=True, tw=ROLE_TW)
        detail = edu["institution"]
        if edu.get("dates"): detail += f" · {edu['dates']}"
        if edu.get("gpa"):   detail += f" · GPA: {edu['gpa']}"
        yr = rc_para(c, ROLE_X, yr, detail, FS_CODATE, DARK, italic=True, tw=ROLE_TW)
        yr += 4

    c.save()
    _check_page_count(output_path)
    print(f"[pdf_renderer] Resume → {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# COVER LETTER
# ─────────────────────────────────────────────────────────────────────────────

def _build_single_column_cover_letter(data: dict, output_path: str):
    """Single-column cover letter — matches the single-column resume header exactly.
    ATS-safe: no sidebar, no columns, clean full-width text flow."""
    c = rl_canvas.Canvas(output_path, pagesize=A4)
    c.setTitle("Vinay Patidar — Cover Letter")
    c.setAuthor("Vinay Patidar")
    c.setCreator("Vinay Patidar")
    c.setSubject("Cover Letter")

    LMAR   = 40.0
    RMAR   = 40.0
    CTW    = PW - LMAR - RMAR
    CRIGHT = PW - RMAR
    CL_FS  = 10.0
    CL_LEAD = CL_FS * 1.65
    MID    = colors.Color(0.30, 0.30, 0.30)

    contact = data.get("contact", {})

    # ── HEADER (identical to single-column resume) ────────────────────────────
    y = 18.0

    # Contact lines drawn FIRST in PDF stream (stream order fix — see resume header).
    contact_lines = [v for v in [
        contact.get("email", ""),
        contact.get("phone", ""),
        contact.get("linkedin", ""),
        contact.get("github", ""),
        contact.get("address", ""),
    ] if v]
    yc = y
    for cl in contact_lines:
        c.setFillColor(TENURE_GREY)
        c.setFont("Helvetica", FS_SVAL)
        c.drawRightString(CRIGHT, cy(yc, FS_SVAL), cl)
        yc += FS_SVAL * 1.4  # 1.4× keeps 5 contact lines within title-block height

    # Name — left, bold, dark
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", FS_NAME)
    c.drawString(LMAR, cy(y, FS_NAME), data["name"])
    name_bottom = y + FS_NAME  # visual bottom of name in top-down coords

    # Pre-compute rule_y so title_lines can be centred in the available space.
    title_lines_list = data.get("title_lines", [])
    N = len(title_lines_list)
    title_content_h = ((N - 1) * FS_PROF * 1.5 + FS_PROF) if N > 0 else 0
    min_gap = 5
    rule_y = max(name_bottom + min_gap + title_content_h + min_gap, yc + 2)

    # Draw title lines — centred between name bottom and rule (equal gap above and below).
    if N > 0:
        equal_gap = (rule_y - name_bottom - title_content_h) / 2
        ty = name_bottom + equal_gap
        c.setFillColor(TEAL)
        c.setFont("Helvetica", FS_PROF)
        for tl in title_lines_list:
            c.drawString(LMAR, cy(ty, FS_PROF), tl)
            ty += FS_PROF * 1.5

    y = rule_y

    # Teal rule (same 2pt rule as resume)
    c.setFillColor(TEAL)
    c.rect(LMAR, cy(y), CTW, 2.0, fill=1, stroke=0)
    y += 14

    # ── INNER HELPERS ─────────────────────────────────────────────────────────

    def cl_line(text, y_top, color=None, fs=None):
        fsize = fs or CL_FS
        col   = color or DARK
        c.setFillColor(col)
        c.setFont("Helvetica", fsize)
        c.drawString(LMAR, cy(y_top, fsize), text)
        return y_top + fsize * 1.6

    def cl_para(text, y_top):
        style = ParagraphStyle("clp", fontName="Helvetica", fontSize=CL_FS,
                               leading=CL_LEAD, textColor=DARK)
        p = Paragraph(_safe_html(text), style)
        _, h = p.wrap(CTW, PH)
        p.drawOn(c, LMAR, cy(y_top) - h)
        return y_top + h

    # ── DATE + RECIPIENT + SALUTATION ─────────────────────────────────────────
    y = cl_line(data.get("date", ""), y, color=MID, fs=CL_FS * 0.9)
    y += CL_FS * 0.4
    y = cl_line(data.get("recipient", ""), y, color=DARK)
    y += CL_FS * 0.2
    y = cl_line(data.get("salutation", "Dear Hiring Team,"), y, color=DARK)
    y += CL_FS * 1.8

    # ── PARAGRAPHS ────────────────────────────────────────────────────────────
    for para in data.get("paragraphs", []):
        y = cl_para(para, y)
        y += CL_FS * 1.5

    # ── CLOSING SIGNATURE ─────────────────────────────────────────────────────
    y += CL_FS * 0.5
    y = cl_line(data.get("closing", "Kind regards,"), y, color=MID)
    y = cl_line(data["name"], y, color=DARK)
    for val in [contact.get("phone", ""), contact.get("email", "")]:
        if val:
            y = cl_line(val, y, color=MID, fs=CL_FS * 0.95)

    c.save()
    print(f"[pdf_renderer] Cover letter (single-column) → {output_path}")


def build_cover_letter(data: dict, output_path: str, layout: str = "single"):
    _validate_doc(data, "cover")
    if layout != "two_column":
        _build_single_column_cover_letter(data, output_path)
        return

    c = rl_canvas.Canvas(output_path, pagesize=A4)
    c.setTitle("Vinay Patidar — Cover Letter")
    c.setAuthor("Vinay Patidar")
    c.setCreator("Vinay Patidar")
    c.setSubject("Cover Letter")
    draw_sidebar_bg(c)

    # ── LEFT COLUMN ──────────────────────────────────────────────────────────
    y = 14.0
    c.setFillColor(WHITE)
    c.setFont("Helvetica", FS_NAME)
    c.drawString(SPAD, cy(y, FS_NAME), data["name"])
    y += FS_NAME + 10

    for line in data.get("title_lines", []):
        y = sb_wrapped(c, y, line, size=FS_PROF)
    y += 8

    y = draw_section_bar(c, y, "Contact")
    y += 8
    contact = data.get("contact", {})
    for label, value in [
        ("Email",    contact.get("email","")),
        ("Phone",    contact.get("phone","")),
        ("Address",  contact.get("address","")),
        ("LinkedIn", contact.get("linkedin","")),
    ]:
        if not value: continue
        y = sb_line(c, y, label, size=FS_SLBL)
        y -= 3
        y = sb_wrapped(c, y, value, size=FS_SVAL)
        y += 5

    # ── RIGHT COLUMN ─────────────────────────────────────────────────────────
    CL_FS   = 10.0
    CL_LEAD = CL_FS * 1.65
    MID     = colors.Color(0.30, 0.30, 0.30)

    def cl_line(text, y_top, italic=False, color=None, fs=None):
        fsize = fs or CL_FS
        col   = color or MID
        fn    = "Helvetica-Oblique" if italic else "Helvetica"
        c.setFillColor(col)
        c.setFont(fn, fsize)
        c.drawString(RX+RPAD, cy(y_top, fsize), text)
        return y_top + fsize * 1.6

    def cl_para(text, y_top, color=None, fs=None):
        fsize  = fs or CL_FS
        col    = color or MID
        style  = ParagraphStyle("cl", fontName="Helvetica", fontSize=fsize,
                                leading=fsize*1.65, textColor=col)
        p = Paragraph(text, style)
        w, h = p.wrap(RTW, PH)
        p.drawOn(c, RX+RPAD, cy(y_top) - h)
        return y_top + h

    yr = 20.0
    yr = cl_line(data.get("date",""),      yr, color=MID)
    yr = cl_line(data.get("recipient",""), yr, color=DARK)
    yr += CL_FS * 0.3
    yr = cl_line(data.get("salutation","Dear Hiring Team,"), yr, color=DARK)
    yr += CL_FS * 1.6

    for para in data.get("paragraphs",[]):
        yr = cl_para(para, yr)
        yr += CL_FS * 1.5

    # Safety net: ensure closing block always renders within page bounds
    yr = min(yr + CL_FS * 0.8, PH - 20 - 72)
    yr = cl_line(data.get("closing","Kind regards,"), yr, color=MID)
    yr = cl_line(data["name"], yr, color=DARK)
    for val in [contact.get("phone",""), contact.get("email","")]:
        if val:
            yr = cl_line(val, yr, color=MID, fs=CL_FS*0.95)

    c.save()
    print(f"[pdf_renderer] Cover letter → {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# SAMPLE DATA
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_RESUME = {
    "name": "Vinay Patidar",
    "title_lines": ["Lead Product, Growth &", "Commercial Analytics Professional"],
    "contact": {
        "email":    "vinay_patidar02@yahoo.com",
        "phone":    "+91 XXXXX XXXXX",
        "linkedin": "linkedin.com/in/vinay-patidar-vp02",
        "github":   "github.com/vinaypatidar02",
        "address":  "Bengaluru, Karnataka 560035",
    },
    "core_expertise": [
        "Product Analytics","Growth Analytics",
        "Experimentation & A/B Testing",
        "Pricing & Commercial Optimization",
        "Customer Lifecycle Analytics",
        "KPI Strategy & Business Intelligence",
        "Strategic Decision-Making",
        "Stakeholder Management","Analytics Transformation",
    ],
    "skills": ["SQL","Tableau","Python","Looker Studio","BigQuery","Amazon Redshift"],
    "summary": (
        "Lead Product & Commercial Analytics professional with 8+ years of experience "
        "driving experimentation, customer growth, strategic decision-making, and pricing "
        "optimisation across ecommerce and technology businesses. Proven track record of "
        "building scalable KPI frameworks, leading cross-functional analytics initiatives, "
        "and delivering measurable business impact. Experienced in partnering with product, "
        "growth, operations, and leadership teams to solve complex business problems in "
        "high-growth environments."
    ),
    "work_history": [
        {
            "company":"Flipkart (Ecommerce)","location":"Bengaluru",
            "role":"Lead Business Analyst","dates":"2025-04-2026-03",
            "bullets":[
                "Led CRM analytics, incrementality testing, and experimentation for customer growth, driving 40% growth in Grocery visits and improved cohort monetisation.",
                "Developed customer propensity models capturing 70% of potential Grocery customers while targeting only 30% of the user base.",
                "Built automated KPI frameworks and scalable reporting standards improving decision-making consistency across product and growth teams.",
                "Managed analysts and drove cross-functional experimentation, behavioural analytics, and commercial modelling to optimise reseller behaviour and pricing elasticity.",
            ],
        },
        {
            "company":"BeepKart (Used 2W)","location":"Bengaluru",
            "role":"Analytics Manager","dates":"2023-07-2024-11",
            "featured_link": {
                "text": "Dynamic Pricing Algorithm — Published by BeepKart COO on Medium",
                "url":  "https://medium.com/@abhisheksaraf_44597/the-art-and-science-of-dynamic-pricing-how-we-built-an-algorithm-for-450-vehicle-models-d691a09361d2",
            },
            "bullets":[
                "Designed a Dynamic Pricing Algorithm reducing inventory holding from 40 to 25 days through optimised pricing and inventory decisions.",
                "Drove 30%+ improvement in procurement efficiency through geo-clustering, lead-density optimisation, and resource allocation modelling.",
                "Led and mentored a team of 5 analysts using Agile delivery practices (Jira/Confluence sprint planning), improving team throughput and delivery predictability across all analytics charters.",
            ],
        },
        {
            "company":"DeHaat (Agriculture Technology)","location":"Bengaluru",
            "role":"Lead Business Analyst","dates":"2021-07-2023-07",
            "bullets":[
                "Reduced overdue outstanding by 30% through data-driven collections prioritisation using customer risk segmentation.",
                "Developed customer scoring models improving engagement, targeting, and sales conversion. Led a team of 3 analysts.",
            ],
        },
        {
            "company":"Quinbay/Coviam Technology","location":"Bengaluru",
            "role":"Senior Data Analyst","dates":"2020-10-2021-07",
            "bullets":["Built search conversion tracking pipelines; conducted A/B tests via Firebase to optimise search and conversion."],
        },
        {
            "company":"Coviam Technology (Ecommerce)","location":"Bengaluru",
            "role":"Data Analyst","dates":"2017-05-2020-09",
            "bullets":[
                "Developed 150+ Tableau dashboards and 100+ data marts across 50+ microservices.",
                "Built an XGBoost delivery prediction model achieving 85% accuracy.",
            ],
        },
    ],
    "education": [{"degree":"Mining Engineering, B.Tech","institution":"IIT (BHU), Varanasi","dates":"2012 – 2016","gpa":"7.67/10"}],
    "certifications": [
        "Data Science using SAS and R — Analytix Labs",
        "Managing Big Data with MySQL — Coursera",
        "Data Visualization with Tableau — Coursera",
        "Mastering Data Analysis in Excel — Coursera",
        "Claude 101 — Anthropic Skilljar (2026)",
        "Claude Code 101 — Anthropic Skilljar (2026)",
    ],
}

SAMPLE_COVER = {
    "name": "Vinay Patidar",
    "title_lines": ["Lead Product, Growth &", "Commercial Analytics Professional"],
    "contact": {
        "email":    "vinay_patidar02@yahoo.com",
        "phone":    "+91 XXXXX XXXXX",
        "linkedin": "linkedin.com/in/vinay-patidar-vp02",
        "github":   "github.com/vinaypatidar02",
        "address":  "Bengaluru, Karnataka 560035",
    },
    "date":       "London, 2026-06-17",
    "recipient":  "Monzo Hiring Team",
    "salutation": "Dear Hiring Team,",
    "paragraphs": [
        "I am delighted to apply for the Analytics Manager role at Monzo. With over 8 years "
        "of experience across ecommerce and fintech-adjacent businesses, I have led analytics "
        "initiatives spanning customer growth, CRM experimentation, pricing optimisation, and "
        "KPI strategy. Monzo's customer-obsessed, data-driven culture is precisely the kind "
        "of environment where I do my best work.",

        "At Flipkart, I led CRM analytics and incrementality testing for Grocery growth — "
        "developing propensity models that identified 70% of high-potential customers while "
        "reaching only 30% of the base, and building automated KPI frameworks that gave "
        "leadership real-time visibility into cohort health and campaign ROI. At BeepKart, "
        "I designed a Dynamic Pricing Algorithm that reduced inventory holding from 40 to 25 "
        "days, and led a team of 5 analysts delivering experimentation and strategic insights "
        "in an Agile delivery model.",

        "Across my career I have consistently worked at the intersection of analytics, product "
        "strategy, and stakeholder management — mentoring teams, running cross-functional "
        "experimentation programmes, and translating complex data into decisions that move "
        "business metrics. I am also actively developing AI fluency through Anthropic's "
        "Claude Code programme, building agentic automation workflows that extend what "
        "analytics teams can deliver.",

        "What excites me most about Monzo is the scale of customer data and the genuine "
        "opportunity to improve people's financial lives through precision analytics. I would "
        "welcome the chance to discuss how my background in experimentation, lifecycle "
        "analytics, and team leadership can contribute to Monzo's next phase of growth. "
        "Thank you for your time and consideration.",
    ],
    "closing": "Kind regards,",
}

if __name__ == "__main__":
    # Auto-detect argument order: correct form is `mode json output`.
    # If argv[1] looks like a JSON path (ends in .json or contains a path separator
    # and isn't a known mode keyword), assume caller passed `json output mode` and swap.
    _argv = sys.argv[1:]
    _MODES = {"resume", "cover", "test"}
    if _argv and _argv[0] not in _MODES and (_argv[0].endswith(".json") or "/" in _argv[0]):
        if len(_argv) >= 3:
            _argv = [_argv[2], _argv[0], _argv[1]] + _argv[3:]
            print(f"[pdf_renderer] ⚠ arg order auto-corrected → mode={_argv[0]}")

    mode = _argv[0] if _argv else "test"
    if mode == "test":
        out = Path(__file__).parent.parent / "outputs" / "applications" / "_test_output"
        out.mkdir(parents=True, exist_ok=True)
        build_resume(SAMPLE_RESUME,      str(out / "sample_resume_single.pdf"))
        build_resume(SAMPLE_RESUME,      str(out / "sample_resume_two_col.pdf"), layout="two_column")
        build_cover_letter(SAMPLE_COVER, str(out / "sample_cover_letter.pdf"))
        print(f"\nOutputs → {out}")
    elif mode == "resume":
        with open(_argv[1]) as f: data = json.load(f)
        layout = _argv[3] if len(_argv) > 3 else "single"
        build_resume(data, _argv[2], layout=layout)
    elif mode == "cover":
        with open(_argv[1]) as f: data = json.load(f)
        build_cover_letter(data, _argv[2])
