"""Tests for customer_contact_normalize — STRICT TDD, written first.

Run: cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_customer_contact_normalize.py -q
"""
import pytest

from customer_contact_normalize import (
    parse_phone_block,
    classify_name,
    normalize_address,
    normalize_customer,
    is_valid_thai_phone,
    lossless_ok,
)


# --------------------------------------------------------------------------
# is_valid_thai_phone — the validation oracle
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tok", [
    "02-7303880-1", "081-8048070", "025874466", "02-4727341-48",
    "(034)217756", "043-519373", "02-7303882", "034-872616",
    "086-3379146", "02-2155776", "035-629292-3", "065-9920959",
])
def test_valid_thai_phone(tok):
    assert is_valid_thai_phone(tok) is True


@pytest.mark.parametrize("tok", [
    "8112658", "3794746", "546-1768", "038-59", "512324", "525408",
    "4471787", "5614505", "391887", "281756", "3580", "2008", "1991",
    "00002", "4",
])
def test_invalid_thai_phone(tok):
    assert is_valid_thai_phone(tok) is False


# --------------------------------------------------------------------------
# parse_phone_block — bucket placement
# --------------------------------------------------------------------------
def test_pb_phone_plus_fax_colon():
    r = parse_phone_block("02-7303880-1,F:02-7303882")
    assert r["phones"] == ["02-7303880-1"]
    assert r["faxes"] == ["02-7303882"]
    assert r["people"] == [] and r["lines"] == [] and r["notes"] == []
    assert r["undialable"] == []
    assert r["clean"] is True


def test_pb_three_phones_one_fax():
    r = parse_phone_block("02-2142905,02-2154868,02-2169345,FAX:02-2155776")
    assert r["phones"] == ["02-2142905", "02-2154868", "02-2169345"]
    assert r["faxes"] == ["02-2155776"]
    assert r["clean"] is True


def test_pb_phone_fax_with_extension_ranges():
    r = parse_phone_block("02-4727341-48 F:02-4727349-50")
    assert r["phones"] == ["02-4727341-48"]
    assert r["faxes"] == ["02-4727349-50"]
    assert r["clean"] is True


def test_pb_glued_fax_no_colon():
    r = parse_phone_block("02-2230832 Fax02-2234346")
    assert r["phones"] == ["02-2230832"]
    assert r["faxes"] == ["02-2234346"]
    assert r["clean"] is True


def test_pb_phone_label_stripped():
    r = parse_phone_block("086-3379146 โทร 034-872616")
    assert r["phones"] == ["086-3379146", "034-872616"]
    assert r["faxes"] == []
    assert r["clean"] is True


def test_pb_person_with_number_stays_attached():
    r = parse_phone_block("035-629292-3, เฮีย 089-8048070,065-9920959")
    assert r["phones"] == ["035-629292-3", "065-9920959"]
    assert r["people"] == ["เฮีย 089-8048070"]
    assert r["clean"] is False


def test_pb_glued_people_and_plain_phone():
    r = parse_phone_block("เฮีย081-0433196,ซ้อ087-2030950,056-352038")
    assert r["people"] == ["เฮีย081-0433196", "ซ้อ087-2030950"]
    assert r["phones"] == ["056-352038"]
    assert r["clean"] is False


def test_pb_line_marker_and_trailing_person():
    r = parse_phone_block("Line 094-5477724,086-5675864 เจ๊นุช")
    assert r["lines"] == ["094-5477724"]
    assert r["phones"] == ["086-5675864"]
    assert r["people"] == ["เจ๊นุช"]
    assert r["clean"] is False


def test_pb_weekday_note():
    r = parse_phone_block("02-5262280  วางจัน,อัง,พฤ,ศุกร")
    assert r["phones"] == ["02-5262280"]
    note_blob = " ".join(r["notes"])
    assert "จัน" in note_blob and "ศุกร" in note_blob
    assert r["clean"] is False


def test_pb_paren_areacode_phones_plus_paren_person():
    r = parse_phone_block("(034)217756,034-218587,099-1955422 (ซ้อ)")
    assert r["phones"] == ["(034)217756", "034-218587", "099-1955422"]
    assert r["people"] == ["(ซ้อ)"]
    assert r["clean"] is False


def test_pb_continuation_locals_inherit_area():
    # Bare local numbers after a full landline share its area code (Thai shorthand).
    r = parse_phone_block("043-519373,512324,525408,F:043-518198")
    assert r["phones"] == ["043-519373", "043-512324", "043-525408"]
    assert r["undialable"] == []
    assert r["inferred"] == ["043-512324", "043-525408"]
    assert r["faxes"] == ["043-518198"]


