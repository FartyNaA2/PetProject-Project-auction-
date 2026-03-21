FROM python:3.10-slim

WORKDIR /app

# Копіюємо список залежностей та встановлюємо їх
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копіюємо всі файли нашого проєкту (app.py, index.html)
COPY . .

EXPOSE 5000

# Запускаємо додаток (Gunicorn автоматично підхопить змінну середовища $PORT)
CMD gunicorn -w 2 -b 0.0.0.0:${PORT:-5000} app:app