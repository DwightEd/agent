FROM python:3.12-slim
WORKDIR /app
COPY requirements-pdf.txt .
RUN pip install --no-cache-dir -r requirements-pdf.txt && useradd --create-home --uid 10001 research
COPY backend ./backend
COPY frontend ./frontend
COPY examples ./examples
RUN mkdir /app/data && chown research:research /app/data
USER research
ENV HOST=0.0.0.0 PORT=8000 DATABASE_PATH=/app/data/researchops.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3)"
CMD ["python", "-m", "backend.server"]