def test_pb_continuation_bangkok():
    # The exact case Put flagged: 02-2114322,2114125 = two Bangkok numbers.
    r = parse_phone_block("02-2114322,2114125")
    assert r["phones"] == ["02-2114322", "02-2114125"]
    assert r["inferred"] == ["02-2114125"]
    assert r["undialable"] == []


def test_pb_continuation_paren_area():
    # Parenthesized provincial area code seeds the continuation too.
    r = parse_phone_block("(034)234330,391887,081-9425977")
    assert "034-391887" in r["phones"]
    assert "391887" not in r["undialable"]
    assert r["inferred"] == ["034-391887"]


def test_pb_lone_undialable():
    # No preceding landline to borrow an area code from → still undialable.
    r = parse_phone_block("3794746")
    assert r["undialable"] == ["3794746"]
    assert r["phones"] == []
    assert r["inferred"] == []
    assert r["clean"] is False


def test_pb_empty():
    r = parse_phone_block("")
    assert r["phones"] == [] and r["faxes"] == []
    assert r["clean"] is False  # nothing dialable present -> not a clean phone block


# --------------------------------------------------------------------------
# classify_name
# --------------------------------------------------------------------------
def test_name_person_with_embedded_phone():
    r = classify_name("คุณ งามจิต 081-8191774")
    assert r["kind"] == "person"
    assert r["cleaned_name"] == "คุณ งามจิต"
    assert r["phone"] == "081-8191774"
    assert r["changed"] is True


def test_name_company_branch_code_not_phone():
    r = classify_name("บจก. กรีนไดมอนด์(00002)")
    assert r["kind"] == "company"
    assert r["phone"] is None
    assert r["changed"] is False
    assert r["cleaned_name"] == "บจก. กรีนไดมอนด์(00002)"


def test_name_company_year_suffix_not_phone():
    r = classify_name("ร้าน ทิพย์วารี 2008")
    assert r["kind"] == "company"
    assert r["phone"] is None
    assert r["changed"] is False


def test_name_company_paren_year_not_phone():
    r = classify_name("หจก. นิววิเศษพาณิชย์(1991)")
    assert r["kind"] == "company"
    assert r["phone"] is None
    assert r["changed"] is False


def test_name_person_phone_in_parens():
    r = classify_name("คุณออย(081-4834024)")
    assert r["kind"] == "person"
    assert r["phone"] == "081-4834024"
    assert r["changed"] is True


def test_name_plain_company_unchanged():
    r = classify_name("บริษัท ทรัพย์ทวีสิน จำกัด")
    assert r["kind"] == "company"
    assert r["changed"] is False
    assert r["phone"] is None


# --------------------------------------------------------------------------
# normalize_address
# --------------------------------------------------------------------------
def test_address_whitespace_collapse_and_region():
    r = normalize_address("123  ถ.สุขุมวิท   กรุงเทพมหานคร")
    assert r["address"] == "123 ถ.สุขุมวิท กรุงเทพมหานคร"
    assert r["region"] == "กรุงเทพฯ/ปริมณฑล"


def test_address_empty():
    r = normalize_address("")
    assert r["address"] == ""
    assert r["region"] == "ไม่ระบุภาค"


# --------------------------------------------------------------------------
# normalize_customer — merge logic + confidence
# --------------------------------------------------------------------------
def _row(name="", phone="", contact="", address=""):
    return {"name": name, "phone": phone, "contact": contact, "address": address}


def test_nc_auto_simple_fax_split():
    r = normalize_customer(_row(name="บริษัท เอ จำกัด",
                                phone="02-7303880-1,F:02-7303882"))
    assert r["proposed"]["phone"] == "02-7303880-1"
    assert r["proposed"]["fax"] == "02-7303882"
    assert r["confidence"] == "auto"
    assert "fax_in_phone" in r["issues"]
    assert lossless_ok(r)


def test_nc_auto_three_phones_one_fax():
    r = normalize_customer(_row(name="ร้าน บี",
                                phone="02-2142905,02-2154868,02-2169345,FAX:02-2155776"))
    assert r["proposed"]["phone"] == "02-2142905,02-2154868,02-2169345"
    assert r["proposed"]["fax"] == "02-2155776"
    assert r["confidence"] == "auto"
    assert lossless_ok(r)


def test_nc_auto_label_stripped():
    r = normalize_customer(_row(name="ร้าน ซี", phone="086-3379146 โทร 034-872616"))
    assert r["proposed"]["phone"] == "086-3379146,034-872616"
    assert r["confidence"] == "auto"
    assert lossless_ok(r)


