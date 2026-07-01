# สรุปข่าวตลาดหุ้นรายวันทางอีเมล

สคริปต์ `market_news_daily.py` ดึงราคาปิดล่าสุดและ % การเปลี่ยนแปลงของดัชนีหลัก
ในตลาดสำคัญ (สหรัฐฯ, ยุโรป, เอเชีย, ตลาดเกิดใหม่) จาก Yahoo Finance (ฟรี ไม่ต้องมี API key)
พร้อม **พาดหัวข่าวเด่น** จาก RSS ฟรี (CNBC, MarketWatch) แล้วส่งสรุปภาษาไทยเข้า **อีเมล**
ทุกเช้าเวลา **07:30 น. (เวลาไทย)** วันจันทร์–ศุกร์ ผ่าน GitHub Actions โดยอัตโนมัติ
(คุณเปิดอีเมลแล้วกด forward ต่อเข้ากลุ่ม LINE ของทีมได้เลย)

เพิ่ม/แก้แหล่งข่าวได้ที่ตัวแปร `NEWS_FEEDS` และจำนวนพาดหัวที่ `HEADLINE_LIMIT`
ในไฟล์ `market_news_daily.py` (feed ที่ล่มหรือ URL ผิดจะถูกข้ามโดยอัตโนมัติ ไม่ทำให้สรุปพัง)

## ตลาดที่ครอบคลุม

สหรัฐฯ (S&P 500, Nasdaq, Dow) · เยอรมนี · ฝรั่งเศส · สเปน · อิตาลี · ญี่ปุ่น ·
เกาหลีใต้ · จีน · ฮ่องกง · ไต้หวัน · อินเดีย (Sensex, Nifty) · บราซิล · อินโดนีเซีย

แก้ไข/เพิ่ม/ลบตลาดได้ที่ตัวแปร `MARKETS` ในไฟล์ `market_news_daily.py`

## ตั้งค่าครั้งแรก (ทำครั้งเดียว ~5 นาที)

### 1) สร้าง Gmail App Password

ต้องเปิด **2-Step Verification** ในบัญชี Google ก่อน (ถ้ายังไม่เปิด: myaccount.google.com/security)
จากนั้น:

1. เข้า [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
2. ตั้งชื่อ (เช่น `market-brief`) แล้วกด **Create**
3. จะได้รหัส 16 ตัว (เช่น `abcd efgh ijkl mnop`) — **คัดลอกเก็บไว้** (นี่คือ "กุญแจ" ไม่ใช่รหัส Gmail ปกติ)

### 2) ใส่ค่าเป็น GitHub Secrets

ไปที่ repository → **Settings → Secrets and variables → Actions → New repository secret**
แล้วเพิ่มค่าต่อไปนี้:

| ชื่อ Secret | ค่า | จำเป็น |
| --- | --- | --- |
| `GMAIL_USER` | อีเมล Gmail ที่ใช้ส่ง (เช่น `you@gmail.com`) | ✅ |
| `GMAIL_APP_PASSWORD` | รหัส 16 ตัวจากข้อ 1 (ใส่ได้ทั้งมี/ไม่มีเว้นวรรค) | ✅ |
| `MAIL_TO` | อีเมลผู้รับ (เว้นว่างได้ = ส่งหาตัวเอง) | – |

## ทดสอบ

- **ดูข้อความอย่างเดียว (ไม่ส่ง):**
  ```
  pip install -r scripts/requirements.txt
  python scripts/market_news_daily.py --dry-run
  ```
- **ทดสอบดึงข้อมูลจริงบน GitHub Actions:** แท็บ **Actions → Daily market news email
  → Run workflow** แล้วติ๊ก `dry_run` = true (ดึงตัวเลขจริง พิมพ์ log โดยไม่ส่งอีเมล)
- **ทดสอบส่งจริง:** Run workflow โดยไม่ติ๊ก `dry_run` (ต้องตั้ง secrets ครบก่อน)

## ปรับเวลา/วันที่ส่ง

แก้ `cron` ในไฟล์ `.github/workflows/daily-market-news.yml`
เวลาเป็น **UTC** (เวลาไทย = UTC+7) เช่น `30 0 * * 1-5` = 07:30 น. ไทย จันทร์–ศุกร์

## ส่งเข้า LINE แทนอีเมล (ทางเลือก)

สคริปต์รองรับ LINE Messaging API ด้วย หากตั้ง `LINE_CHANNEL_ACCESS_TOKEN` และ `LINE_TO`
เป็น secrets (แทน `GMAIL_USER`) ระบบจะส่งเข้า LINE โดยตรงแทนอีเมล

## หมายเหตุ

- ตลาดต่างโซนเวลาปิดคนละช่วง ตัวเลขในสรุปเดียวกันจึงเป็น "ราคาปิดล่าสุด" ของแต่ละตลาด
- เวอร์ชันนี้เน้นตัวเลขดัชนี (แม่นยำ เชื่อถือได้จากแหล่งฟรี) หากต้องการ **บทวิเคราะห์/พาดหัวข่าว**
  จาก Bloomberg/Refinitiv/WSJ ฯลฯ สามารถต่อยอดด้วย subscription/API ที่มีอยู่ได้
