FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY schooltool.py main.py ./
USER nobody
CMD ["python", "-u", "main.py"]
