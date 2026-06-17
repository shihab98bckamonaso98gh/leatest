FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Copy your bot code and requirements
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py .

# Railway uses $PORT for health‑check, bot already listens on it
ENV PORT=8080

CMD ["python", "bot.py"]