FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home his
COPY backend/ .
COPY integrations/ /integrations/
ENV PYTHONPATH=/
RUN mkdir -p /app/data /data && chown -R his:his /app /data
USER his
EXPOSE 8787
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--no-access-log"]
