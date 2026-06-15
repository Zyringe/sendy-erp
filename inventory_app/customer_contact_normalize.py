# customer_contact_normalize.py
# Pure-Python normalizer for messy customer contact data (name / phone / contact / address).
# Conservative auto/review split; losslessness is the whole point (every >=7-digit run in the
# original must survive into the proposal).
#
# Reuses customer_geo.region_of for province->region (do NOT reimplement province logic).
# Python 3.9 — no `X | None`; use Optional[...].

import re
from typing import Optional

from customer_geo import region_of


# ---------------------------------------------------------------------------
# Vocab / markers (case-insensitive; with or without trailing colon; glued or spaced)
# ---------------------------------------------------------------------------
# FAX markers — longest first so "แฟกซ์" matches before "F".
_FAX_MARKERS = ["แฟกซ์", "แฟ็กซ์", "fax", "f"]
# LINE markers
_LINE_MARKERS = ["id line", "line", "ไลน์"]
# PHONE-LABEL noise (strip; the following number is a normal phone). Longest first.
_PHONE_LABELS = ["มือถือ", "เบอร์", "phone", "โทร.", "โทร", "tel", "ph", "t"]

# PERSON / honorific markers (glued or spaced before a name/number)
_PERSON_MARKERS = [
    "เถ้าแก่", "เฮีย", "ซ้อ", "เจ๊", "เจ้", "พี่", "น้อง", "คุณ", "คุน", "อา",
    "ป้า", "ลุง", "น้า", "เสี่ย", "บัง", "หมวย", "เจ",
]

# NOTE words (non-person, non-phone — delivery schedules etc.)
_WEEKDAYS = [
    "จันทร์", "จัน", "อังคาร", "อัง", "พุธ", "พฤหัส", "พฤ",
    "ศุกร์", "ศุกร", "เสาร์", "อาทิตย์", "อา",
]
_NOTE_WORDS = _WEEKDAYS + ["วางบิล", "รับเช็ค", "วาง", "เก็บเช็ค", "เก็บเงิน"]

# Thai personal-name prefixes (for classify_name person detection)
_PERSON_NAME_PREFIXES = ["นางสาว", "น.ส.", "ด.ช.", "ด.ญ.", "นาย", "นาง", "คุณ", "คุน"]
# Company prefixes
_COMPANY_PREFIXES = [
    "ห้างหุ้นส่วน", "บริษัท", "ร้าน", "หจก.", "หจก", "บจก.", "บจก",
    "ห้าง", "สหกรณ์",
]

# A "number token" — a run of digits/()/-/./space, length >= 6, starting+ending with a digit
# (parenthesized area code allowed at the front).
_NUM_RE = re.compile(r"\(?\d[\d().\- ]{4,}\d")


# ---------------------------------------------------------------------------
# Thai-phone validation
# ---------------------------------------------------------------------------
def is_valid_thai_phone(token):
    """Valid iff the core (after stripping a trailing -NN/-N extension) starts with 0
    and is 9 (landline) or 10 (mobile) digits. Parenthesized area codes count.
    """
    if token is None:
        return False
    s = token.strip()
    if not s:
        return False
    # Drop a trailing extension group: a final -NN / -N after the core, but only if the
    # resulting core is a valid 9-10 digit 0-led number.
    m = re.match(r"^(.*\d)-(\d{1,2})$", s)
    if m:
        core = re.sub(r"\D", "", m.group(1))
        if len(core) in (9, 10) and core.startswith("0"):
            return True
    core = re.sub(r"\D", "", s)
    return len(core) in (9, 10) and core.startswith("0")


def _landline_core_digits(token):
    """Digits of a landline after dropping a trailing -NN/-N extension (when that leaves a
    valid 9/10-digit 0-led core); else the raw digits."""
    s = (token or "").strip()
    m = re.match(r"^(.*\d)-(\d{1,2})$", s)
    if m:
        core = re.sub(r"\D", "", m.group(1))
        if len(core) in (9, 10) and core.startswith("0"):
            return core
    return re.sub(r"\D", "", s)


