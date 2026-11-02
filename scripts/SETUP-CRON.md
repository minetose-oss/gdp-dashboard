# ตั้งให้อีเมลส่งตรงเวลาทุกวัน (ทำครั้งเดียว ~10 นาที)

## ทำไมต้องทำ

GitHub cron **พึ่งไม่ได้** สำหรับงานนี้ — มีหลักฐานจาก repo นี้เอง:

| อาการ | หลักฐาน |
| --- | --- |
| สายทุกวัน | ตั้งไว้ 23:30 UTC แต่รันจริง 01:30–02:36 UTC (สาย 2–3 ชม.) ตลอด 68 รัน |
| ถูกปิดเงียบ ๆ | 60 วันที่ repo ไม่มี commit → GitHub ปิด workflow อัตโนมัติ อีเมลหายไปทั้งเดือน ต.ค. |
| ข้ามรอบ | 2 พ.ย. 2026 workflow เป็น `active` cron ถูกต้อง แต่ GitHub ไม่ยิงเลย |

วิธีแก้คือให้บริการภายนอกเป็นคนกดปุ่มแทน GitHub cron — workflow รองรับอยู่แล้ว
ผ่าน trigger `repository_dispatch: types: [daily-brief]`

---

## ขั้นที่ 1 — สร้าง GitHub token

1. เปิด **https://github.com/settings/personal-access-tokens**
2. กด **Generate new token** → เลือกแบบ **Fine-grained**
3. กรอก:
   - **Token name**: `market-brief-cron`
   - **Expiration**: เลือก **No expiration** (ถ้าเลือกวันหมดอายุ ต้องกลับมาต่ออายุ ไม่งั้นอีเมลหยุดอีก)
   - **Repository access**: เลือก **Only select repositories** → ติ๊ก `minetose-oss/gdp-dashboard`
4. เลื่อนลงไปที่ **Permissions → Repository permissions**
   หา **Contents** แล้วเปลี่ยนจาก `No access` เป็น **Read and write**
   (อันเดียวพอ ไม่ต้องเปิดอย่างอื่น)
5. กด **Generate token** ด้านล่างสุด
6. **คัดลอกรหัสที่ขึ้นมา** (ขึ้นต้นด้วย `github_pat_`) เก็บไว้ก่อน —
   หน้านี้จะโชว์ครั้งเดียว ปิดแล้วดูซ้ำไม่ได้

---

## ขั้นที่ 2 — ตั้ง cron ที่ cron-job.org

1. สมัครฟรีที่ **https://cron-job.org** แล้วล็อกอิน
2. กด **CREATE CRONJOB** (ปุ่มขวาบน)
3. แท็บ **COMMON**
   - **Title**: `สรุปตลาดหุ้นเช้า`
   - **URL**: `https://api.github.com/repos/minetose-oss/gdp-dashboard/dispatches`
   - **Execution schedule**: เลือก **Custom**
     - **Days of week**: ติ๊ก Mon, Tue, Wed, Thu, Fri (เอา Sat, Sun ออก)
     - **Days of month / Months**: เลือก `Every`
     - **Hours**: ติ๊กเฉพาะ `6`
     - **Minutes**: ติ๊กเฉพาะ `0`
   - มองหาช่อง **Timezone** แล้วเลือก **Asia/Bangkok** (สำคัญมาก ไม่งั้นได้เวลา UTC)
4. แท็บ **ADVANCED**
   - **Request method**: เปลี่ยนจาก `GET` เป็น **POST**
   - **Headers** — กด **ADD HEADER** ทีละอัน รวม 4 อัน:

     | Key | Value |
     | --- | --- |
     | `Accept` | `application/vnd.github+json` |
     | `Authorization` | `Bearer github_pat_xxxxx` ← วาง token จากขั้นที่ 1 ต่อท้ายคำว่า Bearer เว้นวรรค 1 ที |
     | `Content-Type` | `application/json` |
     | `X-GitHub-Api-Version` | `2022-11-28` |

   - **Request body**: วางข้อความนี้ลงไปตรง ๆ

     ```
     {"event_type":"daily-brief"}
     ```
5. กด **CREATE** (ปุ่มขวาบน)

---

## ขั้นที่ 3 — ทดสอบทันที ไม่ต้องรอพรุ่งนี้

1. ในหน้า cronjob ที่เพิ่งสร้าง กด **TEST RUN**
2. ดูผลลัพธ์ที่ขึ้นมา — ต้องได้ **HTTP 204 No Content**
   (204 คือสำเร็จ GitHub ตอบแบบไม่มีเนื้อหา ถือว่าถูกต้อง)
3. เปิด **https://github.com/minetose-oss/gdp-dashboard/actions**
   ต้องเห็นรันใหม่ขึ้นมาภายใน ~10 วินาที โดยช่อง Event เขียนว่า `repository_dispatch`
4. รอประมาณ 2–3 นาที แล้วเช็กอีเมล

### ถ้าไม่ได้ 204

| ได้รหัส | แปลว่า | แก้ยังไง |
| --- | --- | --- |
| `401` | token ผิดหรือหมดอายุ | สร้าง token ใหม่ที่ขั้นที่ 1 ระวังอย่าให้มีช่องว่างเกินตอนวาง |
| `403` | token ไม่มีสิทธิ์ | กลับไปแก้ token ให้ **Contents = Read and write** |
| `404` | URL ผิด หรือ token ไม่ได้ผูกกับ repo นี้ | เช็ก URL ให้ตรงเป๊ะ และเช็กว่าติ๊ก repo ถูกตัว |
| `422` | body ผิด | ต้องเป็น `{"event_type":"daily-brief"}` เป๊ะ ๆ |

---

## ขั้นที่ 4 — ปิด GitHub cron ทิ้ง (หลังขั้นที่ 3 ผ่านแล้วเท่านั้น)

ถ้าปล่อยไว้ทั้งคู่ จะได้อีเมลวันละ 2 ฉบับ เมื่อมั่นใจว่า cron-job.org ทำงานดีแล้ว
บอกผมได้ ผมจะลบบล็อก `schedule` ในไฟล์ workflow ให้ (หรือคุณแก้เองก็ได้ที่
`.github/workflows/daily-market-news.yml` ลบ 4 บรรทัดที่ขึ้นต้นด้วย `schedule:`)

ตัว `repository_dispatch` และ `workflow_dispatch` ต้องเก็บไว้ ห้ามลบ

---

## สรุปสั้น ๆ ว่าหลังทำเสร็จจะเป็นยังไง

- **06:00 น. ทุกวันจันทร์–ศุกร์** cron-job.org ยิงไปที่ GitHub
- GitHub รัน workflow → ดึงราคาจริง → ให้ Claude ค้นข่าวจริงและเขียนบทวิเคราะห์
  → เรนเดอร์ PDF + PNG → ส่งอีเมล
- ใช้เวลาประมาณ 2–3 นาที อีเมลถึงราว **06:03 น.**
- ไม่ต้องพึ่ง GitHub cron อีกเลย และ keepalive ยังทำงานกันไม่ให้ workflow ถูกปิดซ้ำ
