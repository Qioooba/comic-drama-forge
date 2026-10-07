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
# 2026-10-07：5000 → 45871，与 app/ports.py、docker-compose.yml 的映射保持一致。
# 三处必须同步，否则 compose 起容器后端口映射对不上、健康检查恒失败。
EXPOSE 45871

# 设置环境变量
ENV FLASK_APP=app/serve.py
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1
# 显式声明监听端口：.env 被 .dockerignore 排除不会进镜像，容器里没有 APP_PORT
# 就只能落到代码默认值；写死在这里让「镜像实际行为」不依赖代码默认值是否被改过。
ENV APP_HOST=127.0.0.1
ENV APP_PORT=45871

# 启动命令
CMD ["python", "app/serve.py"]