def _landline_area(token):
    """Area code of a 9-digit landline: '02' for Bangkok, else the 3-digit '0XX'. None if the
    token isn't a 9-digit landline (mobiles return None — they don't seed continuations)."""
    d = _landline_core_digits(token)
    if len(d) == 9 and d.startswith("0"):
        return "02" if d.startswith("02") else d[:3]
    return None


def _modernize_digits(d):
    """The 2006 mobile renumbering inserted '8' after the leading 0: 9-digit 0[1/6/8/9]-XXXXXXX
    old mobiles -> 10-digit 08X-XXXXXXX. Landline areas (02, 03X, 04X, 05X, 07X) are NOT touched.
    Returns the modernized digit string, or None if `d` isn't an old mobile."""
    if len(d) == 9 and d.startswith("0") and d[1] in "1689":
        return "0" + "8" + d[1:]
    return None


def _modernize_mobile(num):
    """Return (display_number, was_old). Converts an old 9-digit mobile to the modern 08X format
    (kept hyphenated 0XX-XXXXXXX); leaves anything else untouched."""
    d = re.sub(r"\D", "", num or "")
    md = _modernize_digits(d)
    if md:
        return md[:3] + "-" + md[3:], True
    return num, False


# A comma-chunk that is purely a number (digits, hyphens, and parens — e.g. "(034)234330").
_PURE_NUM_RE = re.compile(r"^\(?\d[\d\-()]*\d$")


def _expand_continuations(chunks):
    """Thai shorthand: a bare local number listed after a full landline shares its area code.
    '02-2114322,2114125'       -> ['02-2114322','02-2114125']
    '043-519373,512324,525408' -> ['043-519373','043-512324','043-525408']
    Only PURE numeric comma-chunks are touched (anything with text is left for review).
    Returns (new_chunks, set_of_inferred_full_numbers).
    """
    last_area = None
    new_chunks = []
    inferred = set()
    for ch in chunks:
        c = ch.strip()
        if not c or not _PURE_NUM_RE.match(c):
            new_chunks.append(ch)
            continue
        cd = re.sub(r"\D", "", c)
        if not c.lstrip("(").startswith("0"):
            # bare local number (no leading 0) — a continuation candidate
            if last_area and 6 <= len(cd) <= 7 and len(last_area) + len(cd) == 9:
                full = last_area + "-" + cd
                inferred.add(full)
                new_chunks.append(full)
                continue
            new_chunks.append(ch)
            continue
        # full number with a leading 0 — update the running area code if it's a landline
        area = _landline_area(c)
        if area:
            last_area = area
        new_chunks.append(ch)
    return new_chunks, inferred


def _is_number_token(tok):
    """True if tok looks like a (possibly multi-part) number run >= 6 chars."""
    if not tok:
        return False
    t = tok.strip()
    # must contain >= 6 digits-ish and match the number shape over its whole length region
    digits = re.sub(r"\D", "", t)
    if len(digits) < 6:
        return False
    return bool(_NUM_RE.fullmatch(t) or _NUM_RE.search(t) and re.fullmatch(r"[\d().\- ]+", t))


# ---------------------------------------------------------------------------
# Marker matching helpers
# ---------------------------------------------------------------------------
def _strip_leading_marker(text, markers):
    """If text starts (case-insensitively) with any marker (optionally followed by ':'
    and/or spaces), return (matched_marker, remainder). Else (None, text)."""
    low = text.lower()
    for mk in markers:
        if low.startswith(mk):
            rest = text[len(mk):]
            # consume an optional colon and surrounding spaces
            rest = re.sub(r"^\s*:?\s*", "", rest)
            return mk, rest
    return None, text


def _find_marker_anywhere(text, markers):
    """Find the earliest marker occurrence in text (case-insensitive). Returns
    (marker, start, end_after_colon) or None."""
    low = text.lower()
    best = None
    for mk in markers:
        idx = low.find(mk)
        if idx != -1:
            end = idx + len(mk)
            # consume optional colon/space after the marker
            m = re.match(r"\s*:?\s*", text[end:])
            end += m.end() if m else 0
            if best is None or idx < best[1]:
                best = (mk, idx, end)
    return best