def test_nc_review_person_in_phone():
    r = normalize_customer(_row(name="ร้าน ดี",
                                phone="035-629292-3, เฮีย 089-8048070,065-9920959"))
    assert r["proposed"]["phone"] == "035-629292-3,065-9920959"
    # the person+number stays intact, lands in contact
    assert "เฮีย 089-8048070" in r["proposed"]["contact"]
    assert r["confidence"] == "review"
    assert "person_in_phone" in r["issues"]
    assert lossless_ok(r)


def test_nc_review_line_in_phone():
    r = normalize_customer(_row(name="ร้าน อี",
                                phone="Line 094-5477724,086-5675864 เจ๊นุช"))
    assert r["proposed"]["phone"] == "086-5675864"
    assert r["confidence"] == "review"
    assert "line_in_phone" in r["issues"]
    # line id and person preserved somewhere in the proposal
    assert "094-5477724" in r["proposed"]["contact"]
    assert "เจ๊นุช" in r["proposed"]["contact"]
    assert lossless_ok(r)


def test_nc_note_in_phone_to_note_field():
    # A delivery-schedule note in the phone field → its own note field; phone cleaned → auto.
    r = normalize_customer(_row(name="ร้าน เอฟ", phone="02-5262280  วางจัน,อัง,พฤ,ศุกร"))
    assert r["proposed"]["phone"] == "02-5262280"
    assert "วางจัน" in r["proposed"]["note"]
    assert "วางจัน" not in r["proposed"]["contact"]
    assert "note_in_phone" in r["issues"]
    assert r["confidence"] == "auto"
    assert lossless_ok(r)


def test_nc_continuation_in_phone_auto():
    # Continuation numbers inherit the area code and are auto-applied (Put 2026-06-15);
    # still flagged inferred_area_code so the change is traceable.
    r = normalize_customer(_row(name="ร้าน จี",
                                phone="043-519373,512324,525408,F:043-518198"))
    assert r["proposed"]["phone"] == "043-519373,043-512324,043-525408"
    assert r["proposed"]["fax"] == "043-518198"
    assert r["confidence"] == "auto"
    assert "inferred_area_code" in r["issues"]
    assert lossless_ok(r)


def test_nc_old_mobile_modernized_review():
    # Old 9-digit mobile → modern 08X format, flagged legacy_mobile, kept in review (may be dead).
    r = normalize_customer(_row(name="ร้าน ไอ", phone="01-4862090"))
    assert r["proposed"]["phone"] == "081-4862090"
    assert "legacy_mobile" in r["issues"]
    assert r["confidence"] == "review"
    assert lossless_ok(r)


def test_nc_note_in_contact_to_note_field():
    # Billing note in the contact field → note field; contact no longer carries it.
    r = normalize_customer(_row(name="ร้าน เจ", phone="02-5101049,02-5103227",
                                contact="วางบิล 1-10"))
    assert "วางบิล 1-10" in r["proposed"]["note"]
    assert "วางบิล" not in r["proposed"]["contact"]
    assert lossless_ok(r)


def test_nc_review_name_changed_pulls_phone():
    r = normalize_customer(_row(name="คุณ งามจิต 081-8191774"))
    assert r["confidence"] == "review"
    assert "name_changed" in r["issues"] or "phone_in_name" in r["issues"]
    assert r["proposed"]["name"] == "คุณ งามจิต"
    # the phone pulled from the name must survive in the proposal
    assert "081-8191774" in (r["proposed"]["phone"] + r["proposed"]["contact"])
    assert lossless_ok(r)


def test_nc_bare_local_after_areacode_auto():
    # 4471787 follows a Bangkok number → inherits '02' → '02-4471787', auto-applied.
    r = normalize_customer(_row(name="ร้าน เอช", phone="02-4471527,4471787"))
    assert r["proposed"]["phone"] == "02-4471527,02-4471787"
    assert r["confidence"] == "auto"
    assert "inferred_area_code" in r["issues"]
    assert lossless_ok(r)


def test_nc_noop_already_clean():
    # single clean phone, nothing to change -> auto, no-op, no issues
    r = normalize_customer(_row(name="ร้าน ไอ", phone="02-4654534"))
    assert r["proposed"]["phone"] == "02-4654534"
    assert r["proposed"]["fax"] == ""
    assert r["confidence"] == "auto"
    assert r["issues"] == []
    assert lossless_ok(r)


def test_nc_contact_name_with_fax_only_is_auto():
    # contact carrying only a plain name + phone has only fax markers? -> here contact is plain name
    r = normalize_customer(_row(name="บริษัท เจ",
                                phone="02-6170255,02-6196469,F:02-6170255",
                                contact="นางบรรจงศรี ชัยวัฒน์"))
    assert r["proposed"]["fax"] == "02-6170255"
    assert r["proposed"]["phone"] == "02-6170255,02-6196469"
    assert "นางบรรจงศรี ชัยวัฒน์" in r["proposed"]["contact"]
    assert r["confidence"] == "auto"
    assert lossless_ok(r)


