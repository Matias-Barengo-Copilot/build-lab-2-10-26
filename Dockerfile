FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pipeline.py app.py ./
COPY .streamlit ./.streamlit

# Run as a non-root user
RUN useradd -m appuser && chown -R appuser /app
USER appuser

# Hosting platforms inject PORT; default 8501 for local runs.
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request;urllib.request.urlopen('http://localhost:%s/_stcore/health' % os.environ.get('PORT','8501'))" || exit 1
CMD ["sh", "-c", "streamlit run app.py --server.port=${PORT:-8501} --server.address=0.0.0.0"]
