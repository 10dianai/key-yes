FROM python:3.12-slim

WORKDIR /app

# 先装依赖（利用镜像层缓存）；用清华 PyPI 源，国内构建不卡
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 应用代码
COPY run.py .
COPY app/ app/
COPY core/ core/
COPY static/ static/

# 数据目录（挂载卷持久化 pool_data.json / panel_auth.json / logs）
RUN mkdir -p /data
# 容器内必须监听 0.0.0.0，端口映射的流量才能进到服务（run.py 会读此环境变量）
ENV HOST=0.0.0.0

# 默认配置（敏感配置用 volume 覆盖或 local 配置）
COPY key_pool_config.json /data/key_pool_config.json

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"

WORKDIR /data
CMD ["python", "/app/run.py", "--config", "/data/key_pool_config.json"]
