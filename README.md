# Production Plan Board

เว็บแอปพลิเคชันระบบจัดการและแสดงผลแผนการผลิต (Production Plan Board) พัฒนาด้วย Python (Flask)

---

## 🚀 การรันแอปพลิเคชัน (Running the Application)

### 1. รันสำหรับทดสอบบน Windows (Development)

> **หมายเหตุ:** Gunicorn ไม่รองรับการทำงานบน Windows โดยตรง ให้รันผ่าน Python หรือใช้ Waitress แทน

```bash
python app.py
```

### 2. รันสำหรับใช้งานจริงบน Linux (Production)

```bash
gunicorn -w 4 -b 0.0.0.0:22000 app:app
```

**คำอธิบายพารามิเตอร์:**

* `-w 4` : จำนวน Worker Processes (สูตรแนะนำทั่วไปคือ `(2 x จำนวน CPU Core) + 1`)
* `-b 0.0.0.0:22000` : เปิดใช้งานทุก IP บนพอร์ต `22000`
* `app:app` : ชื่อไฟล์ (`app.py`) : ชื่อตัวแปร Flask instance (`app`)

---

## ⚙️ การตั้งค่าให้รันเบื้องหลังอัตโนมัติบน Linux (Systemd Service)

เพื่อให้ระบบทำงานเบื้องหลังตลอดเวลาและเริ่มต้นทำงานอัตโนมัติเมื่อเปิดเครื่อง Server ให้ตั้งค่าผ่าน `systemd` ดังนี้:

### ขั้นตอนที่ 1: สร้างไฟล์คอนฟิกสำหรับ Service

เปิดไฟล์ด้วยคำสั่ง:

```bash
sudo nano /etc/systemd/system/production-plan-board.service
```

เพิ่มเนื้อหาต่อไปนี้ลงในไฟล์:

```ini
[Unit]
Description = Production Plan Board Background Service
After = network.target

[Service]
Type = simple
User = tte-it
WorkingDirectory = /home/tte-it/PycharmProjects/Production-Plan-Board
Environment = "PATH=/home/tte-it/PycharmProjects/Production-Plan-Board/.venv/bin"
ExecStart = /home/tte-it/PycharmProjects/Production-Plan-Board/.venv/bin/gunicorn -w 4 -b 0.0.0.0:22000 app:app
Restart = always
RestartSec = 5

[Install]
WantedBy = multi-user.target
```

*(บันทึกไฟล์โดยกด `Ctrl + O` -> `Enter` และออกด้วย `Ctrl + X`)*

### ขั้นตอนที่ 2: เปิดใช้งานและเริ่มต้น Service (Auto Start)

```bash
# โหลดไฟล์ตั้งค่า service ใหม่
sudo systemctl daemon-reload

# ตั้งค่าให้เปิดทำงานอัตโนมัติเมื่อบูตเครื่อง
sudo systemctl enable production-plan-board.service

# สั่งเริ่ม/รีสตาร์ต service ให้เริ่มรันด้วย Gunicorn
sudo systemctl restart production-plan-board.service

# ตรวจสอบสถานะการทำงาน
sudo systemctl status production-plan-board.service
```

---

## 🔍 คำสั่งดูแลระบบเพิ่มเติม (Troubleshooting)

* **หยุดการทำงานของ Service:**
  ```bash
  sudo systemctl stop production-plan-board.service
  ```
* **ดู Log การทำงานย้อนหลัง (กรณีระบบมีปัญหา):**
  ```bash
  sudo journalctl -u production-plan-board.service -f
  ```