def _starts_with_person(text):
    """Return the person marker if text starts with one (glued or spaced), else None."""
    for mk in _PERSON_MARKERS:
        if text.startswith(mk):
            return mk
    return None


# Exact weekday words (must match the WHOLE token — never a substring, so the name "พุธิตา"
# is not eaten by the weekday "พุธ").
_WEEKDAY_SET = set(_WEEKDAYS)
# Delivery-action prefixes: a token that STARTS with one of these is a note (e.g. "วางจัน",
# "วางบิล", "เก็บเช็ค"). These are verbs, not names.
_NOTE_ACTION_PREFIXES = ["วางบิล", "วาง", "รับเช็ค", "เก็บเช็ค", "เก็บเงิน", "เก็บ"]


def _is_note_word(token):
    """A single token is a note word iff it is exactly a weekday, or it starts with a known
    delivery-action prefix. Substring weekday matches are deliberately rejected."""
    t = token.strip().strip("()").strip(".")
    if not t:
        return False
    if t in _WEEKDAY_SET:
        return True
    return any(t.startswith(pre) for pre in _NOTE_ACTION_PREFIXES)


def _is_note_chunk(chunk):
    """A chunk is a note if it has no >=6-digit number and every token is a note word
    (or a short numeric like a date range '25-30')."""
    c = chunk.strip()
    if not c:
        return False
    if re.search(r"\d{6,}", re.sub(r"\D", "", c)):
        return False
    toks = [t for t in re.split(r"[\s]+", c) if t]
    if not toks:
        return False
    note_hits = 0
    for t in toks:
        if _is_note_word(t):
            note_hits += 1
        elif re.fullmatch(r"[\d\-,]+", t.strip("()")):
            # short numeric like a date range "25-30" — neutral
            continue
        else:
            return False
    return note_hits >= 1


# ---------------------------------------------------------------------------
# parse_phone_block
# ---------------------------------------------------------------------------
def parse_phone_block(raw):
    """Parse one phone/contact field into typed buckets. See module docstring / contract."""
    out = {
        "phones": [], "faxes": [], "lines": [], "people": [],
        "notes": [], "undialable": [], "leftovers": [], "inferred": [], "clean": False,
    }
    if not raw or not str(raw).strip():
        return out

    text = str(raw).strip()
    # Comma is the primary separator. Keep each comma-chunk and process individually so that
    # space-attached people/notes/labels stay glued to their neighbour where the spec wants.
    chunks = [c for c in text.split(",")]
    # Thai shorthand: bare local numbers after a full landline inherit its area code.
    chunks, inferred = _expand_continuations(chunks)
    out["inferred"] = sorted(inferred)

    for chunk in chunks:
        _process_chunk(chunk, out)

    out["clean"] = (
        (bool(out["phones"]) or bool(out["faxes"]))
        and not out["people"] and not out["lines"]
        and not out["notes"] and not out["undialable"] and not out["leftovers"]
    )
    return out


def _classify_number(tok, out, fax=False, line=False):
    """Route a number token into the correct bucket."""
    tok = tok.strip()
    if not tok:
        return
    if fax:
        out["faxes"].append(tok)
    elif line:
        out["lines"].append(tok)
    elif is_valid_thai_phone(tok):
        out["phones"].append(tok)
    else:
        out["undialable"].append(tok)


