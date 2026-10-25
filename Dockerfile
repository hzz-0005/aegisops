FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
RUN python -c "import tomllib; p=tomllib.load(open('pyproject.toml','rb')); open('/tmp/requirements.txt','w').write('\n'.join(p['project']['dependencies']))" \
    && pip install --no-cache-dir -r /tmp/requirements.txt
COPY src ./src
COPY apps ./apps
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir --no-deps .
EXPOSE 8000
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
