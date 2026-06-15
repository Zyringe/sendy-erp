import customer_geo as geo


def test_province_parsed_from_address_tail():
    assert geo.province_of("12 ถ.มิตรภาพ ต.ในเมือง อ.เมือง นครราชสีมา 30000") == "นครราชสีมา"


def test_province_none_when_absent():
    assert geo.province_of("ไม่มีจังหวัดในนี้") is None


def test_region_isan():
    assert geo.region_of("... ขอนแก่น 40000") == "ภาคอีสาน"


def test_region_bangkok_bucket():
    assert geo.region_of("... กรุงเทพมหานคร 10200") == "กรุงเทพฯ/ปริมณฑล"


def test_region_unknown_bucket():
    assert geo.region_of("ที่อยู่ไม่มีจังหวัด") == "ไม่ระบุภาค"