def _process_chunk(chunk, out):
    """Process a single comma-delimited chunk, mutating out."""
    c = chunk.strip()
    if not c:
        return

    # 1) Whole-chunk note (delivery schedule words, no number)?
    if _is_note_chunk(c):
        out["notes"].append(c)
        return

    # 2) Leading FAX marker
    mk, rest = _strip_leading_marker(c, _FAX_MARKERS)
    if mk is not None and rest and _NUM_RE.search(rest):
        _emit_subtokens(rest, out, fax=True)
        return

    # 3) Leading LINE marker
    mk, rest = _strip_leading_marker(c, _LINE_MARKERS)
    if mk is not None:
        # the remainder may be "094-5477724,086-5675864 เจ๊นุช" style only within this chunk
        if rest and _NUM_RE.search(rest):
            # first number after Line -> line; rest handled by _emit_subtokens as line=False default?
            # Per spec: "Line 094-5477724" -> lines=['094-5477724']. Trailing non-number text -> people.
            _emit_line_chunk(rest, out)
            return
        elif rest:
            # Line with a non-numeric id -> keep whole as a line entry
            out["lines"].append(rest.strip())
            return

    # 4) Leading PERSON marker (glued or spaced) -> the whole chunk (incl any number) is a person
    if _starts_with_person(c):
        out["people"].append(c)
        return

    # 5) Plain text chunk with NO phone-length number -> a single name/person entry, kept whole
    #    (so a multi-word name like "นางบรรจงศรี ชัยวัฒน์" stays as one people token, not scattered).
    if not re.search(r"\d{6,}", re.sub(r"\D", "", c)):
        out["people"].append(c)
        return

    # 6) Parenthetical-only person tag e.g. "(ซ้อ)" possibly followed/preceded by a number
    #    handled inside _emit_subtokens via space splitting.
    _emit_subtokens(c, out)


def _emit_line_chunk(rest, out):
    """Within a 'Line ...' chunk: pull numbers as line ids, route trailing text appropriately.
    Spec example: 'Line 094-5477724,086-5675864 เจ๊นุช' — but commas already split upstream,
    so here rest is e.g. '094-5477724' or '086-5675864 เจ๊นุช'."""
    # the first number in this chunk is a Line id; any further space-split tokens follow normal rules
    parts = _split_space_keep(rest)
    first_number_done = False
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if _looks_like_number(p):
            if not first_number_done:
                out["lines"].append(p)
                first_number_done = True
            else:
                _classify_number(p, out)
        elif _starts_with_person(p) or _is_thai_personal(p):
            out["people"].append(p)
        elif _is_note_chunk(p):
            out["notes"].append(p)
        else:
            out["people"].append(p)


def _is_bare_fax_marker(tok):
    """True if tok is a fax marker alone (optionally with a trailing colon/dot), e.g.
    'Fax', 'F', 'F:', 'Fax.' — i.e. the number follows in a separate space-token."""
    t = tok.strip()
    low = t.lower()
    for mk in _FAX_MARKERS:
        if low == mk or low == mk + ":" or low == mk + "." or low == mk + ".:":
            return True
    return False


