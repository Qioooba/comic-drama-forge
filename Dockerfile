FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
# 审计 P1-5（2026-09-29）：改用 app/requirements.txt —— 根目录 requirements.txt 是
# CLI 旧清单（仅 9 个包，缺 flask/waitress/cryptography/openai），用它装出的镜像
# `import flask` 直接 ModuleNotFoundError；缺 cryptography 还会让 secret_store
# 拒绝任何密钥落盘。桌面发布 CI（desktop-release.yml）装的也是这份完整清单。
COPY app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建输出目录
RUN mkdir -p output/final output/assets output/keyframes output/storyboards

# 暴露端口
EXPOSE 5000

# 设置环境变量
ENV FLASK_APP=app/serve.py
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

# 启动命令
CMD ["python", "app/serve.py"]
