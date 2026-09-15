# 轻量 Webhook 转发服务：Python 标准库单文件，无第三方依赖
# alpine 基础镜像（含 CA 证书），镜像约 25MB，运行内存 <50MB
FROM python:3.11-alpine

# 去重数据库 /opt/bluebird/bluebird.db 由 compose 数据卷提供
COPY server.py ui.html login.html favicon.ico /app/
COPY docs/ /app/docs/
WORKDIR /app

EXPOSE 8082
CMD ["python3", "/app/server.py"]
