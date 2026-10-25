FROM python:3.12-slim
WORKDIR /app
COPY lab/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY lab ./lab
EXPOSE 8000