def _emit_subtokens(text, out, fax=False):
    """Split a chunk on spaces and route each space-token. Handles label-stripping, glued
    person+number, paren person tags, spaced fax markers, and trailing notes/people."""
    parts = _split_space_keep(text)
    i = 0
    pending_fax = False  # a spaced fax marker armed the next number as a fax
    while i < len(parts):
        p = parts[i].strip()
        if not p:
            i += 1
            continue

        # a bare/spaced fax marker token ("Fax", "F", "F:", "Fax.") -> arm next number as fax
        if _is_bare_fax_marker(p):
            pending_fax = True
            i += 1
            continue

        # a stray ":number" token left over from "Fax : 034..." splitting
        if pending_fax and re.match(r"^:?\s*", p) and _looks_like_number(p.lstrip(":").strip()):
            _classify_number(p.lstrip(":").strip(), out, fax=True)
            pending_fax = False
            i += 1
            continue

        # phone label (โทร / Tel / ...) -> strip; following number(s) are plain phones
        lmk, lrest = _strip_leading_marker(p, _PHONE_LABELS)
        if lmk is not None and (lrest == "" or _looks_like_number(lrest)):
            if lrest and _looks_like_number(lrest):
                _classify_number(lrest, out, fax=fax)
            i += 1
            continue

        # glued fax marker inside the chunk e.g. "Fax02-2234346"
        fmk, frest = _strip_leading_marker(p, _FAX_MARKERS)
        if fmk is not None and frest and _looks_like_number(frest):
            _classify_number(frest, out, fax=True)
            i += 1
            continue

        # glued/space person marker e.g. "เฮีย081-0433196" or "เฮีย" then number next
        pmk = _starts_with_person(p)
        if pmk is not None:
            # If a number is glued onto the marker -> single people token
            if _looks_like_number(p[len(pmk):]):
                out["people"].append(p)
                i += 1
                continue
            # spaced: "เฮีย 089-8048070" -> consume the next number too
            person = p
            if i + 1 < len(parts) and _looks_like_number(parts[i + 1].strip()):
                person = p + " " + parts[i + 1].strip()
                i += 1
            out["people"].append(person)
            i += 1
            continue

        # parenthetical person tag, optionally glued to a number e.g. "(ซ้อ)" or "(ซ้อ)081-8403831"
        m_paren = re.match(r"^\(([^()]*)\)(.*)$", p)
        if m_paren and not re.search(r"\d", m_paren.group(1)):
            tail = m_paren.group(2).strip()
            if tail and _looks_like_number(tail):
                # number belongs to this person -> keep glued
                out["people"].append(p)
            elif not tail:
                out["people"].append(p)
            else:
                out["people"].append(p)
            i += 1
            continue

        # number token
        if _looks_like_number(p):
            _classify_number(p, out, fax=(fax or pending_fax))
            pending_fax = False
            i += 1
            continue

        # note word (delivery schedule etc.)
        if _is_note_word(p):
            out["notes"].append(p)
            i += 1
            continue

        # a bare Thai word (no digits, not a note word) in a phone field is a contact name
        # / nickname -> people, not a note.
        if not re.search(r"\d", p):
            out["people"].append(p)
            i += 1
            continue

        # digit-bearing but not a clean number (e.g. "090-1402002(เจ)", "บ/ช088-9294811",
        # ";035-620243") -> leftover. Preserved, never dropped, and (unlike a true schedule
        # note) NOT auto-filed — it likely hides a real phone, so it forces review.
        out["leftovers"].append(p)
        i += 1


def _split_space_keep(text):
    return re.split(r"\s+", text.strip())


def _looks_like_number(tok):
    """A space-token that is a number run (>=6 digits, only number chars)."""
    t = tok.strip()
    if not t:
        return False
    digits = re.sub(r"\D", "", t)
    if len(digits) < 6:
        return False
    return bool(re.fullmatch(r"\(?[\d().\- ]+", t))


def _is_thai_personal(tok):
    """Heuristic: a bare Thai word with no digits that is not a note word -> treat as a person
    name token only when it starts with a person marker; otherwise leave to caller."""
    t = tok.strip()
    if not t or re.search(r"\d", t):
        return False
    if _starts_with_person(t):
        return True
    return False


# ---------------------------------------------------------------------------
# classify_name
# ---------------------------------------------------------------------------
def _detect_kind(name):
    n = name.strip()
    for pre in _COMPANY_PREFIXES:
        if n.startswith(pre):
            return "company"
    for pre in _PERSON_NAME_PREFIXES:
        if n.startswith(pre):
            return "person"
    return "unknown"


def classify_name(name):
    out = {"kind": "unknown", "cleaned_name": name or "",
           "nickname": None, "phone": None, "changed": False}
    if not name or not str(name).strip():
        out["cleaned_name"] = ""
        return out

    original = str(name)
    n = original.strip()
    out["kind"] = _detect_kind(n)

    # Find an embedded VALID phone substring. Only extract if is_valid_thai_phone.
    phone = None
    cleaned = n
    for m in _NUM_RE.finditer(n):
        cand = m.group(0).strip()
        # also try the parenthesized form e.g. "(081-4834024)"
        cand_stripped = cand.strip("()")
        if is_valid_thai_phone(cand) or is_valid_thai_phone(cand_stripped):
            phone = cand_stripped if is_valid_thai_phone(cand_stripped) else cand
            # remove the number (and a wrapping paren pair if present) from the name
            start, end = m.start(), m.end()
            # widen to swallow a wrapping "(...)"
            lo = start
            hi = end
            if lo - 1 >= 0 and n[lo - 1] == "(" and hi < len(n) and n[hi] == ")":
                lo -= 1
                hi += 1
            cleaned = (n[:lo] + n[hi:]).strip()
            break

    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    out["cleaned_name"] = cleaned
    out["phone"] = phone
    out["changed"] = (cleaned != original) or (phone is not None)
    return out


