# 1. 基础镜像：选择一个轻量级的 Python 环境
FROM python:3.12-slim

# 2. 设置工作目录：容器里的代码放哪里
WORKDIR /app

# 3. 复制依赖清单（先复制这个，利用 Docker 缓存，加快构建速度）
COPY requirements.txt .

# 4. 安装依赖（在容器里安装，不是在你的电脑上）
RUN pip install --no-cache-dir -r requirements.txt

# 5. 复制项目所有代码到容器的工作目录
COPY . .
# 6.删除所有 Excel 文件（私钥文件）
RUN find /app/scripts -name "*.xlsx" -type f -delete

# 7. 声明容器运行时监听的端口（只是声明，实际映射在运行命令时指定）
EXPOSE 8000

# 8. 启动命令：容器启动时执行什么
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
