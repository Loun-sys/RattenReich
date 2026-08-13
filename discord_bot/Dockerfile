FROM node:22-slim AS activity-build
WORKDIR /activity
COPY activity_src/package.json ./
RUN npm install
COPY activity_src/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1     RATTEN_DATA_DIR=/app/data
WORKDIR /app
RUN apt-get update     && apt-get install -y --no-install-recommends fonts-dejavu-core     && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=activity-build /activity/dist /app/activity_dist
RUN mkdir -p /app/data
CMD ["python", "bot.py"]