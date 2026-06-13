# customer_geo.py — จังหวัด parse + จังหวัด→ภาค bucket.
# Ported from sendy_erp/data/exports/_gen_customer_contact_list.py.
# กรุงเทพมหานคร is placed in its own "กรุงเทพฯ/ปริมณฑล" bucket (the source script
# handles this at zone level via zone "กท"; here we do it at province level).

# --- province -> macro-region (6 buckets + กรุงเทพฯ/ปริมณฑล; West folded into ภาคกลาง per Put) ---
REGION_PROVINCES = {
    "กรุงเทพฯ/ปริมณฑล": ["กรุงเทพมหานคร","นนทบุรี","ปทุมธานี","สมุทรปราการ","สมุทรสาคร"],
    "ภาคเหนือ": ["เชียงราย","เชียงใหม่","น่าน","พะเยา","แพร่","แม่ฮ่องสอน","ลำปาง","ลำพูน","อุตรดิตถ์"],
    "ภาคกลาง": ["กำแพงเพชร","ชัยนาท","นครนายก","นครปฐม","นครสวรรค์",
        "พระนครศรีอยุธยา","พิจิตร","พิษณุโลก","เพชรบูรณ์","ลพบุรี",
        "สมุทรสงคราม","สิงห์บุรี","สุโขทัย","สุพรรณบุรี","สระบุรี","อ่างทอง","อุทัยธานี",
        "ตาก","กาญจนบุรี","ราชบุรี","เพชรบุรี","ประจวบคีรีขันธ์"],
    "ภาคตะวันออก": ["จันทบุรี","ฉะเชิงเทรา","ชลบุรี","ตราด","ปราจีนบุรี","ระยอง","สระแก้ว"],
    "ภาคอีสาน": ["กาฬสินธุ์","ขอนแก่น","ชัยภูมิ","นครพนม","นครราชสีมา","บึงกาฬ","บุรีรัมย์","มหาสารคาม",
        "มุกดาหาร","ยโสธร","ร้อยเอ็ด","เลย","ศรีสะเกษ","สกลนคร","สุรินทร์","หนองคาย","หนองบัวลำภู",
        "อุดรธานี","อุบลราชธานี","อำนาจเจริญ"],
    "ภาคใต้": ["กระบี่","ชุมพร","ตรัง","นครศรีธรรมราช","นราธิวาส","ปัตตานี","พังงา","พัทลุง","ภูเก็ต",
        "ยะลา","ระนอง","สงขลา","สตูล","สุราษฎร์ธานี"],
}
PROV2REGION = {p: r for r, ps in REGION_PROVINCES.items() for p in ps}

# aliases / abbreviations -> canonical province
ALIASES = {"ประจวบฯ":"ประจวบคีรีขันธ์","ประจวบ":"ประจวบคีรีขันธ์","อยุธยา":"พระนครศรีอยุธยา",
    "โคราช":"นครราชสีมา","กรุงเทพ":"กรุงเทพมหานคร","กทม":"กรุงเทพมหานคร","ศรีษะเกษ":"ศรีสะเกษ"}

# scan list: (pattern, canonical_province) longest pattern first
SCAN = sorted([(p, p) for p in PROV2REGION] + list(ALIASES.items()),
              key=lambda kv: len(kv[0]), reverse=True)


def province_of(addr):
    if not addr: return None
    best_pos, best_prov = -1, None
    for pat, canon in SCAN:
        pos = addr.rfind(pat)          # closest-to-end wins (province sits at tail)
        if pos > best_pos:
            best_pos, best_prov = pos, canon
    return best_prov


UNKNOWN_REGION = "ไม่ระบุภาค"

_PROV_TO_REGION = {p: reg for reg, provs in REGION_PROVINCES.items() for p in provs}


def region_of(addr):
    p = province_of(addr)
    return _PROV_TO_REGION.get(p, UNKNOWN_REGION) if p else UNKNOWN_REGION


REGION_ORDER = ["กรุงเทพฯ/ปริมณฑล","ภาคกลาง","ภาคเหนือ","ภาคอีสาน","ภาคตะวันออก","ภาคใต้","ไม่ระบุภาค"]
