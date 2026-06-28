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

# 用 gunicorn 跑生产服务；akshare 拉数偶尔较慢，timeout 给大一些。
# 单 worker + 多线程：扫描任务表(_scan_tasks)等为进程内内存状态，多 worker 会
# 导致"建任务"和"查进度"落到不同进程而互相看不见；扫描为 IO 密集，多线程已足够并发。
CMD ["gunicorn", "-w", "1", "-k", "gthread", "--threads", "16", \
     "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
