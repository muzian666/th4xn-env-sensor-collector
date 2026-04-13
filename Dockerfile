FROM python:3.13-slim

WORKDIR /app
COPY collector.py .
COPY dashboard.html .
COPY response_templates.json .

RUN pip install --no-cache-dir fastapi uvicorn

VOLUME /data

ENV DB_PATH=/data/sensor_data.db
ENV LISTEN_PORT=6666
ENV HTTP_PORT=8080
ENV TZ=Asia/Shanghai

EXPOSE 6666/udp
EXPOSE 8080/tcp

CMD ["python", "-u", "collector.py"]