# ---------------------------------------------------------------------------
# normalize_address
# ---------------------------------------------------------------------------
def normalize_address(addr):
    raw = "" if addr is None else str(addr)
    collapsed = re.sub(r"\s+", " ", raw).strip()
    return {"address": collapsed, "region": region_of(raw)}


# ---------------------------------------------------------------------------
# normalize_customer
# ---------------------------------------------------------------------------
def _join_nonempty(parts, sep=","):
    return sep.join(p for p in parts if p)


def normalize_customer(row):
    name = row.get("name") or ""
    phone = row.get("phone") or ""
    contact = row.get("contact") or ""
    address = row.get("address") or ""

    original = {"name": row.get("name") or "" if False else (name),
                "phone": phone, "contact": contact, "address": address}
    # original must be VERBATIM copies (including original None->'' coercion kept as given)
    original = {
        "name": "" if row.get("name") is None else row.get("name"),
        "phone": "" if row.get("phone") is None else row.get("phone"),
        "contact": "" if row.get("contact") is None else row.get("contact"),
        "address": "" if row.get("address") is None else row.get("address"),
    }

    pb = parse_phone_block(phone)
    cb = parse_phone_block(contact)
    nm = classify_name(name)
    ad = normalize_address(address)

    issues = []

    # ----- proposed.phone : untagged dialable phones; old 01/06/08/09 mobiles modernized -----
    phones_out = []
    legacy_mobile = False
    for ph in pb["phones"]:
        mod, was_old = _modernize_mobile(ph)
        phones_out.append(mod)
        legacy_mobile = legacy_mobile or was_old
    proposed_phone = _join_nonempty(phones_out)

    # ----- proposed.fax : faxes from phone + faxes from contact -----
    proposed_fax = _join_nonempty(pb["faxes"] + cb["faxes"])
    if pb["faxes"]:
        issues.append("fax_in_phone")
    if cb["faxes"]:
        issues.append("fax_in_contact")

    # ----- proposed.contact : contact field's own person/name text (cleaned) + extracted
    #       people/lines/notes/undialable from the PHONE field -----
    contact_bits = []

    # contact field's own residual text (everything that's NOT a fax we already pulled).
    # Keep its people, lines (with markers), notes, undialable, AND its own phones (which make
    # it ambiguous -> review). We reconstruct from buckets to preserve order-ish but losslessly.
    contact_self = _contact_residual(contact, cb)
    if contact_self:
        contact_bits.append(contact_self)
    if cb["phones"]:
        issues.append("phone_in_contact")
    if cb["lines"]:
        issues.append("line_in_contact")
    # A plain name in contact (people with no digits) is expected/clean — only flag people that
    # carry a number (an actual annotation worth a human look).
    if any(re.search(r"\d", p) for p in cb["people"]):
        issues.append("person_in_contact")
    if cb["notes"]:
        issues.append("note_in_contact")
    if cb["undialable"]:
        issues.append("undialable_contact")
    if cb["leftovers"]:
        issues.append("leftover_in_contact")  # already inside contact_self; just flag

    # people/lines/undialable/leftovers pulled out of the PHONE field (notes -> note field)
    if pb["people"]:
        issues.append("person_in_phone")
        contact_bits.extend(pb["people"])
    if pb["lines"]:
        issues.append("line_in_phone")
        contact_bits.extend("Line:" + x for x in pb["lines"])
    if pb["notes"]:
        issues.append("note_in_phone")  # notes go to proposed.note, not contact
    if pb["undialable"]:
        issues.append("undialable_phone")
        contact_bits.extend(pb["undialable"])
    if pb["leftovers"]:
        issues.append("leftover_in_phone")  # digit-bearing junk that may hide a real phone
        contact_bits.extend(pb["leftovers"])

    # phone pulled out of the NAME field
    if nm["phone"]:
        issues.append("phone_in_name")
        # if the customer has no other phone, promote it; else keep with contact note
        if not proposed_phone:
            proposed_phone = nm["phone"]
        else:
            contact_bits.append(nm["phone"])
    if nm["changed"]:
        issues.append("name_changed")

    proposed_contact = " ".join(b for b in contact_bits if b).strip()

    # ----- note (billing/delivery schedules etc.) — its own field, kept out of contact -----
    proposed_note = " ".join(n for n in (pb["notes"] + cb["notes"]) if n).strip()

    # ----- nickname (advisory): first clear honorific token -----
    nickname = _first_nickname(pb["people"] + cb["people"])

    if legacy_mobile:
        issues.append("legacy_mobile")

    proposed = {
        "name": nm["cleaned_name"],
        "nickname": nickname,
        "phone": proposed_phone,
        "fax": proposed_fax,
        "contact": proposed_contact,
        "note": proposed_note,
        "address": ad["address"],
        "region": ad["region"],
    }

    # ----- confidence -----
    # Auto-safe = deterministic, lossless transforms only: dialable phones (incl. modern-format),
    # fax split out, inferred area-code continuations, and notes filed to the note field. A
    # human is needed for people (attribution), contact-borne phones/lines, undialable numbers,
    # name changes, or old-format mobiles (which may be dead).
    cb_people_have_number = any(re.search(r"\d", p) for p in cb["people"])
    pb_auto = (not pb["people"] and not pb["lines"]
               and not pb["undialable"] and not pb["leftovers"])
    cb_auto = (
        not cb_people_have_number and not cb["lines"]
        and not cb["undialable"] and not cb["phones"] and not cb["leftovers"]
    )
    inferred_any = bool(pb["inferred"]) or bool(cb["inferred"])
    if inferred_any:
        issues.append("inferred_area_code")

    confidence = "auto" if (
        pb_auto and cb_auto and not nm["changed"] and not legacy_mobile
    ) else "review"

    # dedupe issues preserving order
    seen = set()
    issues = [x for x in issues if not (x in seen or seen.add(x))]

    result = {
        "original": original,
        "proposed": proposed,
        "confidence": confidence,
        "issues": issues,
    }

    # ----- lossless guard -----
    if not lossless_ok(result):
        result["confidence"] = "review"
        if "lossless_risk" not in result["issues"]:
            result["issues"].append("lossless_risk")

    return result


