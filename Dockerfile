FROM python:3.12-slim
WORKDIR /app
COPY public/ ./public/
COPY scripts/start.sh ./scripts/start.sh
RUN chmod +x ./scripts/start.sh
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "./scripts/start.sh"]
