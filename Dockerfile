FROM python:3.11-slim

WORKDIR /app
COPY . .

RUN python -m py_compile server.py

ENV HOST=0.0.0.0
CMD ["python", "server.py"]