def _contact_residual(contact_raw, cb):
    """Return the contact field's OWN text, minus any fax (split into proposed.fax) and any note
    (filed into proposed.note). Keeps names/people/phones exactly as written. Lossless + dup-safe:
    targets only the fax/note spans, so a number that is also a real phone is preserved."""
    s = _strip_fax_text(contact_raw, cb["faxes"]) if cb["faxes"] else (contact_raw or "").strip()
    for note in cb["notes"]:
        s = s.replace(note, " ", 1)
    if cb["notes"]:
        s = re.sub(r"\s{2,}", " ", s).strip(" ,")
    return s.strip()


def _strip_fax_text(contact_raw, faxes):
    """Remove each fax MARKER+number occurrence from contact_raw, return the leftover text.

    Targets the "<fax-marker> <number>" span, NOT every occurrence of the bare number — the
    same number can also appear as a real phone in the same field (e.g.
    '087-8471541,077-601057 F:077-601057'), and that phone copy must be preserved (lossless).
    """
    if not contact_raw:
        return ""
    s = contact_raw
    for fx in faxes:
        fx_esc = re.escape(fx)
        new_s = re.sub(r"(?i)(?:แฟกซ์|แฟ็กซ์|fax|f)\s*[:.]?\s*" + fx_esc, " ", s, count=1)
        if new_s == s:
            # marker not adjacent (rare) — remove a SINGLE bare occurrence so any duplicate stays
            new_s = s.replace(fx, " ", 1)
        s = new_s
    s = re.sub(r"\s{2,}", " ", s).strip(" ,")
    return s.strip()


