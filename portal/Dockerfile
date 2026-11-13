FROM python:3.11-slim

WORKDIR /srv/portal
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

EXPOSE 5002
CMD ["python", "app.py"]