def test_nc_contact_with_phone_forces_review():
    # contact field carrying its own phone -> ambiguous -> review
    r = normalize_customer(_row(name="บริษัท เค",
                                phone="02-6139300-1,085-0856611",
                                contact="083-7888234"))
    assert r["confidence"] == "review"
    assert "phone_in_contact" in r["issues"]
    assert "083-7888234" in r["proposed"]["contact"]
    assert lossless_ok(r)


def test_nc_same_number_as_phone_and_fax_keeps_both():
    # 68ช002: a number listed BOTH as a contact phone AND as a fax. Stripping the fax must NOT
    # also wipe the identical phone copy — both must survive (lossless).
    r = normalize_customer(_row(name="บริษัท ชุนการไฟฟ้า จำกัด",
                                phone="082-1792372 พี่แอน",
                                contact="087-8471541,077-601057 F:077-601057"))
    assert r["proposed"]["fax"] == "077-601057"
    assert "077-601057" in r["proposed"]["contact"]   # the phone copy is preserved
    assert "087-8471541" in r["proposed"]["contact"]
    assert lossless_ok(r)
    assert r["confidence"] == "review"


def test_nc_original_preserved_verbatim():
    row = _row(name="x", phone=" 02-1234567 ", contact="abc", address="def")
    r = normalize_customer(row)
    assert r["original"] == {"name": "x", "phone": " 02-1234567 ",
                             "contact": "abc", "address": "def"}


def test_nc_handles_none_fields():
    r = normalize_customer({"name": None, "phone": None,
                            "contact": None, "address": None})
    assert r["proposed"]["phone"] == ""
    assert lossless_ok(r)


# --------------------------------------------------------------------------
# Lossless invariant must hold for EVERY fixture row
# --------------------------------------------------------------------------
LOSSLESS_FIXTURE_ROWS = [
    _row(name="บริษัท เอ จำกัด", phone="02-7303880-1,F:02-7303882"),
    _row(name="ร้าน บี", phone="02-2142905,02-2154868,02-2169345,FAX:02-2155776"),
    _row(name="ร้าน ซี", phone="086-3379146 โทร 034-872616"),
    _row(name="ร้าน ดี", phone="035-629292-3, เฮีย 089-8048070,065-9920959"),
    _row(name="ร้าน อี", phone="Line 094-5477724,086-5675864 เจ๊นุช"),
    _row(name="ร้าน เอฟ", phone="02-5262280  วางจัน,อัง,พฤ,ศุกร"),
    _row(name="ร้าน จี", phone="043-519373,512324,525408,F:043-518198"),
    _row(name="คุณ งามจิต 081-8191774"),
    _row(name="ร้าน เอช", phone="02-4471527,4471787"),
    _row(name="ร้าน ไอ", phone="02-4654534"),
    _row(name="บริษัท เจ", phone="02-6170255,02-6196469,F:02-6170255",
         contact="นางบรรจงศรี ชัยวัฒน์"),
    _row(name="บริษัท เค", phone="02-6139300-1,085-0856611", contact="083-7888234"),
    _row(name="คุณออย(081-4834024)"),
    _row(name="ร้าน ทิพย์วารี 2008"),
    _row(name="บจก. กรีนไดมอนด์(00002)"),
    _row(name="หจก. นิววิเศษพาณิชย์(1991)"),
    _row(name="ร้าน แอล", phone="เฮีย081-0433196,ซ้อ087-2030950,056-352038"),
    _row(name="ร้าน เอ็ม", phone="(034)217756,034-218587,099-1955422 (ซ้อ)"),
    _row(name="ร้าน เอ็น", phone="3794746"),
]


@pytest.mark.parametrize("row", LOSSLESS_FIXTURE_ROWS)
def test_lossless_every_fixture(row):
    r = normalize_customer(row)
    assert lossless_ok(r) is True


def test_lossless_ok_detects_drop():
    # construct a result that drops a 7+ digit run -> lossless_ok must be False
    bad = {
        "original": {"name": "", "phone": "02-1234567,089-7654321",
                     "contact": "", "address": ""},
        "proposed": {"name": "", "nickname": None, "phone": "02-1234567",
                     "fax": "", "contact": "", "address": "", "region": "ไม่ระบุภาค"},
        "confidence": "auto",
        "issues": [],
    }
    assert lossless_ok(bad) is False