# Thai combining marks (tone/vowel signs). A person marker followed by one of these is really
# part of a larger syllable (e.g. "เจ" inside the name "เจี๊ยบ"), NOT the standalone honorific —
# stripping it would leave broken orphaned vowel marks. The guard below rejects that case.
_THAI_COMBINING = set(
    "ัิีึืฺุู"
    "็่้๊๋์ํ๎"
)


def _first_nickname(people):
    for p in people:
        for mk in _PERSON_MARKERS:
            if not p.startswith(mk):
                continue
            after = p[len(mk):]
            # guard: marker glued into a larger syllable (e.g. "เจ" in "เจี๊ยบ") → not a nickname
            if after and after[0] in _THAI_COMBINING:
                continue
            # strip any attached number/punctuation; what remains is the nickname
            rest = re.sub(r"[\d().\-: ]+", "", after).strip()
            # drop any leading combining marks / non-letters left behind
            while rest and (rest[0] in _THAI_COMBINING or not rest[0].isalpha()):
                rest = rest[1:]
            if len(rest) >= 2:
                return rest
            # marker stood alone (e.g. "เฮีย 081-..."): the honorific itself is the nickname
            if not rest:
                return mk
            # 1-char leftover is degenerate → skip rather than show garbage
        # fall through to next person token
    return None


# ---------------------------------------------------------------------------
# Lossless invariant
# ---------------------------------------------------------------------------
# A digit-run = digits joined only by phone-internal punctuation ( - ( ) . / ), bounded by
# spaces, commas, Thai/Latin letters, or string ends. It must NOT bridge across a space or a
# comma — otherwise an adjacent year-suffix in the name + the first phone would merge into one
# fake run that exists nowhere as a real number (false lossless alarm).
_DIGIT_RUN_RE = re.compile(r"\d(?:[\d()./\-]*\d)?")


def _digit_runs(text, min_len=7):
    """Return the set of digit-only strings (length >= min_len) for each contiguous number token
    in text. Tokens are split on whitespace and commas (and any non phone-punctuation char)."""
    if not text:
        return set()
    runs = set()
    for m in _DIGIT_RUN_RE.finditer(text):
        digs = re.sub(r"\D", "", m.group(0))
        if len(digs) >= min_len:
            runs.add(digs)
    return runs


def lossless_ok(result):
    """Every digit-run of length >= 7 in the ORIGINAL (name+phone+contact) must survive in the
    proposal (phone+fax+contact+note+nickname+name). Tolerant of two intentional transforms:
    an inferred area-code prepend (original is a substring of the enriched number) and an old
    mobile modernized to 08X (the '8'-inserted form appears instead)."""
    orig = result["original"]
    prop = result["proposed"]
    orig_text = " ".join([
        orig.get("name") or "", orig.get("phone") or "",
        orig.get("contact") or "",
    ])
    prop_text = " ".join([
        prop.get("phone") or "", prop.get("fax") or "",
        prop.get("contact") or "", prop.get("note") or "",
        prop.get("nickname") or "", prop.get("name") or "",
    ])
    prop_digits_concat = re.sub(r"\D", "", prop_text)
    prop_runs = _digit_runs(prop_text, 7)

    for run in _digit_runs(orig_text, 7):
        if run in prop_runs:
            continue
        if run in prop_digits_concat:
            continue
        # an old mobile that was modernized: its '8'-inserted form is what survives
        modern = _modernize_digits(run)
        if modern and modern in prop_digits_concat:
            continue
        return False
    return True
