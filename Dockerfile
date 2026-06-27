FROM python:3.12-slim

WORKDIR /app

# 时区设为上海，便于日期默认值正确
ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1

# 先装依赖（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.cloud.tencent.com/pypi/simple -r requirements.txt

# 拷贝项目代码
COPY . .

# 缓存目录（数据CSV / PE）
RUN mkdir -p /app/data_cache

EXPOSE 5000

# 用 gunicorn 跑生产服务；akshare 拉数偶尔较慢，timeout 给大一些
CMD ["gunicorn", "-w", "2", "-k", "gthread", "--threads", "8", \
     "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
