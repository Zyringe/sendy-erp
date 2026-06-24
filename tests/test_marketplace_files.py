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

import io

def _csv_bytes(header):
    return io.BytesIO(('﻿' + header + '\r\n' + 'x'*0).encode('utf-8'))

def test_detect_lazada_statement_csv():
    h = ('Statement Period;Statement Number;Transaction Date;Fee Name;'
         'Amount(Include Tax);VAT Amount;Release Status;Release Date;Comment;'
         'Order Creation Date;Order Number;Order Line ID;Seller SKU;Lazada SKU;'
         'WHT Amount;WHT included in Amount;Order Status;Product Name;Short Code')
    assert detect_file(_csv_bytes(h)) == ('laz_statement', 'lazada')

def test_detect_lazada_wallet_csv():
    h = 'Transaction Number;Transaction Time;Type;Sub Type;Amount;Remarks'
    assert detect_file(_csv_bytes(h)) == ('laz_wallet', 'lazada')

def test_detect_lazada_statement_csv_thai():
    # Lazada Seller Center exports in the UI language; the Thai Account Statement
    # has Thai headers but is otherwise the same file.
    h = ('ระยะเวลาใบแจ้งยอด;รหัสรอบบิล;วันที่ทำรายการ;ชื่อรายการธุรกรรม;'
         'จำนวนเงิน(รวมภาษี);VAT Amount;สถานะการโอนเงิน;วันที่ปรับปรุงเข้ายอดของฉัน;'
         'ความคิดเห็น;วันที่สร้างคำสั่งซื้อ;หมายเลขคำสั่งซื้อ;รหัสสินค้าในคำสั่งซื้อ;'
         'SKU ร้านค้า;Lazada SKU;WHT Amount;WHT รวมอยู่ในจำนวนเงินแล้ว;'
         'สถานะคำสั่งซื้อ;ชื่อสินค้า;Short Code')
    assert detect_file(_csv_bytes(h)) == ('laz_statement', 'lazada')
