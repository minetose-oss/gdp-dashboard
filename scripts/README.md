# สรุปข่าวตลาดหุ้นรายวันเข้า LINE

สคริปต์ `market_news_daily.py` ดึงราคาปิดล่าสุดและ % การเปลี่ยนแปลงของดัชนีหลัก
ในตลาดสำคัญ (สหรัฐฯ, ยุโรป, เอเชีย, ตลาดเกิดใหม่) จาก Yahoo Finance (ฟรี ไม่ต้องมี API key)
แล้วส่งสรุปภาษาไทยเข้า LINE ผ่าน Messaging API ทุกเช้าเวลา **07:30 น. (เวลาไทย)** วันจันทร์–ศุกร์
ผ่าน GitHub Actions

## ตลาดที่ครอบคลุม

สหรัฐฯ (S&P 500, Nasdaq, Dow) · เยอรมนี · ฝรั่งเศส · สเปน · อิตาลี · ญี่ปุ่น ·
เกาหลีใต้ · จีน · ฮ่องกง · ไต้หวัน · อินเดีย (Sensex, Nifty) · บราซิล · อินโดนีเซีย

แก้ไข/เพิ่ม/ลบตลาดได้ที่ตัวแปร `MARKETS` ในไฟล์ `market_news_daily.py`

## ตั้งค่าครั้งแรก (ทำครั้งเดียว)

### 1) สร้าง LINE Official Account + Messaging API

1. เข้า [LINE Developers Console](https://developers.line.biz/console/) แล้วล็อกอิน
2. สร้าง **Provider** → สร้าง **Messaging API channel**
3. ในแท็บ **Messaging API** กด **Issue** เพื่อออก **Channel access token (long-lived)** → คัดลอกเก็บไว้
4. เชิญ Official Account นี้เข้ากลุ่ม LINE ของทีม (หรือเพิ่มเป็นเพื่อน)

### 2) หา `LINE_TO` (ปลายทางที่จะส่ง)

`LINE_TO` คือ id ของกลุ่ม/ห้อง/ผู้ใช้ที่จะรับข้อความ วิธีหา group id ที่ง่ายที่สุด:
ตั้ง webhook ชั่วคราวเพื่ออ่าน `source.groupId` จาก event ที่ LINE ส่งมาเมื่อมีข้อความในกลุ่ม
(ดู [เอกสาร LINE](https://developers.line.biz/en/reference/messaging-api/#webhook-event-objects))
— ถ้าต้องการ แจ้งผมช่วยทำหน้า webhook ชั่วคราวให้เก็บ id ได้

### 3) ใส่ค่าเป็น GitHub Secrets

ไปที่ repository → **Settings → Secrets and variables → Actions → New repository secret**
แล้วเพิ่ม 2 ค่า:

| ชื่อ Secret | ค่า |
| --- | --- |
| `LINE_CHANNEL_ACCESS_TOKEN` | channel access token จากข้อ 1 |
| `LINE_TO` | group/user id จากข้อ 2 |

## ทดสอบ

- **บนเครื่องตัวเอง (ดูข้อความอย่างเดียว ไม่ส่ง):**
  ```
  pip install -r scripts/requirements.txt
  python scripts/market_news_daily.py --dry-run
  ```
- **ทดสอบดึงข้อมูลจริงบน GitHub Actions:** ไปที่แท็บ **Actions → Daily market news to LINE
  → Run workflow** แล้วติ๊ก `dry_run` = true (จะดึงตัวเลขจริงและพิมพ์ log โดยไม่ส่งเข้า LINE)
- **ทดสอบส่งจริง:** Run workflow โดยไม่ติ๊ก `dry_run` (ต้องตั้ง secrets ครบก่อน)

## ปรับเวลา/วันที่ส่ง

แก้ `cron` ในไฟล์ `.github/workflows/daily-market-news.yml`
เวลาเป็น **UTC** (เวลาไทย = UTC+7) เช่น `30 0 * * 1-5` = 07:30 น. ไทย จันทร์–ศุกร์

## หมายเหตุ

- ตลาดต่างโซนเวลาปิดคนละช่วง ตัวเลขในสรุปเดียวกันจึงเป็น "ราคาปิดล่าสุด" ของแต่ละตลาด
- เวอร์ชันนี้เน้นตัวเลขดัชนี (แม่นยำ เชื่อถือได้จากแหล่งฟรี) หากต้องการ **บทวิเคราะห์/พาดหัวข่าว**
  จาก Bloomberg/Refinitiv/WSJ ฯลฯ สามารถต่อยอดด้วย subscription/API ที่มีอยู่ได้
