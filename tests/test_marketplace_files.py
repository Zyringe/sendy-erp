import io
import pandas as pd
from marketplace_files import detect_file

def _xlsx(sheets):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w:
        for name, df in sheets.items():
            df.to_excel(w, sheet_name=name, index=False, header=False)
    buf.seek(0); return buf

def test_detect_balance():
    df = pd.DataFrame([['รายงาน'], ['วันที่','ประเภทการทำธุรกรรม']])
    assert detect_file(_xlsx({'Transaction Report': df})) == ('balance', 'shopee')

def test_detect_income():
    df = pd.DataFrame([['x']])
    sheets = {'Summary': df, 'Income': df, 'Service Fee Details': df, 'Adjustment': df}
    assert detect_file(_xlsx(sheets)) == ('income', 'shopee')

def test_detect_shopee_order():
    df = pd.DataFrame([['หมายเลขคำสั่งซื้อ','ชื่อสินค้า','จำนวน']])
    assert detect_file(_xlsx({'orders': df})) == ('order', 'shopee')

def test_detect_unknown():
    df = pd.DataFrame([['foo','bar']])
    assert detect_file(_xlsx({'sheet1': df})) == (None, None)
