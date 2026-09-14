# =============================================================================
# Frontend image - nginx เสิร์ฟ HTML/CSS/JS ธรรมดา (ไม่มี build step)
# nginx:alpine มี image ทั้ง amd64 และ arm64 ใช้ได้ทั้งบน Windows และ Pi 5
# =============================================================================
FROM nginx:alpine

ENV TZ=Asia/Bangkok

# คัดลอกไฟล์เข้า image เผื่อกรณีรัน production โดยไม่ mount volume
# (ตอน dev docker-compose.yml จะ mount ไฟล์จาก host มาทับให้อัตโนมัติ)
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
COPY css /usr/share/nginx/html/css
COPY js  /usr/share/nginx/html/js

EXPOSE 80
